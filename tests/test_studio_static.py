"""Testes dos estáticos do Studio: confere que os arquivos são servidos, que
`index.html` referencia `app.js`/`style.css` e que o shell traz as abas e sub-abas
— o conteúdo/comportamento da UI em si não é testável sem um browser real (é o que o
reporte descreve em texto).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.runtime_config import ConfigStore
from agent.studio.app import build_studio_app


async def _fake_conversation_factory():
    raise AssertionError("não deveria ser chamado nestes testes")


@pytest.fixture
def client(tmp_path) -> TestClient:
    store = ConfigStore(tmp_path)
    app = build_studio_app(store=store, conversation_factory=_fake_conversation_factory)
    return TestClient(app)


def test_index_e_servido_na_raiz(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<div id=\"app\"" in resp.text


def test_index_referencia_app_js_e_style_css(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert '/static/app.js' in resp.text
    assert '/static/style.css' in resp.text


def test_app_js_e_servido_em_static(client: TestClient):
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"] or "ecmascript" in resp.headers["content-type"]
    assert "AutoSeguro Studio" in resp.text


def test_style_css_e_servido_em_static(client: TestClient):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    assert ".shell" in resp.text


def test_index_tem_abas_de_topo_e_sub_abas_do_lab(client: TestClient):
    """Hierarquia v2: 3 abas de topo (`data-tab`) e as 3 sub-abas do Lab (`data-sub`) —
    é por esses atributos que o roteador do app.js marca a ativa."""
    resp = client.get("/")
    assert resp.status_code == 200
    for aba in ("atendimentos", "lab", "config"):
        assert f'data-tab="{aba}"' in resp.text
    for sub in ("conversa", "prompts", "tools"):
        assert f'data-sub="{sub}"' in resp.text


def test_markdown_js_e_servido_em_static(client: TestClient):
    """O preview da aba Prompts importa `markdown.js`: se ele não for servido, o módulo
    `app.js` inteiro quebra no import e a UI não sobe."""
    resp = client.get("/static/markdown.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"] or "ecmascript" in resp.headers["content-type"]
    assert "renderMarkdown" in resp.text
    assert "./markdown.js" in client.get("/static/app.js").text


def test_index_tem_a_aba_atendimentos(client: TestClient):
    """A aba Atendimentos precisa dos ganchos que o módulo do app.js procura: lista, filtros,
    detalhe (transcrição + composer) e o painel lateral de eventos/estado."""
    resp = client.get("/")
    assert resp.status_code == 200
    for elemento in (
        "at-itens",
        "at-vazio",
        "at-indisponivel",
        "at-status",
        "at-origem",
        "at-busca",
        "at-conversa",
        "at-cid",
        "at-takeover",
        "at-mensagens",
        "at-composer",
        "at-eventos",
        "at-estado",
    ):
        assert f'id="{elemento}"' in resp.text
