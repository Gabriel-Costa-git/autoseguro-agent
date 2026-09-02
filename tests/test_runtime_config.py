"""ConfigStore: defaults = comportamento entregue, precedência, versões, hot-reload."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent.defaults import SLOTS
from agent.runtime_config import (
    DEFAULT_VERSION,
    ConfigError,
    ConfigStore,
    render_template,
)


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    s = ConfigStore(tmp_path / "config")
    s.ensure_files()
    return s


# ---------------------------------------------------------------- defaults
def test_ensure_files_cria_os_tres_arquivos(store: ConfigStore) -> None:
    assert {p.name for p in store.dir.glob("*.json")} == {"prompts.json", "tools.json", "settings.json"}


def test_todo_slot_do_codigo_existe_com_default_igual(store: ConfigStore) -> None:
    prompts = store.prompts()
    for key, d in SLOTS.items():
        slot = prompts.slots[key]
        assert slot.active == DEFAULT_VERSION
        assert slot.versions[DEFAULT_VERSION].text == d["default"]
        assert store.text(key) == d["default"] if not d["placeholders"] else True


def test_tools_e_settings_sem_override_vem_do_codigo(store: ConfigStore) -> None:
    ef = store.effective("tools.quote_client.timeout_s")
    assert ef["value"] == 3.5 and ef["origem"] in ("default", "env:QUOTE_TIMEOUT_S")
    assert store.param("settings.responder_history_runs") == 8
    assert store.param("tools.rules.pre_validacao_local") is True
    assert store.param("tools.viacep.enabled") is True


def test_snapshot_cobre_todas_as_chaves(store: ConfigStore) -> None:
    snap = store.snapshot()
    assert set(snap["tools"]) == {"quote_client", "viacep", "policy", "rules"}
    assert "gemini_model" in snap["settings"]
    assert snap["tools"]["quote_client"]["timeout_s"]["default"] == 3.5


# ---------------------------------------------------------------- overrides
def test_override_tem_precedencia_e_volta_ao_padrao(store: ConfigStore) -> None:
    store.set_overrides("tools", {"quote_client": {"timeout_s": 9.0}})
    assert store.effective("tools.quote_client.timeout_s") == {"value": 9.0, "origem": "override", "default": 3.5}
    # outras chaves não são tocadas
    assert store.effective("tools.quote_client.max_attempts")["origem"] != "override"
    store.clear_override("tools.quote_client.timeout_s")
    assert store.effective("tools.quote_client.timeout_s")["origem"] != "override"
    assert json.loads((store.dir / "tools.json").read_text())["quote_client"] == {}


def test_override_invalido_e_rejeitado(store: ConfigStore) -> None:
    with pytest.raises(ConfigError):
        store.set_overrides("tools", {"quote_client": {"max_attempts": 0}})
    with pytest.raises(ConfigError):
        store.set_overrides("settings", {"responder_history_runs": "oito"})
    with pytest.raises(ConfigError):
        store.effective("tools.nao.existe")


def test_settings_override(store: ConfigStore) -> None:
    store.set_overrides("settings", {"gemini_model": "gemini-x", "responder_history_runs": 2})
    assert store.param("settings.gemini_model") == "gemini-x"
    assert store.param("settings.responder_history_runs") == 2


# ---------------------------------------------------------------- versões de prompt
def test_criar_versao_ativa_e_aplica_na_hora(store: ConfigStore) -> None:
    key = "policy.txt_midia"
    store.add_version(key, "v2", "Manda por texto, por favor.", note="teste")
    assert store.text(key) == "Manda por texto, por favor."
    store.set_active(key, DEFAULT_VERSION)
    assert store.text(key) == SLOTS[key]["default"]


def test_default_e_imutavel_e_nao_apagavel(store: ConfigStore) -> None:
    key = "policy.txt_midia"
    with pytest.raises(ConfigError):
        store.edit_version(key, DEFAULT_VERSION, "x")
    with pytest.raises(ConfigError):
        store.delete_version(key, DEFAULT_VERSION)
    with pytest.raises(ConfigError):
        store.add_version(key, DEFAULT_VERSION, "x")


def test_nao_apaga_versao_ativa_e_edita_versao(store: ConfigStore) -> None:
    key = "fallback.padrao"
    store.add_version(key, "v2", "a")
    with pytest.raises(ConfigError):
        store.delete_version(key, "v2")
    store.edit_version(key, "v2", "b")
    assert store.text(key) == "b"
    store.set_active(key, DEFAULT_VERSION)
    store.delete_version(key, "v2")
    assert "v2" not in store.slot(key).versions


def test_placeholder_desconhecido_e_rejeitado(tmp_path: Path) -> None:
    slots = {"t.x": {"label": "x", "grupo": "g", "placeholders": ["nome"], "default": "Oi {nome}"}}
    s = ConfigStore(tmp_path / "c", slots=slots)
    s.ensure_files()
    assert s.text("t.x", nome="Ana") == "Oi Ana"
    with pytest.raises(ConfigError):
        s.add_version("t.x", "v2", "Oi {nome} {sobrenome}")
    s.add_version("t.x", "v2", "Olá, {nome}!")
    assert s.text("t.x", nome="Ana") == "Olá, Ana!"


def test_render_template_e_seguro_com_placeholder_ausente() -> None:
    assert render_template("a {b} c", {}) == "a {b} c"
    assert render_template("sem chaves", {"b": 1}) == "sem chaves"


# ---------------------------------------------------------------- hot-reload
def test_edicao_externa_do_arquivo_e_recarregada(store: ConfigStore) -> None:
    key = "policy.txt_aguarde"
    antes = store.text(key)
    path = store.dir / "prompts.json"
    dados = json.loads(path.read_text(encoding="utf-8"))
    dados["slots"][key]["versions"]["externa"] = {"text": "Peraí.", "note": "", "created_at": "2026-01-01T00:00:00+00:00"}
    dados["slots"][key]["active"] = "externa"
    path.write_text(json.dumps(dados), encoding="utf-8")
    novo = time.time() + 5
    os.utime(path, (novo, novo))  # garante mtime diferente mesmo em FS de baixa resolução
    assert store.text(key) == "Peraí." != antes


def test_sync_slots_recompoe_default_alterado_no_arquivo(store: ConfigStore) -> None:
    key = "policy.txt_aguarde"
    path = store.dir / "prompts.json"
    dados = json.loads(path.read_text(encoding="utf-8"))
    dados["slots"][key]["versions"][DEFAULT_VERSION]["text"] = "adulterado"
    path.write_text(json.dumps(dados), encoding="utf-8")
    novo = time.time() + 5
    os.utime(path, (novo, novo))
    assert store.text(key) == SLOTS[key]["default"]


def test_arquivo_corrompido_da_erro_claro(store: ConfigStore) -> None:
    (store.dir / "tools.json").write_text("{nope", encoding="utf-8")
    store._cache.clear()
    with pytest.raises(ConfigError):
        store.param("tools.quote_client.timeout_s")
