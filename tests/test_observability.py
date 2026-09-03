import json
from pathlib import Path

from agent.models import CepInfo
from agent.observability import ConversationLogger
from agent.pii import nome_arquivo_log


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


# --------------------------------------------------------------------------- nome do arquivo (PII)
def test_conversa_de_whatsapp_nao_leva_o_telefone_para_o_nome_do_arquivo(tmp_path):
    logger = ConversationLogger(tmp_path, "wa-5511999990000")
    logger.event("inbound", message_id="m1", text="oi")

    nomes = [p.name for p in tmp_path.iterdir()]
    assert nomes == [f"{nome_arquivo_log('wa-5511999990000')}.jsonl"]
    assert "5511999990000" not in nomes[0]
    # o id INTERNO não muda: é o que o takeover e o Atendimentos usam
    assert _read_lines(logger.path)[0]["conversation_id"] == "wa-5511999990000"


def test_arquivo_antigo_com_o_numero_em_claro_continua_sendo_o_da_conversa(tmp_path):
    """Nada de partir uma conversa em dois arquivos por causa da mudança de nome."""
    legado = tmp_path / "wa-5511999990000.jsonl"
    legado.write_text('{"event": "inbound"}\n', encoding="utf-8")

    ConversationLogger(tmp_path, "wa-5511999990000").event("outbound", text="oi de volta")

    assert len(_read_lines(legado)) == 2
    assert not (tmp_path / f"{nome_arquivo_log('wa-5511999990000')}.jsonl").exists()


def test_nome_de_arquivo_explicito_manda(tmp_path):
    """Quem já tem o nome hasheado (o canal, um exportador) passa pronto."""
    logger = ConversationLogger(tmp_path, "wa-5511999990000", "wa-fixo")
    logger.event("inbound")
    assert logger.path == tmp_path / "wa-fixo.jsonl"


def test_cli_e_lab_continuam_com_o_nome_de_sempre(tmp_path):
    ConversationLogger(tmp_path, "cli-1788350261").event("inbound")
    assert (tmp_path / "cli-1788350261.jsonl").exists()


# --------------------------------------------------------------------------- campos livres
def test_event_grava_qualquer_campo_inclusive_in_reply_to(tmp_path):
    """`in_reply_to` é o campo que faltava no `outbound` (README §5 promete e o log não tinha).

    O logger nunca filtrou campo nenhum — quem precisa mudar é o emissor. Ver o reporte:
    `conversation.py:376` passa a mandar `in_reply_to=out.in_reply_to`.
    """
    logger = ConversationLogger(tmp_path, "conv-6")
    logger.event("outbound", message_id="m1-o1", text="oi", source="template", in_reply_to="m1")

    linha = _read_lines(tmp_path / "conv-6.jsonl")[0]
    assert linha["data"] == {"text": "oi", "source": "template", "in_reply_to": "m1"}


def test_event_aceita_os_eventos_novos_do_kind(tmp_path):
    logger = ConversationLogger(tmp_path, "conv-7")
    for kind in ("llm_retry", "llm_error", "llm_guard", "planos_refresh",
                 "outbound_suprimido", "takeover_expirado", "extraction_regex"):
        logger.event(kind, motivo="x")
    assert [l["event"] for l in _read_lines(tmp_path / "conv-7.jsonl")] == [
        "llm_retry", "llm_error", "llm_guard", "planos_refresh",
        "outbound_suprimido", "takeover_expirado", "extraction_regex",
    ]
