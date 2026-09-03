"""Testes do script de exportação/higienização dos transcripts do Claude Code.

Uma regra por teste, com fixtures minúsculas montadas em `tmp_path`. Nada aqui
toca as sessões reais do Claude Code nem o `ai-logs/` do repo: as funções
recebem diretórios, janela e listas por parâmetro, sem passar pela descoberta
automática de `main()`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from scripts import check_logs_pii
from scripts.export_ai_logs import (
    MARCA_BASE64,
    MARCA_DENYLIST,
    MARCA_IMAGEM,
    MARCA_PESSOAL,
    Higienizador,
    _dentro_da_janela,
    _load_env_secrets,
    _slug,
    carregar_denylist,
    check_logs,
    classificar,
    descobrir_workspaces,
    escrever_index,
    export_logs,
    globs_padrao,
    mapear_destinos,
    regras_de_caminho,
)

FAKE_GOOGLE_KEY = "AIza" + "x1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # 30+ chars após AIza
JANELA = (datetime.fromisoformat("2026-01-01T00:00:00+00:00"), datetime.fromisoformat("2026-01-03T00:00:00+00:00"))


def _hig(segredos=(), denylist=(), regras=(), pessoais=("/casa/.claude",)) -> Higienizador:
    return Higienizador(
        segredos=list(segredos),
        denylist=list(denylist),
        regras_caminho=list(regras),
        prefixos_pessoais=tuple(pessoais),
    )


def _linha(**campos) -> str:
    return json.dumps(campos, ensure_ascii=False) + "\n"


def _sessao(tmp_path, nome: str, linhas: list[str], ts: str = "2026-01-02T10:00:00.000Z"):
    """Sessão sintética com timestamp dentro da `JANELA` e linhas suficientes."""
    origem = tmp_path / nome
    origem.mkdir(parents=True, exist_ok=True)
    corpo = [_linha(type="user", timestamp=ts, texto=f"linha {i}") for i in range(10)] + linhas
    (origem / "sessao.jsonl").write_text("".join(corpo), encoding="utf-8")
    return origem


# --------------------------------------------------------------------------- segredos do .env
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


def test_load_env_secrets_ignora_endereco_local(tmp_path):
    """O falso positivo que fazia `--check` sair 1 em qualquer clone com `.env`."""
    env = tmp_path / ".env"
    env.write_text("QUOTE_API_URL=http://localhost:8000\nEVOLUTION_URL=http://127.0.0.1:8080\n", encoding="utf-8")
    assert _load_env_secrets(env) == []


def test_load_env_secrets_arquivo_ausente_devolve_vazio(tmp_path):
    assert _load_env_secrets(tmp_path / "nao-existe.env") == []


def test_check_passa_com_env_copiado_do_exemplo(tmp_path):
    """`cp .env.example .env` é o que o README manda fazer; o gate tem de continuar verde."""
    env = tmp_path / ".env"
    env.write_text("QUOTE_API_URL=http://localhost:8000\nLOG_DIR=logs\n", encoding="utf-8")
    ai_logs = tmp_path / "ai-logs"
    ai_logs.mkdir()
    (ai_logs / "nota.md").write_text("a API sobe em http://localhost:8000\n", encoding="utf-8")

    assert check_logs(ai_logs, _load_env_secrets(env)) == 0


# --------------------------------------------------------------------------- denylist
def test_carregar_denylist_ignora_comentario_e_ordena_do_maior(tmp_path):
    arq = tmp_path / "deny.txt"
    arq.write_text("# comentário\nexemplo.com\nsub.exemplo.com\n\n", encoding="utf-8")
    assert carregar_denylist(arq) == ["sub.exemplo.com", "exemplo.com"]


def test_carregar_denylist_ausente_devolve_vazio(tmp_path):
    assert carregar_denylist(tmp_path / "nao-existe.txt") == []


def test_denylist_redige_o_maior_literal_primeiro():
    hig = _hig(denylist=["sub.exemplo.com", "exemplo.com"])
    assert hig.texto("host sub.exemplo.com aqui") == f"host {MARCA_DENYLIST} aqui"


# --------------------------------------------------------------------------- caminhos
def test_regras_de_caminho_trocam_repo_worktree_scratch_e_home(tmp_path):
    from pathlib import Path

    repo = Path.home() / "espaco" / "projeto"
    hig = _hig(regras=regras_de_caminho(repo))
    texto = (
        f"veja {repo}/agent/policy.py, {repo.parent}/worktrees/fix-x/tests, "
        "/private/tmp/claude-99/abc/scratchpad e "
        f"{Path.home()}/outra-coisa"
    )
    limpo = hig.texto(texto)
    assert "<repo>/agent/policy.py" in limpo
    assert "<worktree>/tests" in limpo
    assert "<scratch>" in limpo
    assert "<home>/outra-coisa" in limpo
    assert str(Path.home()) not in limpo


# --------------------------------------------------------------------------- redações por conteúdo
def test_ancora_de_config_pessoal_apaga_o_campo_inteiro():
    hig = _hig()
    texto = "1\t# Modo X — protocolo de orquestração (Gabriel)\n2\tregra 1\n3\tregra 2\n"
    assert hig.texto(texto) == MARCA_PESSOAL
    assert hig.counts["config_pessoal"] == 1


def test_memoria_do_agente_apaga_o_campo_inteiro():
    hig = _hig()
    memoria = "---\nname: algo-lembrado\ndescription: uma coisa\nmetadata:\n  type: project\n---\n\ncorpo\n"
    assert hig.texto(memoria) == MARCA_PESSOAL
    assert hig.counts["memoria"] == 1


def test_tool_result_de_leitura_pessoal_e_redigido():
    hig = _hig(pessoais=("/casa/.claude",))
    uso = {
        "message": {
            "content": [
                {"type": "tool_use", "id": "tu-1", "name": "Read", "input": {"file_path": "/casa/.claude/NOTAS.md"}}
            ]
        }
    }
    hig.linha(uso)
    resultado = hig.linha(
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "tu-1", "content": "conteúdo pessoal"}]}}
    )
    assert resultado["message"]["content"][0]["content"] == MARCA_PESSOAL


def test_cat_de_caminho_pessoal_tambem_marca_o_resultado():
    hig = _hig(pessoais=("/casa/.claude",))
    hig.linha(
        {"message": {"content": [{"type": "tool_use", "id": "tu-2", "name": "Bash", "input": {"command": "cat /casa/.claude/NOTAS.md"}}]}}
    )
    saida = hig.linha(
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "tu-2", "content": "linha pessoal"}]}}
    )
    assert saida["message"]["content"][0]["content"] == MARCA_PESSOAL


def test_tool_use_result_com_caminho_pessoal_no_topo_da_linha():
    hig = _hig(pessoais=("/casa/.claude",))
    saida = hig.linha({"toolUseResult": {"file": {"filePath": "/casa/.claude/NOTAS.md", "content": "pessoal"}}})
    assert saida["toolUseResult"] == MARCA_PESSOAL


def test_linha_de_deferred_tools_e_removida():
    hig = _hig()
    assert hig.linha({"attachment": {"type": "deferred_tools_delta", "addedNames": ["mcp__algum__tool"]}}) is None
    assert hig.counts["linha_removida"] == 1


def test_bloco_de_imagem_vira_marcador():
    hig = _hig()
    saida = hig.valor({"type": "image", "source": {"type": "base64", "data": "iVBORw0KGgo"}})
    assert saida == {"type": "text", "text": MARCA_IMAGEM}


def test_png_grande_em_base64_vira_marcador_de_imagem():
    hig = _hig()
    import base64 as b64

    png = b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000).decode()
    assert hig.texto(png) == MARCA_IMAGEM


def test_blob_base64_que_nao_e_imagem_vira_marcador_de_binario():
    """Assinatura opaca de raciocínio: sai do arquivo, mas não é chamada de screenshot."""
    hig = _hig()
    assert hig.texto("Q0FRUw" + "A" * 4000) == MARCA_BASE64


# --------------------------------------------------------------------------- JSON válido
def test_mascara_so_em_valor_string_preserva_numero():
    """O bug antigo: `mask_text` na linha crua comia `"resetsAt": <número>`."""
    hig = _hig()
    saida = hig.linha({"erro": {"resetsAt": 1767225600000, "texto": "ligue 11 91234-5678"}})
    assert saida["erro"]["resetsAt"] == 1767225600000
    assert "91234" not in saida["erro"]["texto"]
    assert json.loads(json.dumps(saida))


def test_export_descarta_linha_que_nao_e_json(tmp_path):
    origem = _sessao(tmp_path, "ws", ["isto não é json\n", _linha(type="user", timestamp="2026-01-02T10:05:00.000Z")])
    dest = tmp_path / "sessions"

    index = export_logs([origem], dest, _hig(), {"ws": "orquestrador"}, JANELA)

    assert index[0]["descartadas"] == 1
    linhas = (dest / "orquestrador" / "sessao.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 11
    assert all(json.loads(linha) for linha in linhas)


# --------------------------------------------------------------------------- segredos e PII
def test_scrub_valor_do_env():
    hig = _hig(segredos=["segredo-super-longo-123"])
    limpa = hig.texto("minha chave e segredo-super-longo-123")
    assert "segredo-super-longo-123" not in limpa
    assert "<REDACTED>" in limpa
    assert hig.counts["env_secrets"] == 1


def test_scrub_chave_google():
    hig = _hig()
    limpa = hig.texto(f"use a chave {FAKE_GOOGLE_KEY} no client")
    assert FAKE_GOOGLE_KEY not in limpa
    assert hig.counts["google_api_key"] == 1


def test_scrub_padrao_apikey():
    hig = _hig()
    limpa = hig.texto('curl -H "apikey: abcdefgh12345678ijklmnop"')
    assert "abcdefgh12345678ijklmnop" not in limpa
    assert hig.counts["apikey_pattern"] == 1


def test_scrub_email_via_mask_text():
    hig = _hig()
    limpa = hig.texto("contato: joao.silva@example.com")
    assert limpa == "contato: ***@example.com"
    assert hig.counts["email"] == 1


def test_texto_sem_nada_sensivel_fica_intacto():
    hig = _hig()
    texto = "nada de sensível aqui, so um R$ 209,90"
    assert hig.texto(texto) == texto
    assert sum(hig.counts.values()) == 0


# --------------------------------------------------------------------------- fontes e nomes neutros
def test_descobrir_workspaces_pega_o_do_repo_e_os_irmaos(tmp_path):
    (tmp_path / "espaco" / "projeto").mkdir(parents=True)
    (tmp_path / "teste-espaco").mkdir()
    (tmp_path / "outro").mkdir()

    workspaces = descobrir_workspaces(tmp_path / "espaco" / "projeto")

    assert workspaces[0] == tmp_path / "espaco"
    assert tmp_path / "teste-espaco" in workspaces
    assert tmp_path / "outro" not in workspaces


def test_globs_padrao_saem_do_workspace(tmp_path):
    assert globs_padrao([tmp_path / "espaco"]) == [_slug(tmp_path / "espaco") + "*"]


def test_classificar_separa_orquestrador_executor_e_outro-workspace(tmp_path):
    principal = tmp_path / "espaco"
    irmao = tmp_path / "teste-espaco"
    workspaces = [principal, irmao]
    role = "759d01f1-1866-42fa-894e-1dc2c1933daf"

    assert classificar(_slug(principal), workspaces)[0] == "orquestrador"
    assert classificar(_slug(principal / "projeto"), workspaces)[0] == "orquestrador"
    assert classificar(f"{_slug(principal)}--sandbox-roles-{role}", workspaces) == ("executor", role)
    assert classificar(f"{_slug(principal / 'projeto')}--sandbox-roles-{role}", workspaces) == ("executor", role)
    assert classificar(_slug(irmao), workspaces)[0] == "outro-workspace"
    assert classificar(f"{_slug(irmao)}--sandbox-roles-{role}", workspaces)[0] == "outro-workspace"


def test_mapear_destinos_numera_executores_e_nao_vaza_o_nome_de_origem(tmp_path):
    principal = tmp_path / "espaco"
    r1 = "111d01f1-1866-42fa-894e-1dc2c1933daf"
    r2 = "222d01f1-1866-42fa-894e-1dc2c1933daf"
    dirs = [
        tmp_path / _slug(principal),
        tmp_path / f"{_slug(principal)}--sandbox-roles-{r1}",
        tmp_path / f"{_slug(principal)}--sandbox-roles-{r2}",
    ]

    destinos = mapear_destinos(dirs, [principal])

    assert sorted(destinos.values()) == ["executor-1", "executor-2", "orquestrador"]
    assert all("sandbox" not in nome and "espaco" not in nome for nome in destinos.values())


# --------------------------------------------------------------------------- janela e filtros
def test_dentro_da_janela_aceita_sobreposicao_e_recusa_o_que_e_anterior():
    assert _dentro_da_janela("2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z", JANELA)
    assert _dentro_da_janela("2025-12-30T10:00:00Z", "2026-01-01T05:00:00Z", JANELA)
    assert not _dentro_da_janela("2025-12-01T10:00:00Z", "2025-12-02T10:00:00Z", JANELA)
    assert not _dentro_da_janela(None, None, JANELA)


def test_export_pula_sessao_curta_e_fora_da_janela(tmp_path):
    origem = tmp_path / "ws"
    origem.mkdir()
    (origem / "curta.jsonl").write_text(_linha(type="user", timestamp="2026-01-02T10:00:00.000Z"), encoding="utf-8")
    velha = "".join(_linha(type="user", timestamp="2020-01-02T10:00:00.000Z") for _ in range(12))
    (origem / "velha.jsonl").write_text(velha, encoding="utf-8")

    index = export_logs([origem], tmp_path / "sessions", _hig(), {"ws": "orquestrador"}, JANELA)

    assert index == []


def test_export_e_idempotente_e_limpa_nomes_antigos(tmp_path):
    origem = _sessao(tmp_path, "ws", [])
    dest = tmp_path / "sessions"
    (dest / "nome-antigo").mkdir(parents=True)
    (dest / "nome-antigo" / "velho.jsonl").write_text("{}\n", encoding="utf-8")

    export_logs([origem], dest, _hig(), {"ws": "orquestrador"}, JANELA)
    primeiro = (dest / "orquestrador" / "sessao.jsonl").read_text(encoding="utf-8")
    export_logs([origem], dest, _hig(), {"ws": "orquestrador"}, JANELA)
    segundo = (dest / "orquestrador" / "sessao.jsonl").read_text(encoding="utf-8")

    assert primeiro == segundo
    assert not (dest / "nome-antigo").exists()


def test_index_lista_origem_janela_e_contagem(tmp_path):
    origem = _sessao(tmp_path, "ws", [])
    dest = tmp_path / "sessions"
    index = export_logs([origem], dest, _hig(), {"ws": "orquestrador"}, JANELA)

    escrever_index(dest, index, JANELA)
    texto = (dest / "INDEX.md").read_text(encoding="utf-8")

    assert "orquestrador/sessao.jsonl" in texto
    assert "2026-01-02T10:00:00.000Z" in texto
    assert "| 10 |" in texto
    assert "1 sessão(ões), 10 linha(s) exportada(s)." in texto


# --------------------------------------------------------------------------- check
def test_check_logs_detecta_segredo_e_falha(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    (ai_logs / "sessions" / "orquestrador").mkdir(parents=True)
    (ai_logs / "sessions" / "orquestrador" / "s.jsonl").write_text(
        _linha(texto=f"opa, {FAKE_GOOGLE_KEY} vazou aqui"), encoding="utf-8"
    )

    assert check_logs(ai_logs, segredos=[]) == 1


def test_check_logs_detecta_json_invalido_em_sessions(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    (ai_logs / "sessions" / "orquestrador").mkdir(parents=True)
    (ai_logs / "sessions" / "orquestrador" / "s.jsonl").write_text("{isto: não é json}\n", encoding="utf-8")

    assert check_logs(ai_logs, segredos=[]) == 1


def test_check_logs_detecta_literal_da_denylist_e_ancora(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    ai_logs.mkdir()
    (ai_logs / "a.md").write_text("host sub.exemplo.com\n", encoding="utf-8")
    (ai_logs / "b.md").write_text("# Modo X — protocolo de orquestração (Gabriel)\n", encoding="utf-8")

    assert check_logs(ai_logs, segredos=[], denylist=["sub.exemplo.com"]) == 1


def test_check_logs_limpo_passa(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    (ai_logs / "sessions" / "orquestrador").mkdir(parents=True)
    (ai_logs / "sessions" / "orquestrador" / "ok.jsonl").write_text(_linha(texto="nada demais"), encoding="utf-8")

    assert check_logs(ai_logs, segredos=[], denylist=["sub.exemplo.com"]) == 0


def test_check_logs_detecta_valor_de_env(tmp_path):
    ai_logs = tmp_path / "ai-logs"
    ai_logs.mkdir()
    (ai_logs / "relato.md").write_text("a chave usada foi segredo-vazado-123\n", encoding="utf-8")

    assert check_logs(ai_logs, segredos=["segredo-vazado-123"]) == 1


def test_check_logs_diretorio_ausente_passa(tmp_path):
    assert check_logs(tmp_path / "nao-existe", segredos=[]) == 0


def test_janela_do_projeto_sem_git_nao_quebra(tmp_path):
    from scripts.export_ai_logs import janela_do_projeto

    agora = datetime.fromisoformat("2026-01-03T00:00:00+00:00")
    ini, fim = janela_do_projeto(tmp_path, agora=agora)
    assert fim == agora
    assert ini < agora - timedelta(days=1)


# --------------------------------------------------------------------------- gate de PII dos logs da entrega
def test_check_logs_pii_flaga_telefone_no_nome_do_arquivo(tmp_path, capsys):
    arq = tmp_path / "wa-5511999990000.jsonl"
    arq.write_text('{"event": "inbound", "data": {"texto": "oi"}}\n', encoding="utf-8")

    assert check_logs_pii.main([str(arq)]) == 1
    saida = capsys.readouterr().out
    assert "telefone em claro no NOME do arquivo" in saida
    assert "1 ocorrência(s) de PII em claro" in saida


def test_check_logs_pii_passa_com_nome_e_conteudo_limpos(tmp_path, capsys):
    arq = tmp_path / "caminho-feliz.jsonl"
    arq.write_text('{"event": "inbound", "data": {"cep": "01310-***", "fone": "+55 ** *****-****"}}\n', encoding="utf-8")

    assert check_logs_pii.main([str(arq)]) == 0
    assert "0 ocorrência(s) de PII em claro" in capsys.readouterr().out
