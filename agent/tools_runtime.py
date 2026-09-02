"""Execução das tools que o operador cria no painel (`config/custom_tools.json`).

Este módulo transforma cada tool habilitada numa `agno.tools.Function` que o **Responder**
oferece ao Gemini por function calling. Ele vive fora de `agent/studio/` de propósito: quem
usa é o canal (o agente em produção), e o Studio só edita o registro.

Três garantias que o resto do sistema depende:

- **Nunca levanta no turno.** Timeout, rede fora, SQL inválido, variável de ambiente ausente:
  tudo vira a string `"erro: …"` devolvida ao modelo, que continua a conversa. O lead não fica
  no vácuo por causa de uma integração do painel.
- **Segredo não vaza.** `${env:X}` é resolvido aqui, na hora da chamada — nunca no arquivo de
  config nem na resposta da API. O valor resolvido é apagado (`***`) de qualquer texto que
  volte para o modelo, para o log ou para a tela.
- **SQL é só leitura.** A validação do registro (`runtime_config`) já exige um `SELECT`/`WITH`
  sem `;`, e o sqlite ainda é aberto em `mode=ro` — segunda linha de defesa, para o caso de um
  arquivo de config editado à mão.

Cada execução devolve um evento `tool_call` (`{tool, args, status, latency_ms, resultado}`) para
quem chamou: no turno, é a `Conversation` que o grava no JSONL (e o Lab o espelha no barramento);
no botão "Testar" do Studio, ninguém grava nada.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from agent.config import ROOT
from agent.runtime_config import (
    ENV_REF_RE,
    SQL_PARAM_RE,
    ConfigError,
    ConfigStore,
    CustomTool,
)

log = logging.getLogger("autoseguro.tools")

MAX_RESULTADO_EVENTO = 300      # o log guarda um resumo, não o dump inteiro
_TEMPLATE_RE = re.compile(r"{([a-z][a-z0-9_]*)}")


class ToolErro(RuntimeError):
    """Falha esperada de uma tool: vira `erro: …` para o modelo, nunca exceção no turno."""


# --------------------------------------------------------------------------- schema para o LLM
def schema_de(tool: CustomTool) -> dict[str, Any]:
    """JSON Schema dos parâmetros, no subconjunto que o Gemini lê (type/description/required)."""
    propriedades: dict[str, Any] = {}
    for nome, p in tool.parametros.items():
        propriedades[nome] = {"type": p.tipo}
        if p.descricao:
            propriedades[nome]["description"] = p.descricao
    return {
        "type": "object",
        "properties": propriedades,
        "required": [n for n, p in tool.parametros.items() if p.obrigatorio],
    }


# --------------------------------------------------------------------------- templates e segredos
def _resolver_env(texto: str, segredos: list[str]) -> str:
    """Troca `${env:X}` pelo valor do ambiente. Variável ausente é erro claro, não string vazia."""

    def troca(m: re.Match[str]) -> str:
        nome = m.group(1)
        valor = os.environ.get(nome)
        if valor is None:
            raise ToolErro(f"variável de ambiente {nome} não está definida no processo do agente")
        if valor:
            segredos.append(valor)
        return valor

    return ENV_REF_RE.sub(troca, texto or "")


def _aplicar_args(texto: str, args: dict[str, Any], escapar: bool) -> str:
    """Substitui `{param}` pelos argumentos. Chave desconhecida fica literal (não estoura)."""

    def troca(m: re.Match[str]) -> str:
        nome = m.group(1)
        if nome not in args:
            return m.group(0)
        valor = "" if args[nome] is None else str(args[nome])
        return quote(valor, safe="") if escapar else valor

    return _TEMPLATE_RE.sub(troca, texto or "")


def _render(texto: str, args: dict[str, Any], segredos: list[str], escapar: bool = False) -> str:
    """Ambiente PRIMEIRO, argumentos depois.

    A ordem é de segurança: se os argumentos entrassem antes, um `${env:CHAVE}` vindo do texto do
    lead (via LLM) seria resolvido e o segredo iria parar na URL da requisição.
    """
    return _aplicar_args(_resolver_env(texto, segredos), args, escapar)


def ocultar(texto: str, segredos: list[str]) -> str:
    """Apaga do texto qualquer valor de segredo que tenha sido resolvido nesta execução."""
    for segredo in segredos:
        if segredo and len(segredo) >= 4:
            texto = texto.replace(segredo, "***")
    return texto


# --------------------------------------------------------------------------- argumentos
def _coagir(valor: Any, tipo: str, nome: str) -> Any:
    """O modelo manda o que quer; aqui o argumento vira o tipo declarado ou é erro explícito."""
    if valor is None:
        return None
    try:
        if tipo == "integer":
            return int(str(valor).strip())
        if tipo == "number":
            return float(str(valor).strip())
        if tipo == "boolean":
            if isinstance(valor, bool):
                return valor
            return str(valor).strip().lower() in ("true", "1", "sim", "yes")
        return str(valor)
    except (TypeError, ValueError) as exc:
        raise ToolErro(f"parâmetro {nome} deveria ser {tipo}: {valor!r}") from exc


def preparar_args(tool: CustomTool, args: dict[str, Any] | None) -> dict[str, Any]:
    """Valida obrigatórios, ignora o que não foi declarado e converte os tipos."""
    recebidos = args or {}
    prontos: dict[str, Any] = {}
    for nome, p in tool.parametros.items():
        if nome not in recebidos or recebidos[nome] is None:
            if p.obrigatorio:
                raise ToolErro(f"parâmetro obrigatório ausente: {nome}")
            continue
        prontos[nome] = _coagir(recebidos[nome], p.tipo, nome)
    return prontos


# --------------------------------------------------------------------------- executores
async def _executar_http(tool: CustomTool, args: dict[str, Any], segredos: list[str], client: Any) -> str:
    cfg = tool.http
    assert cfg is not None  # garantido pelo validador do registro
    url = _render(cfg.url, args, segredos, escapar=True)
    headers = {k: _render(v, args, segredos) for k, v in cfg.headers.items()}
    # Valor de query NÃO é escapado aqui: quem monta a querystring é o httpx (escapar duas vezes
    # transformaria "a b" em "a%2520b").
    params = {k: _render(v, args, segredos) for k, v in cfg.query.items()}
    corpo: dict[str, Any] = {}
    if isinstance(cfg.body, dict):
        corpo["json"] = {
            k: _render(v, args, segredos) if isinstance(v, str) else v for k, v in cfg.body.items()
        }
    elif isinstance(cfg.body, str) and cfg.body:
        corpo["content"] = _render(cfg.body, args, segredos).encode("utf-8")

    proprio = client is None
    http = client or httpx.AsyncClient()
    try:
        resp = await http.request(
            cfg.metodo, url, headers=headers or None, params=params or None,
            timeout=tool.timeout_s, **corpo,
        )
    except httpx.HTTPError as exc:
        raise ToolErro(f"falha de rede ({type(exc).__name__})") from exc
    finally:
        if proprio:
            await http.aclose()

    if resp.status_code >= 400:
        raise ToolErro(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if cfg.resposta == "json":
        try:
            return json.dumps(resp.json(), ensure_ascii=False)
        except ValueError:
            return resp.text          # a API prometeu JSON e mandou outra coisa: devolve cru
    return resp.text


def _caminho_sqlite(conexao: str) -> Path:
    path = Path(conexao)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise ToolErro(f"banco não encontrado: {path}")
    return path


def _consultar_sqlite(conexao: str, query: str, params: dict[str, Any], max_linhas: int) -> str:
    """Somente leitura de verdade: a conexão é aberta com `mode=ro` via URI."""
    path = _caminho_sqlite(conexao)
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ToolErro(f"não consegui abrir o banco ({exc})") from exc
    try:
        con.row_factory = sqlite3.Row
        cursor = con.execute(query, params)
        linhas = [dict(linha) for linha in cursor.fetchmany(max_linhas)]
    except sqlite3.Error as exc:
        raise ToolErro(f"sql: {exc}") from exc
    finally:
        con.close()
    return json.dumps(linhas, ensure_ascii=False, default=str)


def _consultar_postgres(conexao: str, query: str, params: dict[str, Any], max_linhas: int) -> str:
    """Só existe se `psycopg` estiver instalado — não é dependência do projeto."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ToolErro("conexão postgresql exige o pacote psycopg, que não está instalado") from exc

    # psycopg usa `%(nome)s`; o registro guarda `:nome` (padrão do sqlite) para os dois casos.
    query_pg = SQL_PARAM_RE.sub(lambda m: f"%({m.group(1)})s", query)
    try:
        with psycopg.connect(conexao, autocommit=True) as con:
            con.read_only = True
            with con.cursor(row_factory=dict_row) as cur:
                cur.execute(query_pg, params)
                linhas = cur.fetchmany(max_linhas)
    except Exception as exc:  # psycopg.Error e falhas de conexão
        raise ToolErro(f"sql: {exc}") from exc
    return json.dumps(linhas, ensure_ascii=False, default=str)


async def _executar_sql(tool: CustomTool, args: dict[str, Any], segredos: list[str]) -> str:
    cfg = tool.sql
    assert cfg is not None  # garantido pelo validador do registro
    conexao = _resolver_env(cfg.conexao, segredos)
    if not conexao:
        raise ToolErro("conexão vazia")
    citados = set(SQL_PARAM_RE.findall(cfg.query))
    faltando = sorted(citados - set(args))
    if faltando:
        raise ToolErro(f"parâmetro obrigatório ausente: {', '.join(faltando)}")
    params = {nome: args[nome] for nome in citados}
    consulta = _consultar_postgres if conexao.startswith(("postgresql://", "postgres://")) else _consultar_sqlite
    return await asyncio.to_thread(consulta, conexao, cfg.query, params, cfg.max_linhas)


# --------------------------------------------------------------------------- entrada única
async def executar_tool(
    tool: CustomTool, args: dict[str, Any] | None = None, *, client: Any = None
) -> tuple[str, dict[str, Any]]:
    """Executa a tool e devolve `(texto para o modelo, evento tool_call)`. Nunca levanta."""
    inicio = time.perf_counter()
    segredos: list[str] = []
    status = "ok"
    try:
        preparados = preparar_args(tool, args)
        if tool.tipo == "http":
            corotina = _executar_http(tool, preparados, segredos, client)
        else:
            corotina = _executar_sql(tool, preparados, segredos)
        resultado = await asyncio.wait_for(corotina, timeout=tool.timeout_s)
        resultado = ocultar(resultado, segredos)[: tool.max_chars]
    except TimeoutError:
        status, preparados = "timeout", args or {}
        resultado = f"erro: a tool {tool.nome} passou de {tool.timeout_s:g}s e foi cancelada"
    except ToolErro as exc:
        status, preparados = "erro", args or {}
        resultado = ocultar(f"erro: {exc}", segredos)
    except Exception as exc:  # noqa: BLE001 — integração do painel não derruba o turno
        log.warning("tool %s falhou (%s): %s", tool.nome, type(exc).__name__, str(exc)[:200])
        status, preparados = "erro", args or {}
        resultado = ocultar(f"erro: {type(exc).__name__}: {str(exc)[:200]}", segredos)

    evento = {
        "tool": tool.nome,
        "args": preparados,
        "status": status,
        "latency_ms": int((time.perf_counter() - inicio) * 1000),
        "resultado": resultado[:MAX_RESULTADO_EVENTO],
    }
    return resultado, evento


# --------------------------------------------------------------------------- registro → agno
def carregar_tools(store: ConfigStore, emitir: Any = None, client: Any = None) -> list[Any]:
    """`Function`s das tools HABILITADAS. Registro vazio ou ilegível ⇒ `[]` (agente igual ao entregue).

    `emitir(evento)` é chamado depois de cada execução; é por ele que o `tool_call` chega ao log
    do turno. Sem ele (botão "Testar" do Studio), a execução não gera evento nenhum.
    """
    try:
        registro = store.custom_tools()
    except ConfigError as exc:
        log.error("custom_tools.json inválido (%s): o agente segue sem tools", exc)
        return []
    habilitadas = [t for t in registro.tools.values() if t.enabled]
    if not habilitadas:
        return []
    from agno.tools.function import Function

    return [_function(Function, tool, emitir, client) for tool in habilitadas]


def _function(Function: Any, tool: CustomTool, emitir: Any, client: Any) -> Any:
    async def entrypoint(**kwargs: Any) -> str:
        resultado, evento = await executar_tool(tool, kwargs, client=client)
        if emitir is not None:
            try:
                emitir(evento)
            except Exception as exc:  # noqa: BLE001 — observabilidade não derruba a tool
                log.warning("emissão de tool_call falhou (%s)", type(exc).__name__)
        return resultado

    entrypoint.__name__ = tool.nome
    return Function(
        name=tool.nome,
        description=tool.descricao,
        parameters=schema_de(tool),
        entrypoint=entrypoint,
        instructions=tool.instrucoes or None,
        add_instructions=True,
        # Sem isto o agno derivaria `parameters` da assinatura (`**kwargs` ⇒ schema vazio) e
        # embrulharia o entrypoint com `validate_call`. Ver o spike no reporte.
        skip_entrypoint_processing=True,
    )
