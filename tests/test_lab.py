"""Testes do Lab do Studio: sessão → mensagem → outbound, eventos no barramento e SSE.

Sem rede e sem LLM: a `Conversation` é a real (policy, presenter, rules, QuoteClient com
`MockTransport`), e o que é dublê é só o `agno.Agent` dentro do `Extractor`/`Responder` —
assim o hook de trace é exercitado pelo código de produção.
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.brain import Extractor, Responder
from agent.conversation import Conversation, InMemoryStateStore
from agent.models import CepInfo, Extraction, Intent
from agent.rules import Rules
from agent.studio import lab as lab_mod
from agent.studio.lab import EventBus, LabManager, resumir_state, router
from tests.fakes import (
    ERRO_429,
    PLANOS,
    QUOTE_200,
    FakeAgnoAgent,
    FakeCepLookup,
    FakeRun,
    SleepFake,
    run_erro,
    sem_sono,
)

HOJE = date(2026, 9, 1)


def _fabrica(tmp_path, extracoes, respostas, status_quote=None):
    """`conversation_factory` do Lab: mesma `Conversation` de produção, sem rede."""
    restantes = list(status_quote or [])

    def quote_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planos":
            return httpx.Response(200, json=PLANOS)
        status = restantes.pop(0) if restantes else 200
        if status == 200:
            return httpx.Response(200, json=QUOTE_200)
        return httpx.Response(status, json={"error": "upstream_unavailable"})

    async def montar(*, base_url=None, trace=None, logger_factory=None, on_handoff=None, **_kw):
        from agent.quote_client import QuoteClient

        client = QuoteClient(
            base_url or "http://api.local",
            transport=httpx.MockTransport(quote_handler),
            sleep=sem_sono,
        )
        await client.get_planos()
        return Conversation(
            rules=Rules.from_planos(PLANOS, HOJE),
            quote_client=client,
            extractor=Extractor(agent=FakeAgnoAgent(list(extracoes)), sleep=SleepFake(), trace=trace),
            responder=Responder(agent=FakeAgnoAgent(list(respostas)), sleep=SleepFake(), trace=trace),
            log_dir=tmp_path,
            store=InMemoryStateStore(),
            today=lambda: HOJE,
            lookup_cep=FakeCepLookup(CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP")),
            logger_factory=logger_factory,
            on_handoff=on_handoff,
        )

    return montar


def _app(tmp_path, monkeypatch, extracoes=None, respostas=None, **kw) -> tuple[TestClient, LabManager]:
    monkeypatch.setattr(lab_mod, "_log_dir", lambda: tmp_path / "studio")
    fabrica = _fabrica(
        tmp_path,
        extracoes if extracoes is not None else [FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, idade=35))],
        respostas if respostas is not None else [FakeRun(content="Qual o ano do carro?")],
        **kw,
    )
    manager = LabManager(fabrica)
    app = FastAPI()
    app.include_router(router(manager))          # forma fábrica: router amarrado a este manager
    return TestClient(app), manager


def _sid(client: TestClient, api: str | None = None) -> str:
    resp = client.post("/api/lab/sessions", json={"api": api} if api else {})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# --------------------------------------------------------------------------- sessão e turno
def test_cria_sessao_com_a_api_escolhida(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    corpo = client.post("/api/lab/sessions", json={"api": "http://localhost:8001"}).json()
    assert corpo["id"].startswith("lab-")
    assert corpo["api"] == "http://localhost:8001"


def test_mensagem_devolve_outbound_e_estado(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    sid = _sid(client)

    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi, tenho 35 anos"}).json()

    # ordem F8: depois da idade vem o plano, que é template do presenter (não passa pelo LLM)
    assert len(corpo["outbound"]) == 1
    assert corpo["outbound"][0]["source"] == "template"
    assert "Qual deles quer cotar?" in corpo["outbound"][0]["text"]
    assert corpo["state"]["idade"] == 35
    assert corpo["state"]["stage"] == "escolha_plano"
    assert corpo["state"]["ultima_pergunta"] == "plano"
    assert client.get(f"/api/lab/sessions/{sid}/state").json()["idade"] == 35


def test_mensagem_do_lab_marca_a_origem_lab(tmp_path, monkeypatch):
    """A conversa do Lab aparece em Atendimentos com origem `lab`, não como lead de verdade."""
    client, _ = _app(tmp_path, monkeypatch)
    sid = _sid(client)
    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi"}).json()

    assert corpo["state"]["origem"] == "lab"
    linhas = (tmp_path / "studio" / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()
    inbound = json.loads(linhas[0])
    assert inbound["event"] == "inbound"
    assert inbound["data"]["origem"] == "lab"


def test_conversa_completa_ate_a_cotacao_com_o_agente_real(tmp_path, monkeypatch):
    """Caminho feliz de ponta a ponta: o preço só aparece depois da API responder."""
    # "01310-100" e "sim" nem chegam ao Extractor: o pré-parser do `conversation` resolve os
    # dois. O roteiro tem só os quatro turnos que vão ao LLM.
    extracoes = [
        FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, idade=35)),
        FakeRun(content=Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo")),
        FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, veiculo_texto="Onix", veiculo_ano=2019)),
        FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, data_inicio=HOJE)),
    ]
    respostas = [FakeRun(content=f"pergunta {n}") for n in range(1, 5)]
    client, _ = _app(tmp_path, monkeypatch, extracoes, respostas, status_quote=[503, 200])
    sid = _sid(client)

    # A data de início é o último campo da coleta, depois da confirmação do CEP.
    for texto in ["tenho 35", "quero o completo", "Onix 2019", "01310-100", "sim"]:
        client.post(f"/api/lab/sessions/{sid}/messages", json={"text": texto})
    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "hoje mesmo"}).json()

    assert corpo["state"]["stage"] == "apresentado"
    assert corpo["state"]["cotacao"]["outcome"] == "ok"
    assert corpo["state"]["cotacao"]["premio_mensal"] == 209.9
    assert corpo["state"]["cotacao"]["tentativas"] == 2          # a API caiu uma vez e o retry resolveu
    assert "R$ 209,90" in corpo["outbound"][-1]["text"]


def test_midia_sem_texto_nao_chama_o_llm(tmp_path, monkeypatch):
    client, manager = _app(tmp_path, monkeypatch, extracoes=[], respostas=[])
    sid = _sid(client)

    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"media_type": "audio"}).json()

    assert "escrever" in corpo["outbound"][0]["text"]
    assert corpo["outbound"][0]["source"] == "template"
    eventos = manager.sessao(sid).bus.historico()
    assert not [e for e in eventos if e["event"] == "llm_trace"]


# --------------------------------------------------------------------------- eventos
def test_barramento_recebe_eventos_do_logger_e_o_llm_trace(tmp_path, monkeypatch):
    # Duas mensagens: a 1ª (idade) é respondida por template (a vitrine de planos) e a 2ª é uma
    # dúvida sobre o produto, que é o que ainda chama o Responder — pergunta seca de campo virou
    # template (template-first), então não serve mais para exercitar o trace do responder.
    client, manager = _app(
        tmp_path,
        monkeypatch,
        [
            FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, idade=35)),
            FakeRun(content=Extraction(intent=Intent.DUVIDA_PRODUTO)),
        ],
        [FakeRun(content="O Completo cobre vidros, sim.")],
    )
    sid = _sid(client)
    client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi, tenho 35 anos"})
    client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "o completo cobre vidros?"})

    eventos = manager.sessao(sid).bus.historico()
    kinds = [e["event"] for e in eventos]
    for esperado in ("inbound", "extraction", "decision", "llm_call", "outbound", "llm_trace"):
        assert esperado in kinds, kinds

    traces = [e for e in eventos if e["event"] == "llm_trace"]
    assert [t["data"]["papel"] for t in traces] == ["extractor", "extractor", "responder"]
    assert [t["conversation_id"] for t in traces] == [sid, sid, sid]
    assert traces[0]["data"]["session_id"] == f"extract-{sid}"   # o agno do Extractor usa sessão à parte
    trace = traces[0]["data"]
    assert trace["status"] == "ok" and trace["tentativa"] == 1
    assert "Você extrai dados de UMA mensagem" in trace["instructions"]
    assert trace["entrada"] == "oi, tenho 35 anos"
    assert trace["saida"]["idade"] == 35
    # o trace vai cru (é para ler o prompt), mas o evento do logger continua mascarado
    assert traces[2]["data"]["instructions"].startswith("Você é a Lia, consultora de vendas")


def test_evento_do_logger_vai_para_o_arquivo_em_logs_studio(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    sid = _sid(client)
    client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi"})

    arquivo = tmp_path / "studio" / f"{sid}.jsonl"
    assert arquivo.exists()
    linhas = [json.loads(linha) for linha in arquivo.read_text(encoding="utf-8").splitlines()]
    assert next(e["event"] for e in linhas) == "inbound"
    assert not [e for e in linhas if e["event"] == "llm_trace"]   # trace NUNCA vai para o JSONL


def test_trace_de_falha_do_llm_aparece_no_barramento(tmp_path, monkeypatch):
    client, manager = _app(
        tmp_path, monkeypatch, extracoes=[run_erro(ERRO_429)] * 8, respostas=[FakeRun(content="ok")]
    )
    sid = _sid(client)
    client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi"})

    traces = [e["data"] for e in manager.sessao(sid).bus.historico() if e["event"] == "llm_trace"]
    assert [t["status"] for t in traces][-1] == "fallback"
    assert traces[0]["status"] == "erro" and "RESOURCE_EXHAUSTED" in traces[0]["erro"]


def test_sse_devolve_um_json_por_evento_e_fecha_no_delete(tmp_path, monkeypatch):
    """O stream replica o histórico, bate heartbeat e termina quando a sessão é encerrada."""
    import threading
    import time

    monkeypatch.setattr(lab_mod, "HEARTBEAT_S", 0.05)
    client, _ = _app(tmp_path, monkeypatch)
    sid = _sid(client)
    client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "oi, tenho 35 anos"})

    def encerrar_depois() -> None:
        time.sleep(0.3)
        client.delete(f"/api/lab/sessions/{sid}")

    tarefa = threading.Thread(target=encerrar_depois)
    tarefa.start()

    recebidos: list[dict] = []
    pings = 0
    with client.stream("GET", f"/api/lab/sessions/{sid}/events") as resposta:
        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/event-stream")
        for linha in resposta.iter_lines():
            if linha.startswith("data: "):
                recebidos.append(json.loads(linha[len("data: "):]))
            elif linha.startswith(": ping"):
                pings += 1
    tarefa.join()

    assert next(e["event"] for e in recebidos) == "inbound"
    assert {"extraction", "decision", "outbound", "llm_trace"} <= {e["event"] for e in recebidos}
    assert all(e["conversation_id"] == sid for e in recebidos)
    assert pings >= 1


# --------------------------------------------------------------------------- ciclo de vida
def test_sessao_desconhecida_devolve_404(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    assert client.post("/api/lab/sessions/nao-existe/messages", json={"text": "oi"}).status_code == 404
    assert client.get("/api/lab/sessions/nao-existe/state").status_code == 404
    assert client.delete("/api/lab/sessions/nao-existe").status_code == 404


def test_delete_encerra_a_sessao(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, monkeypatch)
    sid = _sid(client)
    assert client.delete(f"/api/lab/sessions/{sid}").json() == {"id": sid, "closed": True}
    assert client.get(f"/api/lab/sessions/{sid}/state").status_code == 404


def test_falha_de_boot_vira_400(tmp_path, monkeypatch):
    async def montar_quebrado(**_kw):
        raise RuntimeError("GOOGLE_API_KEY não configurada")

    monkeypatch.setattr(lab_mod, "_log_dir", lambda: tmp_path / "studio")
    app = FastAPI()
    app.include_router(router(LabManager(montar_quebrado)))
    resposta = TestClient(app).post("/api/lab/sessions", json={})
    assert resposta.status_code == 400
    assert "GOOGLE_API_KEY" in resposta.json()["detail"]


def test_router_pronto_usa_a_fabrica_do_app_state(tmp_path, monkeypatch):
    """Forma que o `build_studio_app` usa: `app.include_router(router)` sem manager explícito."""
    monkeypatch.setattr(lab_mod, "_log_dir", lambda: tmp_path / "studio")
    app = FastAPI()
    app.state.conversation_factory = _fabrica(
        tmp_path,
        [FakeRun(content=Extraction(intent=Intent.FORNECER_DADOS, idade=35))],
        [FakeRun(content="Qual o ano do carro?")],
    )
    app.include_router(router)
    client = TestClient(app)

    sid = _sid(client)
    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "tenho 35"}).json()
    assert corpo["state"]["idade"] == 35


# --------------------------------------------------------------------------- unidades
@pytest.mark.asyncio
async def test_uma_mensagem_por_vez_por_sessao(tmp_path, monkeypatch):
    """O lock da sessão serializa os turnos: nada de dois `handle` concorrentes."""
    import asyncio

    monkeypatch.setattr(lab_mod, "_log_dir", lambda: tmp_path / "studio")
    manager = LabManager(
        _fabrica(
            tmp_path,
            [FakeRun(content=Extraction(idade=35)), FakeRun(content=Extraction(veiculo_ano=2019))],
            [FakeRun(content="a"), FakeRun(content="b")],
        )
    )
    sessao = await manager.create(None)

    em_voo = {"n": 0, "max": 0}
    handle_real = sessao.conversation.handle

    async def handle_espiao(inbound, emit):
        em_voo["n"] += 1
        em_voo["max"] = max(em_voo["max"], em_voo["n"])
        await asyncio.sleep(0)
        try:
            return await handle_real(inbound, emit)
        finally:
            em_voo["n"] -= 1

    sessao.conversation.handle = handle_espiao
    await asyncio.gather(manager.send(sessao.id, "tenho 35"), manager.send(sessao.id, "Onix 2019"))

    assert em_voo["max"] == 1
    assert sessao.turnos == 2


def test_event_bus_guarda_historico_limitado_e_entrega_aos_assinantes():
    bus = EventBus("c1", maxlen=3)
    for n in range(5):
        bus.publish({"event": "x", "n": n})
    assert [e["n"] for e in bus.historico()] == [2, 3, 4]

    fila = bus.subscribe()
    bus.publish({"event": "y", "n": 9})
    assert fila.get_nowait()["n"] == 9

    bus.unsubscribe(fila)
    bus.publish({"event": "z", "n": 10})
    assert fila.empty()


def test_resumir_state_mascara_pii_e_nao_inventa_preco():
    from agent.models import LeadState, Stage

    estado = LeadState(
        conversation_id="lab-1",
        stage=Stage.COLETA_CEP,
        idade=35,
        cep="01310100",
        cep_info=CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"),
    )
    resumo = resumir_state(estado)

    assert resumo["origem"] is None
    assert resumo["plano_perguntado"] is False and resumo["plano_assumido"] is False
    assert resumo["cep"] == "01310-***"
    assert resumo["cep_cidade"] == "São Paulo"
    assert resumo["cotacao"] is None
    assert resumir_state(None) == {}


# --------------------------------------------------------------------------- handoff simulado (F9)
def test_handoff_no_lab_e_simulado_e_aparece_nos_eventos(tmp_path, monkeypatch):
    """Conversa de teste não manda WhatsApp para o consultor nem marca takeover — só mostra."""
    import dataclasses

    from agent import handoff as handoff_mod
    from agent import runtime_config
    from agent.config import settings as settings_reais
    from agent.runtime_config import ConfigStore

    # Config isolada: o notificador do Lab lê o store singleton (que aponta para o `config/` do
    # repo, vivo enquanto o operador mexe no Studio). Aqui ele lê um store de `tmp_path`, e o
    # consultor é forçado — assim o teste vale igual em qualquer máquina.
    monkeypatch.setattr(
        runtime_config, "settings", dataclasses.replace(settings_reais, consultor_number="5511977770000")
    )
    loja = ConfigStore(tmp_path / "config")
    loja.ensure_files()
    monkeypatch.setattr(handoff_mod, "config_store", loja)
    # policy e presenter também leem o singleton: com o Studio do operador vivo, o `config/` do
    # repo muda debaixo do teste (já vi este teste piscar por isso).
    monkeypatch.setattr("agent.policy.store", loja)
    monkeypatch.setattr("agent.presenter.store", loja)
    client, manager = _app(
        tmp_path,
        monkeypatch,
        [FakeRun(content=Extraction(intent=Intent.PEDIR_HUMANO))],
        [FakeRun(content="ok")],
    )
    sid = _sid(client)
    corpo = client.post(f"/api/lab/sessions/{sid}/messages", json={"text": "quero falar com uma pessoa"}).json()

    assert corpo["state"]["stage"] == "handoff"
    eventos = manager.sessao(sid).bus.historico()
    avisos = {e["data"]["canal"]: e["data"] for e in eventos if e["event"] == "handoff_notice"}
    assert set(avisos) == {"takeover", "whatsapp", "webhook"}
    assert avisos["takeover"]["status"] == "simulado" and avisos["takeover"]["destino"] == sid
    assert avisos["whatsapp"]["status"] == "simulado"
    assert "Lead para assumir" in avisos["whatsapp"]["texto"]      # o que teria ido ao consultor
    assert avisos["webhook"]["status"] == "desligado"      # sem `webhook_url`, nem chega a simular

    # e o arquivo de takeover do repo continua intocado (o Lab nunca assume `lab-*`)
    from agent.runtime_config import CONFIG_DIR
    from agent.takeover import TakeoverStore

    assert TakeoverStore(CONFIG_DIR).is_humano(sid) is False

