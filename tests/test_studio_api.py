"""Testes da API HTTP do Studio (prompts/tools/config/effective).

`ConfigStore(tmp_path)` injetado — nunca toca `config/` do repo. `conversation_factory`
é um fake trivial (o Lab é do executor C; aqui só garantimos que o app aceita a injeção
sem quebrar o boot).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.defaults import SLOTS
from agent.runtime_config import ConfigStore
from agent.studio.app import build_studio_app


async def _fake_conversation_factory():
    raise AssertionError("não deveria ser chamado nos testes da API de prompts/tools/config")


@pytest.fixture
def client(tmp_path) -> TestClient:
    store = ConfigStore(tmp_path)
    app = build_studio_app(store=store, conversation_factory=_fake_conversation_factory)
    return TestClient(app)


# --------------------------------------------------------------------------- prompts
def test_get_prompts_lista_todos_os_slots_com_default_ativo(client: TestClient):
    resp = client.get("/api/prompts")
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert set(slots.keys()) == set(SLOTS.keys())
    for key, definicao in SLOTS.items():
        slot = slots[key]
        assert slot["active"] == "default"
        assert slot["versions"]["default"]["text"] == definicao["default"]
        assert slot["label"] == definicao["label"]
        assert slot["grupo"] == definicao["grupo"]


def test_post_versao_cria_e_ativa(client: TestClient):
    resp = client.post("/api/prompts/fallback.idade/versions", json={"name": "v1", "text": "novo texto"})
    assert resp.status_code == 200
    slot = resp.json()
    assert slot["active"] == "v1"
    assert slot["versions"]["v1"]["text"] == "novo texto"
    assert "default" in slot["versions"]  # default continua existindo, intocado


def test_post_versao_activate_false_nao_ativa(client: TestClient):
    resp = client.post(
        "/api/prompts/fallback.idade/versions",
        json={"name": "rascunho", "text": "x", "activate": False},
    )
    assert resp.status_code == 200
    assert resp.json()["active"] == "default"


def test_put_versao_edita(client: TestClient):
    client.post("/api/prompts/fallback.idade/versions", json={"name": "v1", "text": "primeira"})
    resp = client.put("/api/prompts/fallback.idade/versions/v1", json={"text": "segunda"})
    assert resp.status_code == 200
    assert resp.json()["versions"]["v1"]["text"] == "segunda"


def test_put_versao_default_e_imutavel_400(client: TestClient):
    resp = client.put("/api/prompts/fallback.idade/versions/default", json={"text": "mexeu"})
    assert resp.status_code == 400


def test_put_active_troca_versao_ativa(client: TestClient):
    client.post("/api/prompts/fallback.idade/versions", json={"name": "v1", "text": "x", "activate": False})
    resp = client.put("/api/prompts/fallback.idade/active", json={"name": "v1"})
    assert resp.status_code == 200
    assert resp.json()["active"] == "v1"

    resp = client.put("/api/prompts/fallback.idade/active", json={"name": "default"})
    assert resp.status_code == 200
    assert resp.json()["active"] == "default"


def test_delete_versao_recusa_default_400(client: TestClient):
    resp = client.delete("/api/prompts/fallback.idade/versions/default")
    assert resp.status_code == 400


def test_delete_versao_recusa_ativa_400(client: TestClient):
    client.post("/api/prompts/fallback.idade/versions", json={"name": "v1", "text": "x"})  # fica ativa
    resp = client.delete("/api/prompts/fallback.idade/versions/v1")
    assert resp.status_code == 400


def test_delete_versao_inativa_funciona(client: TestClient):
    client.post("/api/prompts/fallback.idade/versions", json={"name": "v1", "text": "x", "activate": False})
    resp = client.delete("/api/prompts/fallback.idade/versions/v1")
    assert resp.status_code == 200
    assert "v1" not in resp.json()["versions"]


def test_slot_inexistente_404(client: TestClient):
    resp = client.post("/api/prompts/nao-existe/versions", json={"name": "x", "text": "y"})
    assert resp.status_code == 404

    resp = client.put("/api/prompts/nao-existe/active", json={"name": "default"})
    assert resp.status_code == 404

    resp = client.put("/api/prompts/nao-existe/versions/default", json={"text": "x"})
    assert resp.status_code == 404

    resp = client.delete("/api/prompts/nao-existe/versions/default")
    assert resp.status_code == 404


def test_versao_inexistente_404(client: TestClient):
    resp = client.put("/api/prompts/fallback.idade/versions/nao-existe", json={"text": "x"})
    assert resp.status_code == 404
    resp = client.put("/api/prompts/fallback.idade/active", json={"name": "nao-existe"})
    assert resp.status_code == 404
    resp = client.delete("/api/prompts/fallback.idade/versions/nao-existe")
    assert resp.status_code == 404


def test_placeholder_desconhecido_400(client: TestClient):
    resp = client.post(
        "/api/prompts/fallback.idade/versions",
        json={"name": "v1", "text": "oi {campo_que_nao_existe}"},
    )
    assert resp.status_code == 400
    assert "placeholder" in resp.json()["detail"]


# --------------------------------------------------------------------------- tools / config / effective
def test_put_tools_override_e_effective_mostra_origem(client: TestClient):
    resp = client.put("/api/tools", json={"quote_client": {"timeout_s": 9.0}})
    assert resp.status_code == 200
    assert resp.json()["quote_client"]["timeout_s"] == 9.0

    resp = client.get("/api/effective")
    assert resp.status_code == 200
    campo = resp.json()["tools"]["quote_client"]["timeout_s"]
    assert campo["value"] == 9.0
    assert campo["origem"] == "override"
    assert campo["default"] != 9.0  # default do código/`.env`, não o override

    resp = client.get("/api/tools")
    assert resp.status_code == 200
    assert resp.json()["quote_client"]["timeout_s"] == 9.0


def test_delete_tools_volta_ao_padrao(client: TestClient):
    client.put("/api/tools", json={"quote_client": {"timeout_s": 9.0}})
    resp = client.delete("/api/tools/quote_client/timeout_s")
    assert resp.status_code == 200
    assert "quote_client" not in resp.json() or "timeout_s" not in resp.json().get("quote_client", {})

    resp = client.get("/api/effective")
    campo = resp.json()["tools"]["quote_client"]["timeout_s"]
    # não assume "default" puro: a máquina pode ter QUOTE_TIMEOUT_S no .env (aí origem é "env:...")
    assert campo["origem"] != "override"
    assert campo["value"] == campo["default"]


def test_put_config_responder_history_runs(client: TestClient):
    resp = client.put("/api/config", json={"responder_history_runs": 12})
    assert resp.status_code == 200
    assert resp.json()["responder_history_runs"] == 12

    resp = client.get("/api/effective")
    assert resp.json()["settings"]["responder_history_runs"]["value"] == 12
    assert resp.json()["settings"]["responder_history_runs"]["origem"] == "override"


def test_put_config_valor_invalido_400(client: TestClient):
    resp = client.put("/api/config", json={"responder_history_runs": -5})
    assert resp.status_code == 400
    assert "detail" in resp.json()


# --------------------------------------------------------------------------- estáticos / saúde
def test_get_raiz_serve_index(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="app"' in resp.text


def test_health(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "studio": True}


# --------------------------------------------------------------------------- isolamento do canal Evolution
def test_serve_nao_importa_studio():
    """`agent/serve.py` monta o canal Evolution — o Studio nunca pode entrar nesse processo."""
    fonte = Path("agent/serve.py").read_text(encoding="utf-8")
    assert "studio" not in fonte.lower()


def test_studio_main_usa_host_fixo_127_0_0_1():
    fonte = Path("agent/studio/__main__.py").read_text(encoding="utf-8")
    assert '"127.0.0.1"' in fonte
    assert "0.0.0.0" not in fonte
