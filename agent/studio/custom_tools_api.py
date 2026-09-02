"""Rotas do registro de tools do painel: criar, editar, apagar e TESTAR uma tool.

Quem valida é o `ConfigStore` (`agent/runtime_config.py`): nome, tipo, templates de parâmetro,
referência de ambiente e a regra de SQL somente leitura. Aqui só se traduz `ConfigError` em 400
e tool inexistente em 404 — a mesma regra de ouro do resto do Studio.

Sobre segredo: o registro guarda `${env:NOME}` literal, e é isso que esta API devolve. O valor
real só existe dentro de `agent/tools_runtime.py`, na hora da chamada, e é apagado (`***`) de
qualquer texto que volte. `GET /api/custom-tools/env` existe justamente para o frontend oferecer
os NOMES das variáveis sem nunca ver um valor.

`POST /{nome}/testar` roda a tool de verdade, com os mesmos limites do runtime, mas sem LLM e sem
`emitir`: nenhum `tool_call` entra no log de conversa nenhuma.
"""
from __future__ import annotations

import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent.runtime_config import ConfigError, ConfigStore, CustomTool
from agent.tools_runtime import executar_tool

NOME_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class TesteIn(BaseModel):
    args: dict[str, Any] = {}


def _store(request: Request) -> ConfigStore:
    return request.app.state.store


def _tool_ou_404(request: Request, nome: str) -> CustomTool:
    try:
        return _store(request).custom_tool(nome)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=f"tool desconhecida: {nome}") from exc


def construir_router() -> APIRouter:
    api = APIRouter(prefix="/api/custom-tools", tags=["custom-tools"])

    @api.get("/env")
    def variaveis_de_ambiente() -> dict[str, Any]:
        """Só os NOMES do ambiente do processo, para o frontend sugerir `${env:X}`."""
        return {"vars": sorted(nome for nome in os.environ if NOME_ENV_RE.fullmatch(nome))}

    @api.get("")
    def listar(request: Request) -> dict[str, Any]:
        try:
            registro = _store(request).custom_tools()
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"tools": {nome: t.model_dump(mode="json") for nome, t in registro.tools.items()}}

    @api.put("/{nome}")
    def salvar(request: Request, nome: str, corpo: dict[str, Any]) -> dict[str, Any]:
        if corpo.get("nome") not in (None, nome):
            raise HTTPException(status_code=400, detail=f"nome do corpo ({corpo['nome']}) difere da URL ({nome})")
        try:
            tool = _store(request).upsert_custom_tool(nome, corpo)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return tool.model_dump(mode="json")

    @api.delete("/{nome}")
    def apagar(request: Request, nome: str) -> dict[str, bool]:
        _tool_ou_404(request, nome)
        _store(request).delete_custom_tool(nome)
        return {"ok": True}

    @api.post("/{nome}/testar")
    async def testar(request: Request, nome: str, corpo: TesteIn | None = None) -> dict[str, Any]:
        tool = _tool_ou_404(request, nome)
        args = corpo.args if corpo else {}
        resultado, evento = await executar_tool(
            tool, args, client=getattr(request.app.state, "tools_client", None)
        )
        ok = evento["status"] == "ok"
        return {
            "ok": ok,
            "resultado": resultado,
            "latency_ms": evento["latency_ms"],
            "erro": None if ok else resultado,
        }

    return api


router = construir_router()
