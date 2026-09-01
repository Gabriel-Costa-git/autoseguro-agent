import json
from pathlib import Path

from agent.models import CepInfo
from agent.observability import ConversationLogger


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(linha) for linha in path.read_text(encoding="utf-8").splitlines()]


def test_event_escreve_e_le_jsonl(tmp_path):
    logger = ConversationLogger(tmp_path, "conv-1")
    logger.event("inbound", message_id="m1", texto="oi")

    linhas = _read_lines(tmp_path / "conv-1.jsonl")
    assert len(linhas) == 1
    linha = linhas[0]
    assert linha["conversation_id"] == "conv-1"
    assert linha["event"] == "inbound"
    assert linha["message_id"] == "m1"
    assert linha["quote_id"] is None
    assert linha["data"] == {"texto": "oi"}
    assert "ts" in linha


def test_event_cria_diretorio_se_nao_existir(tmp_path):
    log_dir = tmp_path / "logs" / "aninhado"
    logger = ConversationLogger(log_dir, "conv-2")
    logger.event("outbound", texto="oi de volta")

    assert (log_dir / "conv-2.jsonl").exists()


def test_event_mascara_pii_dentro_de_data(tmp_path):
    logger = ConversationLogger(tmp_path, "conv-3")
    logger.event("extraction", observacao="cpf 123.456.789-01, email a@b.com")

    linha = _read_lines(tmp_path / "conv-3.jsonl")[0]
    assert "123.456.789-01" not in linha["data"]["observacao"]
    assert "***.***.***-**" in linha["data"]["observacao"]
    assert "***@b.com" in linha["data"]["observacao"]


def test_event_aceita_modelo_pydantic(tmp_path):
    logger = ConversationLogger(tmp_path, "conv-4")
    cep_info = CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP")
    logger.event("cep_lookup", quote_id="q1", resultado=cep_info)

    linha = _read_lines(tmp_path / "conv-4.jsonl")[0]
    assert linha["quote_id"] == "q1"
    assert linha["data"]["resultado"]["cidade"] == "São Paulo"
    assert linha["data"]["resultado"]["uf"] == "SP"


def test_event_append_grava_multiplas_linhas(tmp_path):
    logger = ConversationLogger(tmp_path, "conv-5")
    logger.event("inbound", message_id="m1")
    logger.event("outbound", message_id="m2")

    linhas = _read_lines(tmp_path / "conv-5.jsonl")
    assert len(linhas) == 2
    assert [l["message_id"] for l in linhas] == ["m1", "m2"]
