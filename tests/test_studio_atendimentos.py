"""Testes das rotas de Atendimentos do Studio (`agent/studio/atendimentos_api.py`).

O app é o real, com tudo injetado por `app.state`: `ConfigStore(tmp_path)` (logo o
takeover grava em `tmp_path/atendimentos.json`), um `Catalogo` apontado para logs de
mentira em `tmp_path` e um `EvolutionSender` falso — nada de rede nem de `config/` e
`logs/` do repo.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.atendimentos import Catalogo
from agent.runtime_config import ConfigStore
from agent.studio import atendimentos_api as api_mod
from agent.studio.app import build_studio_app

WA = "wa-5511999990000"


class FakeSender:
    """Registra o que seria enviado pela Evolution.

    `entregue` imita o contrato novo do `EvolutionSender`: `False` é recusa (número
    inválido, instância desconectada) e `None` é o sender antigo, que não devolvia nada.
    """

    def __init__(self, entregue: bool | None = None) -> None:
        self.enviadas: list[tuple[str, str]] = []
        self.entregue = entregue

    async def send_text(self, number: str, text: str) -> bool | None:
        self.enviadas.append((number, text))
        return self.entregue


async def _fake_conversation_factory():
    raise AssertionError("as rotas de atendimentos não montam conversa")


def _linha(ts: str, event: str, message_id: str = "m1", cid: str = WA, **data) -> str:
    """Formato do `ConversationLogger`: o `conversation_id` de dentro é o id de verdade
    (o nome do arquivo pode ser o hasheado, ver `pii.nome_arquivo_log`)."""
    return json.dumps(
        {"ts": ts, "conversation_id": cid, "event": event, "message_id": message_id,
         "quote_id": None, "data": data},
        ensure_ascii=False,
    )


@pytest.fixture
def studio(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"{WA}.jsonl").write_text(
        "\n".join([
            _linha("2026-09-02T10:00:00+00:00", "inbound", text="Oi, quero cotar", media_type="text",
                   sender_name="Ana", origem="whatsapp:corretora"),
            _linha("2026-09-02T10:00:01+00:00", "decision", stage="coleta_idade", actions=["ask_field"]),
            _linha("2026-09-02T10:00:02+00:00", "outbound", text="Quantos anos você tem?", source="llm"),
        ]) + "\n",
        encoding="utf-8",
    )
    (logs / "cli-1.jsonl").write_text(
        _linha("2026-09-01T09:00:00+00:00", "inbound", cid="cli-1", text="oi", media_type="text") + "\n",
        encoding="utf-8",
    )

    app = build_studio_app(store=ConfigStore(tmp_path / "config"), conversation_factory=_fake_conversation_factory)
    app.state.catalogo = Catalogo(logs, takeover=app.state.takeover)
    app.state.evolution_sender = FakeSender()
    return TestClient(app), logs


# --------------------------------------------------------------------------- listagem / detalhe
def test_get_atendimentos_lista_ordenada(studio):
    client, _ = studio
    resp = client.get("/api/atendimentos")
    assert resp.status_code == 200
    itens = resp.json()["itens"]
    assert [i["conversation_id"] for i in itens] == [WA, "cli-1"]
    assert itens[0]["origem"] == "whatsapp:corretora"
    assert itens[0]["status"] == "agente"
    assert itens[0]["nome"] == "Ana"


def test_get_atendimentos_com_filtros(studio):
    """A busca cobre o que a lista mostra: id, nome e última mensagem."""
    client, _ = studio
    assert [i["conversation_id"] for i in client.get("/api/atendimentos?origem=cli").json()["itens"]] == ["cli-1"]
    assert [i["conversation_id"] for i in client.get("/api/atendimentos?q=ana").json()["itens"]] == [WA]
    assert [i["conversation_id"] for i in client.get("/api/atendimentos?q=quantos anos").json()["itens"]] == [WA]
    assert client.get("/api/atendimentos?status=humano").json()["itens"] == []


def test_get_transcricao_com_since(studio):
    client, _ = studio
    resp = client.get(f"/api/atendimentos/{WA}")
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["total"] == 3
    assert [e["event"] for e in corpo["eventos"]] == ["inbound", "decision", "outbound"]
    assert corpo["resumo"]["conversation_id"] == WA

    parcial = client.get(f"/api/atendimentos/{WA}?since=2").json()
    assert [e["event"] for e in parcial["eventos"]] == ["outbound"]
    assert parcial["total"] == 3


def test_transcricao_de_conversa_inexistente_404(studio):
    client, _ = studio
    resp = client.get("/api/atendimentos/wa-000")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# --------------------------------------------------------------------------- assumir / devolver
def test_assumir_e_devolver_mudam_o_status(studio, tmp_path):
    client, _ = studio

    resp = client.post(f"/api/atendimentos/{WA}/assumir")
    assert resp.status_code == 200
    assert resp.json()["status"] == "humano"
    assert json.loads((tmp_path / "config" / "atendimentos.json").read_text(encoding="utf-8"))[WA]["modo"] == "humano"
    assert client.get("/api/atendimentos").json()["itens"][0]["status"] == "humano"

    resp = client.post(f"/api/atendimentos/{WA}/devolver")
    assert resp.status_code == 200
    assert resp.json()["status"] == "agente"
    assert json.loads((tmp_path / "config" / "atendimentos.json").read_text(encoding="utf-8")) == {}


def test_assumir_conversa_inexistente_404(studio):
    client, _ = studio
    assert client.post("/api/atendimentos/wa-000/assumir").status_code == 404
    assert client.post("/api/atendimentos/wa-000/devolver").status_code == 404


# --------------------------------------------------------------------------- mensagens
def test_enviar_mensagem_humana(studio):
    client, _ = studio
    client.post(f"/api/atendimentos/{WA}/assumir")

    resp = client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "Aqui é a Ana da AutoSeguro, tudo bem?"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert client.app.state.evolution_sender.enviadas == [
        ("5511999990000", "Aqui é a Ana da AutoSeguro, tudo bem?")
    ]

    eventos = client.get(f"/api/atendimentos/{WA}").json()["eventos"]
    assert eventos[-1]["event"] == "outbound"
    assert eventos[-1]["data"] == {"text": "Aqui é a Ana da AutoSeguro, tudo bem?", "source": "humano"}
    assert eventos[-1]["message_id"]
    assert client.get("/api/atendimentos").json()["itens"][0]["ultima_msg"] == "Aqui é a Ana da AutoSeguro, tudo bem?"


def test_enviar_sem_assumir_400(studio):
    client, _ = studio
    resp = client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "oi"})
    assert resp.status_code == 400
    assert "assuma" in resp.json()["detail"]
    assert client.app.state.evolution_sender.enviadas == []


def test_enviar_em_conversa_que_nao_e_whatsapp_400(studio):
    client, _ = studio
    client.post("/api/atendimentos/cli-1/assumir")
    resp = client.post("/api/atendimentos/cli-1/mensagens", json={"text": "oi"})
    assert resp.status_code == 400
    assert client.app.state.evolution_sender.enviadas == []


def test_enviar_texto_vazio_400(studio):
    client, _ = studio
    client.post(f"/api/atendimentos/{WA}/assumir")
    assert client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "   "}).status_code == 400
    assert client.post(f"/api/atendimentos/{WA}/mensagens", json={}).status_code == 400


def test_enviar_em_conversa_inexistente_404(studio):
    client, _ = studio
    assert client.post("/api/atendimentos/wa-000/mensagens", json={"text": "oi"}).status_code == 404


def test_sem_evolution_configurada_400(studio, monkeypatch):
    """Sem EVOLUTION_* no ambiente a rota falha limpo, em vez de fingir que enviou."""
    from types import SimpleNamespace

    client, _ = studio
    client.app.state.evolution_sender = None
    monkeypatch.setattr(
        api_mod, "settings", SimpleNamespace(evolution_url=None, evolution_apikey=None, evolution_instance=None)
    )
    client.post(f"/api/atendimentos/{WA}/assumir")
    resp = client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "oi"})
    assert resp.status_code == 400
    assert "Evolution" in resp.json()["detail"]


# --------------------------------------------------------------------------- envio que falhou
def test_envio_recusado_pela_evolution_vira_502_e_nao_entra_no_log(studio):
    """Gravar `outbound source="humano"` de mensagem que não saiu é a mesma mentira que a
    F9 corrigiu no aviso ao consultor: o operador acharia que respondeu."""
    client, _ = studio
    client.app.state.evolution_sender = FakeSender(entregue=False)
    client.post(f"/api/atendimentos/{WA}/assumir")

    resp = client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "Oi, aqui é a Ana"})

    assert resp.status_code == 502
    assert "não foi entregue" in resp.json()["detail"].lower()
    eventos = client.get(f"/api/atendimentos/{WA}").json()["eventos"]
    assert [e["event"] for e in eventos if e["event"] == "outbound" and e["data"].get("source") == "humano"] == []


def test_envio_confirmado_vira_outbound_humano(studio):
    client, _ = studio
    client.app.state.evolution_sender = FakeSender(entregue=True)
    client.post(f"/api/atendimentos/{WA}/assumir")

    assert client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "oi"}).status_code == 200
    eventos = client.get(f"/api/atendimentos/{WA}").json()["eventos"]
    assert eventos[-1]["data"] == {"text": "oi", "source": "humano"}


def test_mensagem_do_operador_reinicia_o_relogio_da_devolucao_automatica(studio):
    """Sem isto, uma conversa bem atendida pelo painel volta ao agente 4 h depois do handoff."""
    client, _ = studio
    takeover = client.app.state.takeover
    client.post(f"/api/atendimentos/{WA}/assumir")
    assert takeover.listar()[WA]["ultima_humana"] is None

    client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "já te respondo"})

    assert takeover.listar()[WA]["ultima_humana"] is not None


def test_envio_recusado_nao_reinicia_o_relogio(studio):
    client, _ = studio
    client.app.state.evolution_sender = FakeSender(entregue=False)
    takeover = client.app.state.takeover
    client.post(f"/api/atendimentos/{WA}/assumir")

    client.post(f"/api/atendimentos/{WA}/mensagens", json={"text": "oi"})

    assert takeover.listar()[WA]["ultima_humana"] is None
