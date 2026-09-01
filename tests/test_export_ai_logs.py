"""Testes do script de exportação/higienização dos transcripts do Claude Code.

Tudo isolado em `tmp_path` — nunca toca `~/.claude/projects` de verdade nem o
`ai-logs/` do repo: `export_logs`/`check_logs` recebem os diretórios direto,
sem passar pela descoberta automática de `main()`.
"""
from __future__ import annotations

from collections import Counter

from scripts.export_ai_logs import (
    _load_env_secrets,
    _scrub_line,
    check_logs,
    export_logs,
)

FAKE_GOOGLE_KEY = "AIza" + "x1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # 30+ chars após AIza


def _linha_jsonl(texto: str) -> str:
    return '{"role": "assistant", "content": "' + texto.replace('"', "'") + '"}\n'


# --------------------------------------------------------------------------- _load_env_secrets
def test_load_env_secrets_le_valores_ignora_comentarios_e_vazias(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comentário\nGOOGLE_API_KEY=segredo-super-longo-123\n\nEVOLUTION_APIKEY=outra-chave-longa\nPORT=3000\n",
        encoding="utf-8",
    )
    valores = _load_env_secrets(env)
    assert "segredo-super-longo-123" in valores
    assert "outra-chave-longa" in valores
    # PORT não tem nome de segredo nem tamanho de credencial: scrub cego destruiria os transcripts
    assert "3000" not in valores


def test_load_env_secrets_arquivo_ausente_devolve_vazio(tmp_path):
    assert _load_env_secrets(tmp_path / "nao-existe.env") == []


# --------------------------------------------------------------------------- _scrub_line (um padrão por vez)
def test_scrub_valor_do_env():
    counts = Counter()
    linha = _linha_jsonl("minha chave e segredo-super-longo-123")
    limpa = _scrub_line(linha, ["segredo-super-longo-123"], counts)
    assert "segredo-super-longo-123" not in limpa
    assert "<REDACTED>" in limpa
    assert counts["env_secrets"] == 1


def test_scrub_chave_google():
    counts = Counter()
    linha = _linha_jsonl(f"use a chave {FAKE_GOOGLE_KEY} no client")
    limpa = _scrub_line(linha, [], counts)
    assert FAKE_GOOGLE_KEY not in limpa
    assert "<REDACTED>" in limpa
    assert counts["google_api_key"] == 1


def test_scrub_padrao_apikey():
    counts = Counter()
    linha = _linha_jsonl('curl -H "apikey: abcdefgh12345678ijklmnop"')
    limpa = _scrub_line(linha, [], counts)
    assert "abcdefgh12345678ijklmnop" not in limpa
    assert "<REDACTED>" in limpa
    assert counts["apikey_pattern"] == 1


def test_scrub_email_via_mask_text():
    counts = Counter()
    linha = _linha_jsonl("contato: joao.silva@example.com")
    limpa = _scrub_line(linha, [], counts)
    assert "joao.silva@example.com" not in limpa
    assert "***@example.com" in limpa
    assert counts["email"] == 1


def test_scrub_linha_sem_segredo_fica_intacta():
    counts = Counter()
    linha = _linha_jsonl("nada de sensível aqui, so um R$ 209,90")
    limpa = _scrub_line(linha, [], counts)
    assert limpa == linha
    assert sum(counts.values()) == 0


# --------------------------------------------------------------------------- export_logs
def test_export_logs_copia_e_higieniza(tmp_path):
    origem = tmp_path / "-workspace-autoseguro-agent"
    origem.mkdir()
    (origem / "sessao1.jsonl").write_text(
        _linha_jsonl(f"chave: {FAKE_GOOGLE_KEY}") + _linha_jsonl("email: lead@cliente.com"),
        encoding="utf-8",
    )
    dest = tmp_path / "sessions"

    arquivos, bytes_total, counts = export_logs([origem], dest, segredos=[])

    assert arquivos == 1
    assert bytes_total > 0
    destino = dest / "-workspace-autoseguro-agent" / "sessao1.jsonl"
    assert destino.exists()
    conteudo = destino.read_text(encoding="utf-8")
    assert FAKE_GOOGLE_KEY not in conteudo
    assert "lead@cliente.com" not in conteudo
    assert counts["google_api_key"] == 1
    assert counts["email"] == 1


def test_export_logs_origem_ausente_nao_quebra(tmp_path):
    arquivos, bytes_total, counts = export_logs([tmp_path / "nao-existe"], tmp_path / "sessions", segredos=[])
    assert arquivos == 0
    assert bytes_total == 0
    assert not counts


def test_export_logs_e_idempotente(tmp_path):
    origem = tmp_path / "origem"
    origem.mkdir()
    (origem / "s.jsonl").write_text(_linha_jsonl(f"segredo {FAKE_GOOGLE_KEY} aqui"), encoding="utf-8")
    dest = tmp_path / "sessions"

    export_logs([origem], dest, segredos=[])
    conteudo_1 = (dest / "origem" / "s.jsonl").read_text(encoding="utf-8")

    export_logs([origem], dest, segredos=[])
    conteudo_2 = (dest / "origem" / "s.jsonl").read_text(encoding="utf-8")

    assert conteudo_1 == conteudo_2


# --------------------------------------------------------------------------- check_logs
def test_check_logs_detecta_segredo_e_falha(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    (ai_logs / "sessions" / "algum").mkdir(parents=True)
    (ai_logs / "sessions" / "algum" / "vazou.jsonl").write_text(
        _linha_jsonl(f"opa, {FAKE_GOOGLE_KEY} vazou aqui"), encoding="utf-8"
    )

    assert check_logs(ai_logs, segredos=[]) == 1


def test_check_logs_limpo_passa(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    (ai_logs / "sessions" / "algum").mkdir(parents=True)
    (ai_logs / "sessions" / "algum" / "ok.jsonl").write_text(_linha_jsonl("nada demais por aqui"), encoding="utf-8")

    assert check_logs(ai_logs, segredos=[]) == 0


def test_check_logs_detecta_valor_de_env(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    ai_logs.mkdir()
    (ai_logs / "relato.md").write_text("a chave usada foi segredo-vazado-123\n", encoding="utf-8")

    assert check_logs(ai_logs, segredos=["segredo-vazado-123"]) == 1


def test_check_logs_diretorio_ausente_passa(tmp_path):
    assert check_logs(tmp_path / "nao-existe", segredos=[]) == 0
