"""API HTTP do Studio: prompts (versões/ativação), overrides de tools/settings, o valor
efetivo de cada parâmetro, o catálogo de modelos do Gemini e as rotas de Atendimentos
(`agent/studio/atendimentos_api.py`). A sessão de teste (Lab) mora em `agent/studio/lab.py`
e é montada aqui se o módulo existir (import tolerante).

`app.state` é o ponto de injeção de tudo o que os routers usam: `store`,
`conversation_factory`, `takeover`, `catalogo`, `evolution_sender` e
`models_client_factory` — os testes trocam qualquer um deles sem mexer na assinatura de
`build_studio_app`.

Regra de ouro deste módulo: ele NUNCA decide comportamento do agente — só lê/escreve o
`ConfigStore` (`agent/runtime_config.py`), lê os logs pelo `Catalogo` e traduz
`ConfigError` em 400, coisa inexistente em 404.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.atendimentos import Catalogo
from agent.config import settings
from agent.runtime_config import ConfigError, ConfigStore, PromptSlot
from agent.runtime_config import store as _singleton_store
from agent.studio import models_catalog
from agent.studio.atendimentos_api import construir_router as construir_atendimentos
from agent.takeover import TakeoverStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------- bodies
class VersaoIn(BaseModel):
    name: str
    text: str
    note: str = ""
    activate: bool = True


class EdicaoIn(BaseModel):
    text: str
    note: str | None = None


class AtivarIn(BaseModel):
    name: str


# --------------------------------------------------------------------------- helpers
def _slot_ou_404(store: ConfigStore, key: str) -> PromptSlot:
    slot = store.prompts().slots.get(key)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"slot desconhecido: {key}")
    return slot


def _versao_ou_404(slot: PromptSlot, name: str) -> None:
    if name not in slot.versions:
        raise HTTPException(status_code=404, detail=f"versão desconhecida: {name}")


def _overrides_planas(mapa: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`{chave: {value, origem, default}}` → `{chave: value}`, só as com origem `override`."""
    return {k: v["value"] for k, v in mapa.items() if v["origem"] == "override"}


def _overrides_agrupadas(mapa: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """`{grupo: {chave: {value, origem, default}}}` → `{grupo: {chave: value}}` (grupo sem override some)."""
    out: dict[str, Any] = {}
    for grupo, chaves in mapa.items():
        sub = _overrides_planas(chaves)
        if sub:
            out[grupo] = sub
    return out


# --------------------------------------------------------------------------- app
def build_studio_app(
    store: ConfigStore | None = None,
    conversation_factory: Callable[..., Awaitable[Any]] | None = None,
) -> FastAPI:
    store = store if store is not None else _singleton_store
    store.ensure_files()

    if conversation_factory is None:
        from agent.channels.cli import montar_conversa

        conversation_factory = montar_conversa

    app = FastAPI(title="AutoSeguro Studio")
    app.state.store = store
    app.state.conversation_factory = conversation_factory
    # Takeover e catálogo moram no mesmo lugar que o canal usa: `config/` do store injetado
    # e `logs/`. Nada é lido aqui — só na chamada da rota (hot-reload e testes com tmp_path).
    app.state.takeover = TakeoverStore(store.dir)
    app.state.catalogo = Catalogo(settings.log_dir, takeover=app.state.takeover)
    app.state.evolution_sender = None
    app.state.models_client_factory = None

    # ------------------------------------------------------------ prompts
    @app.get("/api/prompts")
    def listar_prompts() -> dict[str, Any]:
        return store.prompts().model_dump(mode="json")

    @app.post("/api/prompts/{key}/versions")
    def criar_versao(key: str, body: VersaoIn) -> PromptSlot:
        _slot_ou_404(store, key)
        try:
            return store.add_version(key, body.name, body.text, body.note, body.activate)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/prompts/{key}/versions/{name}")
    def editar_versao(key: str, name: str, body: EdicaoIn) -> PromptSlot:
        slot = _slot_ou_404(store, key)
        _versao_ou_404(slot, name)
        try:
            return store.edit_version(key, name, body.text, body.note)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/prompts/{key}/active")
    def ativar_versao(key: str, body: AtivarIn) -> PromptSlot:
        slot = _slot_ou_404(store, key)
        _versao_ou_404(slot, body.name)
        try:
            return store.set_active(key, body.name)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/prompts/{key}/versions/{name}")
    def apagar_versao(key: str, name: str) -> PromptSlot:
        slot = _slot_ou_404(store, key)
        _versao_ou_404(slot, name)
        try:
            return store.delete_version(key, name)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------ tools / config / effective
    @app.get("/api/effective")
    def effective() -> dict[str, Any]:
        return store.snapshot()

    @app.get("/api/tools")
    def get_tools() -> dict[str, Any]:
        return _overrides_agrupadas(store.snapshot()["tools"])

    @app.put("/api/tools")
    def put_tools(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 — padrão FastAPI
        try:
            store.set_overrides("tools", patch)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _overrides_agrupadas(store.snapshot()["tools"])

    @app.delete("/api/tools/{caminho:path}")
    def delete_tools(caminho: str) -> dict[str, Any]:
        try:
            store.clear_override(f"tools.{caminho.replace('/', '.')}")
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _overrides_agrupadas(store.snapshot()["tools"])

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return _overrides_planas(store.snapshot()["settings"])

    @app.put("/api/config")
    def put_config(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 — padrão FastAPI
        try:
            store.set_overrides("settings", patch)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _overrides_planas(store.snapshot()["settings"])

    @app.delete("/api/config/{caminho:path}")
    def delete_config(caminho: str) -> dict[str, Any]:
        try:
            store.clear_override(f"settings.{caminho.replace('/', '.')}")
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _overrides_planas(store.snapshot()["settings"])

    # ------------------------------------------------------------ catálogo de modelos
    def _catalogo_modelos(dados: dict[str, Any] | None) -> dict[str, Any]:
        """Sem cache, a única opção honesta é o modelo que o agente está usando agora."""
        if dados and dados.get("modelos"):
            return {"modelos": dados["modelos"], "atualizado_em": dados.get("atualizado_em"), "cache": True}
        atual = store.param("settings.gemini_model")
        return {"modelos": [{"id": atual, "nome": atual}], "atualizado_em": None, "cache": False}

    @app.get("/api/models")
    def listar_modelos() -> dict[str, Any]:
        return _catalogo_modelos(models_catalog.listar(store.dir))

    @app.post("/api/models/refresh")
    def atualizar_modelos() -> dict[str, Any]:
        try:
            dados = models_catalog.atualizar(
                store.dir, settings.google_api_key, client_factory=app.state.models_client_factory
            )
        except models_catalog.ModelsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _catalogo_modelos(dados)

    # ------------------------------------------------------------ atendimentos
    app.include_router(construir_atendimentos())

    # ------------------------------------------------------------ saúde + lab (se já existir)
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "studio": True}

    try:
        from agent.studio.lab import router as lab_router
    except ImportError:
        pass
    else:
        app.include_router(lab_router)

    # ------------------------------------------------------------ estáticos
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
