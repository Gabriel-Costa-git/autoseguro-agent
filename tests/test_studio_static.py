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


def test_index_tem_a_aba_tools_reorganizada(client: TestClient):
    """Tools virou lista (Integrações) + detalhe, com o popover de nova tool; policy e rules
    passaram para Config, que agora tem o container das fichas de `tools.*`."""
    resp = client.get("/")
    assert resp.status_code == 200
    for elemento in ("tl-itens", "tl-detalhe", "tl-nova", "tl-nova-btn", "tl-nova-nome", "tl-nova-tipo", "tl-sem-suporte"):
        assert f'id="{elemento}"' in resp.text
    assert 'id="config-tools-cards"' in resp.text


def test_app_js_tem_a_ficha_do_canal(client: TestClient):
    """`tools.canal` (os freios do WhatsApp) precisa estar em Integrações: sem o grupo na
    lista e sem a ficha, os dois parâmetros existem na API e não têm onde ser mexidos."""
    app_js = client.get("/static/app.js").text
    assert '"quote_client", "viacep", "handoff", "canal"' in app_js
    for campo in ("max_respostas_por_minuto", "debounce_s", "auto_devolver_apos_min"):
        assert campo in app_js


def test_app_js_tem_a_ficha_de_handoff(client: TestClient):
    """A ficha de `tools.handoff` e o evento `handoff_notice` são desenhados por JS (não há id
    fixo no HTML), então o que dá para travar aqui é o app.js servido trazer os dois."""
    app_js = client.get("/static/app.js").text
    assert '"quote_client", "viacep", "handoff"' in app_js  # entra na lista de Integrações
    for campo in ("auto_assumir", "consultor_number", "webhook_url", "webhook_headers", "studio_url"):
        assert campo in app_js
    assert "handoff_notice" in app_js
    assert ".badge-ev-handoff_notice" in client.get("/static/style.css").text
