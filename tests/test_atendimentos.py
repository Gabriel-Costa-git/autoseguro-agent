"""Testes do catálogo de conversas lido dos JSONL (`agent/atendimentos.py`).

Tudo em `tmp_path`: os arquivos são montados com linhas no formato real do
`ConversationLogger` (as de `LINHAS_ENTREGA` são cópias literais de
`logs/entrega/caminho-feliz.jsonl`, para o parser ser exercitado contra o que o
agente de fato grava).
"""
from __future__ import annotations

import json

import pytest

from agent.atendimentos import Catalogo
from agent.takeover import TakeoverStore

# Linhas copiadas de `logs/entrega/caminho-feliz.jsonl` (formato entregue, sem `data.origem`).
LINHAS_ENTREGA = [
    (
        '{"ts": "2026-09-01T23:39:15.208505+00:00", "conversation_id": "demo-feliz-v3", "event": "inbound",'
        ' "message_id": "m1", "quote_id": null, "data": {"text": "Oi, queria fazer um seguro pro meu carro",'
        ' "media_type": "text", "sender_name": null}}'
    ),
    (
        '{"ts": "2026-09-01T23:39:16.420544+00:00", "conversation_id": "demo-feliz-v3", "event": "decision",'
        ' "message_id": "m1", "quote_id": null, "data": {"stage": "coleta_idade", "actions": ["ask_field"]}}'
    ),
    (
        '{"ts": "2026-09-01T23:39:48.731329+00:00", "conversation_id": "demo-feliz-v3", "event": "outbound",'
        ' "message_id": "m1-o1", "quote_id": null, "data": {"text": "Pra te cotar direitinho: quantos anos'
        ' você tem?", "source": "llm"}}'
    ),
]


def _linha(ts: str, event: str, message_id: str = "m1", **data) -> str:
    return json.dumps(
        {"ts": ts, "conversation_id": "x", "event": event, "message_id": message_id,
         "quote_id": None, "data": data},
        ensure_ascii=False,
    )


def _escrever(dir_, cid: str, linhas: list[str]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{cid}.jsonl").write_text("\n".join(linhas) + "\n", encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    """`logs/` com três conversas (whatsapp, cli, lab) + uma pasta `entrega/` para ignorar."""
    raiz = tmp_path / "logs"
    _escrever(
        raiz,
        "wa-5511999990000",
        [
            _linha("2026-09-02T10:00:00+00:00", "inbound", text="Oi, quero cotar",
                   media_type="text", sender_name="Ana Souza", origem="whatsapp:corretora"),
            _linha("2026-09-02T10:00:01+00:00", "decision", stage="coleta_idade", actions=["ask_field"]),
            _linha("2026-09-02T10:00:02+00:00", "outbound", text="Quantos anos você tem?", source="llm"),
            _linha("2026-09-02T10:00:30+00:00", "inbound", message_id="m2", text="35",
                   media_type="text", sender_name="Ana Souza", origem="whatsapp:corretora"),
        ],
    )
    _escrever(
        raiz,
        "cli-1788350261",
        [
            _linha("2026-09-01T09:00:00+00:00", "inbound", text="oi", media_type="text", sender_name=None),
            _linha("2026-09-01T09:00:01+00:00", "decision", stage="handoff", actions=["handoff"]),
            _linha("2026-09-01T09:00:02+00:00", "handoff", reason="lead_pediu_humano", payload={}),
        ],
    )
    _escrever(
        raiz / "studio",
        "lab-abc12345",
        [
            _linha("2026-09-03T08:00:00+00:00", "inbound", text="teste do lab",
                   media_type="text", origem="lab"),
            _linha("2026-09-03T08:00:01+00:00", "decision", stage="coleta_idade", actions=["ask_field"]),
        ],
    )
    _escrever(raiz / "entrega", "demo-feliz-v3", LINHAS_ENTREGA)
    return raiz


def _catalogo(logs, takeover=None) -> Catalogo:
    return Catalogo(logs, takeover=takeover)


# --------------------------------------------------------------------------- listagem
def test_listar_ordena_por_ultimo_ts_desc_e_ignora_entrega(logs):
    itens = _catalogo(logs).listar()
    assert [i["conversation_id"] for i in itens] == ["lab-abc12345", "wa-5511999990000", "cli-1788350261"]


def test_resumo_traz_os_campos_do_contrato(logs):
    item = next(i for i in _catalogo(logs).listar() if i["conversation_id"] == "wa-5511999990000")
    assert set(item) == {
        "conversation_id", "origem", "nome", "inicio", "ultimo_ts", "ultima_msg",
        "turnos", "stage", "status", "handoff_reason",
    }
    assert item["origem"] == "whatsapp:corretora"
    assert item["nome"] == "Ana Souza"
    assert item["inicio"] == "2026-09-02T10:00:00+00:00"
    assert item["ultimo_ts"] == "2026-09-02T10:00:30+00:00"
    assert item["ultima_msg"] == "35"       # último inbound/outbound com texto
    assert item["turnos"] == 2
    assert item["stage"] == "coleta_idade"
    assert item["status"] == "agente"
    assert item["handoff_reason"] is None


def test_origem_inferida_do_prefixo_quando_o_log_e_antigo(logs):
    itens = {i["conversation_id"]: i for i in _catalogo(logs).listar()}
    assert itens["cli-1788350261"]["origem"] == "cli"        # log sem `data.origem`
    assert itens["lab-abc12345"]["origem"] == "lab"          # log novo, campo explícito


def test_status_encerrado_por_handoff_e_por_stage_terminal(logs):
    itens = {i["conversation_id"]: i for i in _catalogo(logs).listar()}
    assert itens["cli-1788350261"]["status"] == "encerrado"
    assert itens["cli-1788350261"]["handoff_reason"] == "lead_pediu_humano"


def test_status_encerrado_por_refusal(tmp_path):
    _escrever(tmp_path, "wa-1", [
        _linha("2026-09-02T10:00:00+00:00", "inbound", text="oi", media_type="text"),
        _linha("2026-09-02T10:00:01+00:00", "refusal", motivo="idade fora da faixa"),
    ])
    assert _catalogo(tmp_path).listar()[0]["status"] == "encerrado"


def test_status_humano_vem_do_takeover(logs, tmp_path):
    takeover = TakeoverStore(tmp_path / "config")
    catalogo = _catalogo(logs, takeover)
    assert catalogo.resumo("wa-5511999990000")["status"] == "agente"

    takeover.assumir("wa-5511999990000")
    assert catalogo.resumo("wa-5511999990000")["status"] == "humano"

    # takeover manda até numa conversa já encerrada (o operador está com ela na mão)
    takeover.assumir("cli-1788350261")
    assert catalogo.resumo("cli-1788350261")["status"] == "humano"

    takeover.devolver("wa-5511999990000")
    assert catalogo.resumo("wa-5511999990000")["status"] == "agente"


# --------------------------------------------------------------------------- filtros
def test_filtro_por_origem_exata_e_por_canal(logs):
    catalogo = _catalogo(logs)
    assert [i["conversation_id"] for i in catalogo.listar(origem="whatsapp:corretora")] == ["wa-5511999990000"]
    assert [i["conversation_id"] for i in catalogo.listar(origem="whatsapp")] == ["wa-5511999990000"]
    assert [i["conversation_id"] for i in catalogo.listar(origem="lab")] == ["lab-abc12345"]
    assert catalogo.listar(origem="whatsapp:outra") == []


def test_filtro_por_status(logs, tmp_path):
    takeover = TakeoverStore(tmp_path / "config")
    takeover.assumir("lab-abc12345")
    catalogo = _catalogo(logs, takeover)
    assert [i["conversation_id"] for i in catalogo.listar(status="humano")] == ["lab-abc12345"]
    assert [i["conversation_id"] for i in catalogo.listar(status="encerrado")] == ["cli-1788350261"]
    assert [i["conversation_id"] for i in catalogo.listar(status="agente")] == ["wa-5511999990000"]


def test_busca_por_id_nome_e_texto(logs):
    catalogo = _catalogo(logs)
    assert [i["conversation_id"] for i in catalogo.listar(q="5511999")] == ["wa-5511999990000"]
    assert [i["conversation_id"] for i in catalogo.listar(q="ana souza")] == ["wa-5511999990000"]
    assert [i["conversation_id"] for i in catalogo.listar(q="teste do lab")] == ["lab-abc12345"]
    assert catalogo.listar(q="nada com isso") == []


# --------------------------------------------------------------------------- transcrição
def test_transcricao_e_since(logs):
    catalogo = _catalogo(logs)
    tudo = catalogo.transcricao("wa-5511999990000")
    assert tudo["total"] == 4
    assert len(tudo["eventos"]) == 4
    assert tudo["resumo"]["conversation_id"] == "wa-5511999990000"

    resto = catalogo.transcricao("wa-5511999990000", since=tudo["total"])
    assert resto["eventos"] == [] and resto["total"] == 4

    parcial = catalogo.transcricao("wa-5511999990000", since=3)
    assert [e["event"] for e in parcial["eventos"]] == ["inbound"]
    assert parcial["eventos"][0]["message_id"] == "m2"


def test_conversa_do_lab_entra_pela_pasta_studio(logs):
    assert _catalogo(logs).transcricao("lab-abc12345")["total"] == 2


def test_cid_desconhecido_e_travessia_de_caminho_levantam_keyerror(logs):
    catalogo = _catalogo(logs)
    with pytest.raises(KeyError):
        catalogo.transcricao("nao-existe")
    with pytest.raises(KeyError):
        catalogo.resumo("../entrega/demo-feliz-v3")


def test_entrega_nao_e_alcancavel_nem_por_id(logs):
    with pytest.raises(KeyError):
        _catalogo(logs).resumo("demo-feliz-v3")


# --------------------------------------------------------------------------- cache
def test_cache_invalida_quando_o_arquivo_cresce(logs):
    catalogo = _catalogo(logs)
    assert catalogo.transcricao("wa-5511999990000")["total"] == 4

    with (logs / "wa-5511999990000.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(_linha("2026-09-02T10:01:00+00:00", "outbound", text="Perfeito!", source="humano") + "\n")

    depois = catalogo.transcricao("wa-5511999990000")
    assert depois["total"] == 5
    assert depois["resumo"]["ultima_msg"] == "Perfeito!"
    assert depois["resumo"]["ultimo_ts"] == "2026-09-02T10:01:00+00:00"


def test_arquivo_novo_aparece_na_listagem(logs):
    catalogo = _catalogo(logs)
    assert len(catalogo.listar()) == 3
    _escrever(logs, "wa-999", [_linha("2026-09-04T10:00:00+00:00", "inbound", text="oi", media_type="text")])
    assert next(i["conversation_id"] for i in catalogo.listar()) == "wa-999"


# --------------------------------------------------------------------------- robustez
def test_linha_invalida_e_ignorada_sem_derrubar(tmp_path):
    (tmp_path / "wa-2.jsonl").write_text(
        _linha("2026-09-02T10:00:00+00:00", "inbound", text="oi", media_type="text") + "\n"
        + '{"ts": "2026-09-02T10:00:01+00:00", "event": "outbo\n'      # linha truncada
        + _linha("2026-09-02T10:00:02+00:00", "outbound", text="olá!", source="llm") + "\n",
        encoding="utf-8",
    )
    resumo = _catalogo(tmp_path).resumo("wa-2")
    assert resumo["ultima_msg"] == "olá!"
    assert resumo["turnos"] == 1


def test_log_dir_inexistente_devolve_lista_vazia(tmp_path):
    assert Catalogo(tmp_path / "nao-existe").listar() == []


def test_linhas_da_entrega_sao_parseadas(tmp_path):
    """As linhas reais do agente entregue (sem `origem`) viram um resumo completo."""
    _escrever(tmp_path, "demo-feliz-v3", LINHAS_ENTREGA)
    resumo = _catalogo(tmp_path).resumo("demo-feliz-v3")
    assert resumo["origem"] is None            # id sem prefixo conhecido
    assert resumo["nome"] is None
    assert resumo["turnos"] == 1
    assert resumo["stage"] == "coleta_idade"
    assert resumo["ultima_msg"] == "Pra te cotar direitinho: quantos anos você tem?"
    assert resumo["status"] == "agente"


def test_conversa_com_handoff_e_takeover_fica_humana_sem_perder_o_motivo(tmp_path):
    """F9: o handoff assume a conversa sozinho — em Atendimentos ela some do balde do agente."""
    _escrever(tmp_path, "wa-1", [
        _linha("2026-09-03T10:00:00+00:00", "inbound", text="quero falar com uma pessoa",
               media_type="text", origem="whatsapp:corretora"),
        _linha("2026-09-03T10:00:01+00:00", "decision", stage="handoff", actions=["handoff"]),
        _linha("2026-09-03T10:00:02+00:00", "handoff", reason="lead_pediu_humano", payload={}),
        _linha("2026-09-03T10:00:03+00:00", "handoff_notice", canal="takeover", status="ok", destino="wa-1"),
        _linha("2026-09-03T10:00:03+00:00", "handoff_notice", canal="whatsapp", status="ok",
               destino="+55 ** *****-****"),
    ])
    takeover = TakeoverStore(tmp_path / "config")
    takeover.assumir("wa-1")                       # é o que o notificador faz no turno
    catalogo = _catalogo(tmp_path, takeover)

    resumo = catalogo.resumo("wa-1")
    assert resumo["status"] == "humano"            # takeover vence o "encerrado" do handoff
    assert resumo["handoff_reason"] == "lead_pediu_humano"
    assert [i["conversation_id"] for i in catalogo.listar(status="humano")] == ["wa-1"]

    eventos = catalogo.transcricao("wa-1")["eventos"]
    canais = [e["data"]["canal"] for e in eventos if e["event"] == "handoff_notice"]
    assert canais == ["takeover", "whatsapp"]      # a transcrição mostra o que foi avisado

