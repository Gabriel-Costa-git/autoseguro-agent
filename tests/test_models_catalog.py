"""Testes do catálogo de modelos do Gemini: módulo (`agent/studio/models_catalog.py`)
e as rotas `/api/models` do Studio.

Sem rede: o cliente do Google entra por `client_factory` (no módulo) ou por
`app.state.models_client_factory` (nas rotas), com objetos que só expõem o que o
código lê — `name`, `display_name` e `supported_actions`.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.runtime_config import ConfigStore
from agent.studio import app as app_mod
from agent.studio import models_catalog
from agent.studio.app import build_studio_app
from agent.studio.models_catalog import (
    ModelsError,
    atualizar,
    e_modelo_de_texto,
    listar,
)


# --------------------------------------------------------------------------- dublês
class FakeModel:
    def __init__(self, name: str, display_name: str | None, supported_actions: list[str] | None) -> None:
        self.name = name
        self.display_name = display_name
        self.supported_actions = supported_actions


MODELOS_API = [
    FakeModel("models/gemini-2.5-flash", "Gemini 2.5 Flash", ["generateContent", "countTokens"]),
    FakeModel("models/gemini-2.5-pro", "Gemini 2.5 Pro", ["generateContent"]),
    FakeModel("models/text-embedding-004", "Embedding 004", ["embedContent"]),   # não conversa
    FakeModel("models/veo-3", "Veo 3", None),                                    # sem ações
    # Estes DECLARAM generateContent e mesmo assim não servem ao agente:
    FakeModel("models/gemini-2.5-flash-preview-tts", "Gemini 2.5 Flash TTS", ["generateContent"]),
    FakeModel("models/gemini-2.5-flash-image", "Nano Banana", ["generateContent"]),
]

# Amostra do refresh real (40 modelos): o que tem de sobrar é só a coluna da esquerda.
MODELOS_REAIS = [
    ("models/gemini-2.5-flash", True),
    ("models/gemini-2.5-pro", True),
    ("models/gemini-3.5-flash-lite", True),
    ("models/gemma-3-27b-it", True),
    ("models/codegemma-7b-it", True),
    ("models/gemini-2.5-flash-preview-tts", False),
    ("models/gemini-2.5-pro-preview-tts", False),
    ("models/gemini-2.5-flash-image", False),
    ("models/imagen-4.0-generate-001", False),
    ("models/veo-3.0-generate-preview", False),
    ("models/text-embedding-004", False),
    ("models/embeddinggemma-300m", False),
    ("models/gemini-live-2.5-flash-preview", False),
    ("models/gemini-2.5-flash-native-audio-preview", False),
    ("models/gemini-2.5-flash-exp-native-audio-thinking-dialog", False),
    ("models/gemini-2.5-computer-use-preview-10-2025", False),
    ("models/gemini-robotics-er-1.5-preview", False),
    ("models/paligemma-3b-mix-224", False),
    ("models/shieldgemma-2-4b-it", False),
]


def fabrica_fake(modelos=None, erro: Exception | None = None):
    """`client_factory` falso; guarda a chave recebida para o teste conferir."""
    recebidas: list[str] = []

    def factory(api_key: str):
        recebidas.append(api_key)
        if erro is not None:
            raise erro
        return SimpleNamespace(models=SimpleNamespace(list=lambda: iter(modelos or MODELOS_API)))

    factory.chaves = recebidas  # type: ignore[attr-defined]
    return factory


async def _fake_conversation_factory():
    raise AssertionError("as rotas de modelos não montam conversa")


# --------------------------------------------------------------------------- módulo
def test_atualizar_filtra_grava_e_devolve(tmp_path):
    fabrica = fabrica_fake()
    dados = atualizar(tmp_path, "chave-123", client_factory=fabrica)

    assert fabrica.chaves == ["chave-123"]
    assert dados["modelos"] == [
        {"id": "gemini-2.5-flash", "nome": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-pro", "nome": "Gemini 2.5 Pro"},
    ]
    assert dados["atualizado_em"]

    gravado = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert gravado == dados
    assert set(gravado) == {"atualizado_em", "modelos"}
    assert [p.name for p in tmp_path.iterdir()] == ["models.json"]   # escrita atômica, sem .tmp


def test_atualizar_deixa_so_modelos_de_texto(tmp_path):
    """`generateContent` não basta: TTS, imagem, embedding, vídeo, live e afins também o declaram."""
    fabrica = fabrica_fake(modelos=[FakeModel(nome, None, ["generateContent"]) for nome, _ in MODELOS_REAIS])
    ids = [m["id"] for m in atualizar(tmp_path, "chave", client_factory=fabrica)["modelos"]]

    assert ids == [nome.removeprefix("models/") for nome, texto in MODELOS_REAIS if texto]
    for nome, texto in MODELOS_REAIS:
        assert e_modelo_de_texto(nome.removeprefix("models/")) is texto, nome


def test_filtro_de_texto_e_case_insensitive():
    assert e_modelo_de_texto("Gemini-2.5-Flash-Preview-TTS") is False
    assert e_modelo_de_texto("Gemini-2.5-Flash-IMAGE") is False
    assert e_modelo_de_texto("Gemini-2.5-Flash") is True


def test_atualizar_sem_modelo_de_texto_levanta(tmp_path):
    """Catálogo que só tem TTS/imagem é 400, não uma lista vazia no seletor."""
    fabrica = fabrica_fake(modelos=[
        FakeModel("models/gemini-2.5-flash-preview-tts", "TTS", ["generateContent"]),
        FakeModel("models/imagen-4.0-generate-001", "Imagen", ["generateContent"]),
    ])
    with pytest.raises(ModelsError):
        atualizar(tmp_path, "chave", client_factory=fabrica)
    assert not (tmp_path / "models.json").exists()


def test_atualizar_sem_chave_levanta(tmp_path):
    with pytest.raises(ModelsError) as exc:
        atualizar(tmp_path, None, client_factory=fabrica_fake())
    assert "GOOGLE_API_KEY" in str(exc.value)
    assert not (tmp_path / "models.json").exists()


def test_atualizar_com_falha_de_rede_levanta(tmp_path):
    fabrica = fabrica_fake(erro=ConnectionError("dns"))
    with pytest.raises(ModelsError) as exc:
        atualizar(tmp_path, "chave", client_factory=fabrica)
    assert "ConnectionError" in str(exc.value)


def test_atualizar_sem_modelo_util_levanta(tmp_path):
    fabrica = fabrica_fake(modelos=[FakeModel("models/veo-3", "Veo 3", ["predictLongRunning"])])
    with pytest.raises(ModelsError):
        atualizar(tmp_path, "chave", client_factory=fabrica)


def test_modelo_sem_display_name_usa_o_id(tmp_path):
    fabrica = fabrica_fake(modelos=[FakeModel("models/gemini-x", None, ["generateContent"])])
    dados = atualizar(tmp_path, "chave", client_factory=fabrica)
    assert dados["modelos"] == [{"id": "gemini-x", "nome": "gemini-x"}]


def test_listar_sem_arquivo_e_com_arquivo_corrompido(tmp_path):
    assert listar(tmp_path) is None
    (tmp_path / "models.json").write_text("{quebrado", encoding="utf-8")
    assert listar(tmp_path) is None


def test_listar_le_o_que_foi_gravado(tmp_path):
    atualizar(tmp_path, "chave", client_factory=fabrica_fake())
    dados = listar(tmp_path)
    assert [m["id"] for m in dados["modelos"]] == ["gemini-2.5-flash", "gemini-2.5-pro"]


# --------------------------------------------------------------------------- rotas
@pytest.fixture
def studio(tmp_path):
    store = ConfigStore(tmp_path)
    app = build_studio_app(store=store, conversation_factory=_fake_conversation_factory)
    app.state.models_client_factory = fabrica_fake()
    return TestClient(app), store


def test_get_models_sem_cache_devolve_o_modelo_efetivo(studio):
    client, store = studio
    resp = client.get("/api/models")
    assert resp.status_code == 200
    atual = store.param("settings.gemini_model")
    assert resp.json() == {"modelos": [{"id": atual, "nome": atual}], "atualizado_em": None, "cache": False}


def test_post_refresh_grava_o_cache_e_o_get_passa_a_usar(studio, monkeypatch):
    client, store = studio
    monkeypatch.setattr(app_mod, "settings", SimpleNamespace(google_api_key="chave"))

    resp = client.post("/api/models/refresh")
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["cache"] is True
    assert corpo["atualizado_em"]
    assert corpo["modelos"] == [
        {"id": "gemini-2.5-flash", "nome": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-pro", "nome": "Gemini 2.5 Pro"},
    ]
    assert (store.dir / "models.json").is_file()

    depois = client.get("/api/models").json()
    assert depois == corpo


def test_post_refresh_sem_chave_400(studio, monkeypatch):
    client, _ = studio
    monkeypatch.setattr(app_mod, "settings", SimpleNamespace(google_api_key=None))
    resp = client.post("/api/models/refresh")
    assert resp.status_code == 400
    assert "GOOGLE_API_KEY" in resp.json()["detail"]


def test_post_refresh_com_rede_fora_400(studio, monkeypatch):
    client, store = studio
    monkeypatch.setattr(app_mod, "settings", SimpleNamespace(google_api_key="chave"))
    client.app.state.models_client_factory = fabrica_fake(erro=TimeoutError("api fora"))
    resp = client.post("/api/models/refresh")
    assert resp.status_code == 400
    assert "detail" in resp.json()
    assert not (store.dir / "models.json").exists()   # falha não apaga nem cria cache


def test_selecao_do_modelo_continua_no_put_config(studio, monkeypatch):
    """O seletor grava override: `GEMINI_MODEL` do `.env` vira só fallback."""
    client, store = studio
    monkeypatch.setattr(app_mod, "settings", SimpleNamespace(google_api_key="chave"))
    client.post("/api/models/refresh")

    resp = client.put("/api/config", json={"gemini_model": "gemini-2.5-pro"})
    assert resp.status_code == 200
    assert store.param("settings.gemini_model") == "gemini-2.5-pro"
    assert store.effective("settings.gemini_model")["origem"] == "override"


def test_models_catalog_nao_importa_o_sdk_no_import():
    """O `google.genai` só é carregado quando alguém aperta Atualizar (import tardio)."""
    fonte = Path(models_catalog.__file__).read_text(encoding="utf-8")
    assert "from google import genai" in fonte
    assert not hasattr(models_catalog, "genai")
