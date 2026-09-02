"""Rotas do registro de tools do painel (`agent/studio/custom_tools_api.py`).

`ConfigStore(tmp_path)` injetado e `app.state.tools_client` com `httpx.MockTransport`: nada de
rede, nada de LLM e nada tocando `config/` do repo.
"""
from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.runtime_config import ConfigStore
from agent.studio.app import build_studio_app

TOOL = {
    "tipo": "http",
    "descricao": "Consulta a apólice do cliente pelo CPF.",
    "instrucoes": "Confirme só os 4 últimos dígitos.",
    "parametros": {"cpf": {"tipo": "string", "descricao": "CPF só dígitos", "obrigatorio": True}},
    "http": {
        "metodo": "GET",
        "url": "https://api.exemplo.test/apolices/{cpf}",
        "headers": {"Authorization": "Bearer ${env:APOLICE_KEY}"},
    },
}


async def _fake_conversation_factory():
    raise AssertionError("as rotas de tools não montam conversa")


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"numero": "AP-1", "status": "ativa"})


@pytest.fixture
def studio(tmp_path):
    store = ConfigStore(tmp_path / "config")
    app = build_studio_app(store=store, conversation_factory=_fake_conversation_factory)
    app.state.tools_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return TestClient(app), store


# --------------------------------------------------------------------------- listar / salvar
def test_lista_vazia_e_arquivo_criado_no_boot(studio):
    client, store = studio
    resp = client.get("/api/custom-tools")
    assert resp.status_code == 200
    assert resp.json() == {"tools": {}}
    assert (store.dir / "custom_tools.json").is_file()


def test_put_cria_e_get_devolve_env_literal(studio):
    client, _ = studio
    resp = client.put("/api/custom-tools/consulta_apolice", json=TOOL)
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["nome"] == "consulta_apolice"
    assert corpo["tipo"] == "http" and corpo["sql"] is None
    assert corpo["criado_em"] and corpo["atualizado_em"]

    tools = client.get("/api/custom-tools").json()["tools"]
    # o valor do segredo NUNCA aparece: a API devolve a referência como está no registro
    assert tools["consulta_apolice"]["http"]["headers"]["Authorization"] == "Bearer ${env:APOLICE_KEY}"


def test_put_atualiza_a_mesma_tool(studio):
    client, _ = studio
    client.put("/api/custom-tools/consulta_apolice", json=TOOL)
    resp = client.put("/api/custom-tools/consulta_apolice", json={**TOOL, "enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert len(client.get("/api/custom-tools").json()["tools"]) == 1


@pytest.mark.parametrize(
    "corpo",
    [
        {**TOOL, "descricao": ""},
        {**TOOL, "http": {"metodo": "GET", "url": "https://x/{nao_declarado}"}},
        {**TOOL, "tipo": "sql", "sql": {"conexao": "x.db", "query": "DELETE FROM apolices", "max_linhas": 5}},
        {**TOOL, "http": {"metodo": "GET", "url": "https://x", "headers": {"A": "${env:minusculo}"}}},
    ],
)
def test_put_invalido_400_com_detail(studio, corpo):
    client, _ = studio
    resp = client.put("/api/custom-tools/consulta_apolice", json=corpo)
    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_put_com_nome_divergente_400(studio):
    client, _ = studio
    resp = client.put("/api/custom-tools/consulta_apolice", json={**TOOL, "nome": "outra_coisa"})
    assert resp.status_code == 400


def test_put_nome_invalido_400(studio):
    client, _ = studio
    assert client.put("/api/custom-tools/Nome-Invalido", json=TOOL).status_code == 400


# --------------------------------------------------------------------------- apagar
def test_delete_apaga_e_404_depois(studio):
    client, _ = studio
    client.put("/api/custom-tools/consulta_apolice", json=TOOL)
    resp = client.delete("/api/custom-tools/consulta_apolice")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert client.get("/api/custom-tools").json() == {"tools": {}}
    assert client.delete("/api/custom-tools/consulta_apolice").status_code == 404


# --------------------------------------------------------------------------- testar
def test_testar_executa_sem_llm_e_sem_gravar_log(studio, tmp_path, monkeypatch):
    monkeypatch.setenv("APOLICE_KEY", "segredo-do-cofre")
    client, _ = studio
    client.put("/api/custom-tools/consulta_apolice", json=TOOL)

    resp = client.post("/api/custom-tools/consulta_apolice/testar", json={"args": {"cpf": "12345678901"}})
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["ok"] is True
    assert json.loads(corpo["resultado"]) == {"numero": "AP-1", "status": "ativa"}
    assert isinstance(corpo["latency_ms"], int)
    assert corpo["erro"] is None
    # o botão Testar não escreve conversa nenhuma
    assert not list(tmp_path.glob("**/*.jsonl"))


def test_testar_com_erro_devolve_ok_false(studio, monkeypatch):
    monkeypatch.delenv("APOLICE_KEY", raising=False)
    client, _ = studio
    client.put("/api/custom-tools/consulta_apolice", json=TOOL)

    corpo = client.post("/api/custom-tools/consulta_apolice/testar", json={"args": {"cpf": "1"}}).json()
    assert corpo["ok"] is False
    assert "APOLICE_KEY" in corpo["erro"]


def test_testar_sem_parametro_obrigatorio(studio, monkeypatch):
    monkeypatch.setenv("APOLICE_KEY", "x")
    client, _ = studio
    client.put("/api/custom-tools/consulta_apolice", json=TOOL)
    corpo = client.post("/api/custom-tools/consulta_apolice/testar", json={"args": {}}).json()
    assert corpo["ok"] is False and "cpf" in corpo["erro"]


def test_testar_tool_sql_de_verdade(studio, tmp_path):
    client, _ = studio
    banco = tmp_path / "apolices.db"
    con = sqlite3.connect(banco)
    con.execute("CREATE TABLE apolices (cpf TEXT, numero TEXT)")
    con.execute("INSERT INTO apolices VALUES ('123', 'AP-7')")
    con.commit()
    con.close()

    client.put("/api/custom-tools/consulta_sql", json={
        "tipo": "sql",
        "descricao": "Apólices do CPF.",
        "parametros": {"cpf": {"tipo": "string", "obrigatorio": True}},
        "sql": {"conexao": str(banco), "query": "SELECT numero FROM apolices WHERE cpf = :cpf", "max_linhas": 5},
    })
    corpo = client.post("/api/custom-tools/consulta_sql/testar", json={"args": {"cpf": "123"}}).json()
    assert corpo["ok"] is True
    assert json.loads(corpo["resultado"]) == [{"numero": "AP-7"}]


def test_testar_tool_inexistente_404(studio):
    client, _ = studio
    assert client.post("/api/custom-tools/nao_existe/testar", json={"args": {}}).status_code == 404


# --------------------------------------------------------------------------- env
def test_env_devolve_so_nomes(studio, monkeypatch):
    monkeypatch.setenv("APOLICE_KEY", "segredo-do-cofre")
    monkeypatch.setenv("minuscula_ignorada", "x")
    client, _ = studio

    resp = client.get("/api/custom-tools/env")
    assert resp.status_code == 200
    nomes = resp.json()["vars"]
    assert "APOLICE_KEY" in nomes
    assert "minuscula_ignorada" not in nomes
    assert nomes == sorted(nomes)
    assert "segredo-do-cofre" not in resp.text     # nenhum VALOR de ambiente na resposta
