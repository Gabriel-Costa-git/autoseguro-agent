"""Registro e runtime das tools do painel: validação, HTTP, SQL e o evento `tool_call`.

Sem rede (`httpx.MockTransport`), sem LLM e sem tocar `config/` do repo: `ConfigStore(tmp_path)`
e bancos sqlite criados em `tmp_path`.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from agent.runtime_config import ConfigError, ConfigStore, CustomTool
from agent.tools_runtime import (
    ToolErro,
    _consultar_sqlite,
    carregar_tools,
    executar_tool,
    ocultar,
    schema_de,
)

HTTP_BASE = {
    "tipo": "http",
    "descricao": "Consulta a apólice do cliente pelo CPF.",
    "parametros": {"cpf": {"tipo": "string", "descricao": "CPF só dígitos", "obrigatorio": True}},
    "http": {"metodo": "GET", "url": "https://api.exemplo.test/apolices/{cpf}"},
}


@pytest.fixture
def store(tmp_path) -> ConfigStore:
    s = ConfigStore(tmp_path / "config")
    s.ensure_files()
    return s


def _tool(**over) -> CustomTool:
    dados = {"nome": "consulta_apolice", **HTTP_BASE, **over}
    return CustomTool.model_validate(dados)


def _cliente(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _banco(tmp_path, linhas: int = 3):
    path = tmp_path / "apolices.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE apolices (cpf TEXT, numero TEXT, status TEXT)")
    con.executemany(
        "INSERT INTO apolices VALUES (?, ?, ?)",
        [("12345678901", f"AP-{n}", "ativa") for n in range(linhas)],
    )
    con.commit()
    con.close()
    return path


# --------------------------------------------------------------------------- registro / validação
def test_upsert_grava_e_le_com_env_literal(store: ConfigStore):
    tool = store.upsert_custom_tool(
        "consulta_apolice",
        {**HTTP_BASE, "http": {**HTTP_BASE["http"], "headers": {"Authorization": "Bearer ${env:APOLICE_KEY}"}}},
    )
    assert tool.nome == "consulta_apolice"
    assert tool.criado_em and tool.atualizado_em
    bruto = json.loads((store.dir / "custom_tools.json").read_text(encoding="utf-8"))
    # o segredo NUNCA vai para o disco: só a referência
    assert bruto["tools"]["consulta_apolice"]["http"]["headers"]["Authorization"] == "Bearer ${env:APOLICE_KEY}"
    assert store.custom_tool("consulta_apolice").tipo == "http"


def test_upsert_preserva_criado_em_e_atualiza_o_resto(store: ConfigStore):
    primeira = store.upsert_custom_tool("consulta_apolice", HTTP_BASE)
    segunda = store.upsert_custom_tool("consulta_apolice", {**HTTP_BASE, "descricao": "outra coisa"})
    assert segunda.criado_em == primeira.criado_em
    assert segunda.descricao == "outra coisa"


def test_delete_e_tool_desconhecida(store: ConfigStore):
    store.upsert_custom_tool("consulta_apolice", HTTP_BASE)
    store.delete_custom_tool("consulta_apolice")
    assert store.custom_tools().tools == {}
    with pytest.raises(ConfigError):
        store.delete_custom_tool("consulta_apolice")
    with pytest.raises(ConfigError):
        store.custom_tool("consulta_apolice")


@pytest.mark.parametrize(
    "nome",
    ["Maiuscula", "com-hifen", "ab", "1comeca_com_numero", "com espaco", "x" * 42],
)
def test_nome_invalido_e_rejeitado(store: ConfigStore, nome: str):
    with pytest.raises(ConfigError):
        store.upsert_custom_tool(nome, HTTP_BASE)


@pytest.mark.parametrize(
    "patch",
    [
        {"descricao": "   "},                                             # o LLM precisa da descrição
        {"tipo": "sql", "sql": None},                                     # tipo sem bloco
        {"http": {"metodo": "GET", "url": "ftp://x/y"}},                  # url não-http
        {"http": {"metodo": "GET", "url": "https://x/{nao_existe}"}},     # template desconhecido
        {"http": {"metodo": "GET", "url": "https://x", "headers": {"A": "${env:minuscula}"}}},
        {"parametros": {"CPF": {"tipo": "string"}}},                      # nome de parâmetro inválido
        {"parametros": {"cpf": {"tipo": "date"}}},                        # tipo fora do contrato
        {"timeout_s": 0},
        {"max_chars": 10},
    ],
)
def test_schema_rejeita_configuracao_invalida(store: ConfigStore, patch: dict):
    with pytest.raises(ConfigError):
        store.upsert_custom_tool("consulta_apolice", {**HTTP_BASE, **patch})


SQL_BASE = {
    "tipo": "sql",
    "descricao": "Apólices do CPF.",
    "parametros": {"cpf": {"tipo": "string", "obrigatorio": True}},
    "sql": {"conexao": "apolices.db", "query": "SELECT numero, status FROM apolices WHERE cpf = :cpf", "max_linhas": 2},
}


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE apolices SET status = 'x'",
        "DELETE FROM apolices",
        "SELECT 1; DROP TABLE apolices",
        "SELECT * INTO copia FROM apolices",
        "PRAGMA table_info(apolices)",
        "INSERT INTO apolices VALUES (1)",
        "SELECT numero FROM apolices WHERE cpf = :nao_declarado",
        "   ",
    ],
)
def test_sql_so_leitura(store: ConfigStore, query: str):
    with pytest.raises(ConfigError):
        store.upsert_custom_tool("consulta_sql", {**SQL_BASE, "sql": {**SQL_BASE["sql"], "query": query}})


def test_sql_aceita_with_e_env_na_conexao(store: ConfigStore):
    tool = store.upsert_custom_tool(
        "consulta_sql",
        {
            **SQL_BASE,
            "sql": {
                "conexao": "${env:APOLICES_DB}",
                "query": "WITH ativas AS (SELECT * FROM apolices WHERE status = 'ativa') "
                         "SELECT numero FROM ativas WHERE cpf = :cpf",
                "max_linhas": 5,
            },
        },
    )
    assert tool.sql is not None and tool.sql.conexao == "${env:APOLICES_DB}"


def test_versao_muda_com_o_registro_e_ignora_desabilitada(store: ConfigStore):
    assert store.custom_tools_version() == ""
    store.upsert_custom_tool("consulta_apolice", HTTP_BASE)
    v1 = store.custom_tools_version()
    assert v1
    store.upsert_custom_tool("consulta_apolice", {**HTTP_BASE, "descricao": "mudou"})
    assert store.custom_tools_version() != v1
    store.upsert_custom_tool("consulta_apolice", {**HTTP_BASE, "descricao": "mudou", "enabled": False})
    assert store.custom_tools_version() == ""      # nenhuma habilitada = agente da entrega


# --------------------------------------------------------------------------- schema para o LLM
def test_schema_de_gera_json_schema_minimo():
    tool = _tool(parametros={
        "cpf": {"tipo": "string", "descricao": "CPF só dígitos", "obrigatorio": True},
        "ano": {"tipo": "integer", "obrigatorio": False},
    })
    assert schema_de(tool) == {
        "type": "object",
        "properties": {
            "cpf": {"type": "string", "description": "CPF só dígitos"},
            "ano": {"type": "integer"},
        },
        "required": ["cpf"],
    }


# --------------------------------------------------------------------------- runtime HTTP
@pytest.mark.asyncio
async def test_http_monta_url_headers_e_query(monkeypatch):
    monkeypatch.setenv("APOLICE_KEY", "segredo-do-cofre")
    vistos: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos["url"] = str(request.url)
        vistos["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"numero": "AP-1", "status": "ativa"})

    tool = _tool(http={
        "metodo": "GET",
        "url": "https://api.exemplo.test/apolices/{cpf}",
        "headers": {"Authorization": "Bearer ${env:APOLICE_KEY}"},
        "query": {"formato": "curto", "doc": "{cpf}"},
        "resposta": "json",
    })
    resultado, evento = await executar_tool(tool, {"cpf": "12345678901"}, client=_cliente(handler))

    assert vistos["url"] == "https://api.exemplo.test/apolices/12345678901?formato=curto&doc=12345678901"
    assert vistos["auth"] == "Bearer segredo-do-cofre"
    assert json.loads(resultado) == {"numero": "AP-1", "status": "ativa"}
    assert evento["status"] == "ok" and evento["tool"] == "consulta_apolice"


@pytest.mark.asyncio
async def test_http_escapa_o_parametro_na_url():
    vistos: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos["raw_path"] = request.url.raw_path
        return httpx.Response(200, json={})

    tool = _tool(http={"metodo": "GET", "url": "https://x.test/a/{cpf}"})
    await executar_tool(tool, {"cpf": "a b/c?d"}, client=_cliente(handler))
    # o `?d` continua no CAMINHO (não virou querystring) e a barra não abriu outro segmento
    assert vistos["raw_path"] == b"/a/a%20b%2Fc%3Fd"


@pytest.mark.asyncio
async def test_http_post_com_body_e_resposta_texto():
    vistos: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos["body"] = json.loads(request.content)
        return httpx.Response(200, text="  ok, achei  ")

    tool = _tool(http={
        "metodo": "POST",
        "url": "https://x.test/busca",
        "body": {"documento": "{cpf}", "limite": 5},
        "resposta": "texto",
    })
    resultado, _ = await executar_tool(tool, {"cpf": "123"}, client=_cliente(handler))
    assert vistos["body"] == {"documento": "123", "limite": 5}
    assert resultado == "  ok, achei  "


@pytest.mark.asyncio
async def test_http_erro_vira_texto_para_o_llm_sem_levantar():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="não achei")

    resultado, evento = await executar_tool(_tool(), {"cpf": "1"}, client=_cliente(handler))
    assert resultado.startswith("erro: HTTP 404")
    assert evento["status"] == "erro"


@pytest.mark.asyncio
async def test_http_rede_fora_vira_texto():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=request)

    resultado, evento = await executar_tool(_tool(), {"cpf": "1"}, client=_cliente(handler))
    assert resultado == "erro: falha de rede (ConnectError)"
    assert evento["status"] == "erro"


@pytest.mark.asyncio
async def test_segredo_nao_vaza_no_resultado_nem_no_evento(monkeypatch):
    monkeypatch.setenv("APOLICE_KEY", "segredo-do-cofre")

    def handler(request: httpx.Request) -> httpx.Response:
        # API mal-educada que ecoa o token recebido
        return httpx.Response(200, text=f"token usado: {request.headers.get('authorization')}")

    tool = _tool(http={
        "metodo": "GET", "url": "https://x.test/a",
        "headers": {"Authorization": "${env:APOLICE_KEY}"}, "resposta": "texto",
    })
    resultado, evento = await executar_tool(tool, {"cpf": "1"}, client=_cliente(handler))
    assert "segredo-do-cofre" not in resultado
    assert "segredo-do-cofre" not in evento["resultado"]
    assert resultado == "token usado: ***"


@pytest.mark.asyncio
async def test_variavel_de_ambiente_ausente_e_erro_claro(monkeypatch):
    monkeypatch.delenv("APOLICE_KEY", raising=False)
    tool = _tool(http={"metodo": "GET", "url": "https://x.test/a", "headers": {"A": "${env:APOLICE_KEY}"}})
    resultado, evento = await executar_tool(tool, {"cpf": "1"}, client=_cliente(lambda r: httpx.Response(200)))
    assert resultado == "erro: variável de ambiente APOLICE_KEY não está definida no processo do agente"
    assert evento["status"] == "erro"


@pytest.mark.asyncio
async def test_resultado_truncado_em_max_chars():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 5000)

    tool = _tool(max_chars=100, http={"metodo": "GET", "url": "https://x.test/a", "resposta": "texto"})
    resultado, evento = await executar_tool(tool, {"cpf": "1"}, client=_cliente(handler))
    assert len(resultado) == 100
    assert len(evento["resultado"]) <= 300


@pytest.mark.asyncio
async def test_timeout_vira_status_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, text="tarde demais")

    tool = _tool(timeout_s=0.05, http={"metodo": "GET", "url": "https://x.test/a", "resposta": "texto"})
    resultado, evento = await executar_tool(tool, {"cpf": "1"}, client=_cliente(handler))
    assert evento["status"] == "timeout"
    assert "passou de 0.05s" in resultado


@pytest.mark.asyncio
async def test_parametro_obrigatorio_ausente_e_coercao_de_tipo():
    resultado, evento = await executar_tool(_tool(), {}, client=_cliente(lambda r: httpx.Response(200)))
    assert resultado == "erro: parâmetro obrigatório ausente: cpf"
    assert evento["status"] == "erro"

    vistos: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vistos["url"] = str(request.url)
        return httpx.Response(200, json={})

    tool = _tool(
        parametros={"ano": {"tipo": "integer", "obrigatorio": True}},
        http={"metodo": "GET", "url": "https://x.test/a/{ano}"},
    )
    _, evento = await executar_tool(tool, {"ano": "2019"}, client=_cliente(handler))
    assert vistos["url"].endswith("/a/2019")
    assert evento["args"] == {"ano": 2019}       # veio string do LLM, virou int


# --------------------------------------------------------------------------- runtime SQL
@pytest.mark.asyncio
async def test_sql_le_do_sqlite_com_parametro_nomeado(tmp_path):
    banco = _banco(tmp_path, linhas=3)
    tool = CustomTool.model_validate({
        "nome": "consulta_sql", **SQL_BASE,
        "sql": {**SQL_BASE["sql"], "conexao": str(banco), "max_linhas": 2},
    })
    resultado, evento = await executar_tool(tool, {"cpf": "12345678901"})
    linhas = json.loads(resultado)
    assert linhas == [{"numero": "AP-0", "status": "ativa"}, {"numero": "AP-1", "status": "ativa"}]
    assert evento["status"] == "ok"              # max_linhas cortou a terceira


@pytest.mark.asyncio
async def test_sql_banco_inexistente_vira_erro(tmp_path):
    tool = CustomTool.model_validate({
        "nome": "consulta_sql", **SQL_BASE,
        "sql": {**SQL_BASE["sql"], "conexao": str(tmp_path / "nao-existe.db")},
    })
    resultado, evento = await executar_tool(tool, {"cpf": "1"})
    assert resultado.startswith("erro: banco não encontrado")
    assert evento["status"] == "erro"


def test_conexao_sqlite_e_somente_leitura(tmp_path):
    """Segunda linha de defesa: mesmo com uma query de escrita, a conexão é `mode=ro`."""
    banco = _banco(tmp_path)
    with pytest.raises(ToolErro):
        _consultar_sqlite(str(banco), "UPDATE apolices SET status = 'cancelada'", {}, 10)
    con = sqlite3.connect(banco)
    assert con.execute("SELECT count(*) FROM apolices WHERE status = 'ativa'").fetchone()[0] == 3
    con.close()


@pytest.mark.asyncio
async def test_sql_postgres_sem_psycopg_da_erro_claro(monkeypatch):
    import builtins

    real = builtins.__import__

    def sem_psycopg(nome, *args, **kwargs):
        if nome.startswith("psycopg"):
            raise ImportError("no psycopg")
        return real(nome, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", sem_psycopg)
    tool = CustomTool.model_validate({
        "nome": "consulta_sql", **SQL_BASE,
        "sql": {**SQL_BASE["sql"], "conexao": "postgresql://user@host/db"},
    })
    resultado, _ = await executar_tool(tool, {"cpf": "1"})
    assert "psycopg" in resultado and resultado.startswith("erro:")


# --------------------------------------------------------------------------- evento tool_call
@pytest.mark.asyncio
async def test_evento_tool_call_tem_o_formato_do_contrato():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"numero": "AP-1"})

    _, evento = await executar_tool(_tool(), {"cpf": "123"}, client=_cliente(handler))
    assert set(evento) == {"tool", "args", "status", "latency_ms", "resultado"}
    assert evento["tool"] == "consulta_apolice"
    assert evento["args"] == {"cpf": "123"}
    assert evento["status"] == "ok"
    assert isinstance(evento["latency_ms"], int)
    assert json.loads(evento["resultado"]) == {"numero": "AP-1"}


def test_ocultar_ignora_segredo_curto_demais():
    assert ocultar("valor abc", ["abc"]) == "valor abc"      # 3 chars: risco de mutilar texto normal
    assert ocultar("valor abcd", ["abcd"]) == "valor ***"


# --------------------------------------------------------------------------- carregar_tools
def test_sem_tool_habilitada_nao_carrega_nada(store: ConfigStore):
    assert carregar_tools(store) == []
    store.upsert_custom_tool("consulta_apolice", {**HTTP_BASE, "enabled": False})
    assert carregar_tools(store) == []


def test_registro_ilegivel_nao_derruba_o_agente(store: ConfigStore):
    (store.dir / "custom_tools.json").write_text("{quebrado", encoding="utf-8")
    store._cache.clear()
    assert carregar_tools(store) == []


def test_carregar_tools_monta_a_function_do_agno(store: ConfigStore):
    store.upsert_custom_tool(
        "consulta_apolice",
        {**HTTP_BASE, "instrucoes": "Confirme só os 4 últimos dígitos."},
    )
    (fn,) = carregar_tools(store)
    assert fn.name == "consulta_apolice"
    assert fn.description == "Consulta a apólice do cliente pelo CPF."
    assert fn.parameters == {
        "type": "object",
        "properties": {"cpf": {"type": "string", "description": "CPF só dígitos"}},
        "required": ["cpf"],
    }
    assert fn.instructions == "Confirme só os 4 últimos dígitos."
    assert fn.add_instructions is True
    assert fn.skip_entrypoint_processing is True   # sem isso o agno reescreveria o schema


@pytest.mark.asyncio
async def test_entrypoint_executa_e_emite_evento(store: ConfigStore):
    store.upsert_custom_tool("consulta_apolice", HTTP_BASE)
    eventos: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ativa"})

    (fn,) = carregar_tools(store, eventos.append, client=_cliente(handler))
    resultado = await fn.entrypoint(cpf="123")

    assert json.loads(resultado) == {"status": "ativa"}
    assert [e["tool"] for e in eventos] == ["consulta_apolice"]
    assert eventos[0]["args"] == {"cpf": "123"}


@pytest.mark.asyncio
async def test_emissor_quebrado_nao_derruba_a_tool(store: ConfigStore):
    store.upsert_custom_tool("consulta_apolice", HTTP_BASE)

    def emissor_ruim(_evento):
        raise RuntimeError("barramento fora")

    (fn,) = carregar_tools(store, emissor_ruim, client=_cliente(lambda r: httpx.Response(200, json={})))
    assert await fn.entrypoint(cpf="1") == "{}"
