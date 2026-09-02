"""Testes do interruptor "quem responde": `config/atendimentos.json`.

Tudo em `tmp_path` — o `config/` do repo nunca é tocado. O que importa aqui é o
contrato que o canal (`serve.py`) depende: ausência de arquivo = agente responde,
edição externa é vista sem reiniciar o processo e a escrita não deixa lixo nem
arquivo pela metade.
"""
from __future__ import annotations

import json

from agent.takeover import TakeoverStore

CID = "wa-5511999990000"


def test_arquivo_ausente_e_mapa_vazio(tmp_path):
    loja = TakeoverStore(tmp_path)
    assert loja.listar() == {}
    assert loja.is_humano(CID) is False
    assert not (tmp_path / "atendimentos.json").exists()   # ler não cria arquivo


def test_assumir_e_devolver_roundtrip(tmp_path):
    loja = TakeoverStore(tmp_path)

    loja.assumir(CID)
    assert loja.is_humano(CID) is True
    entrada = loja.listar()[CID]
    assert entrada["modo"] == "humano"
    assert entrada["desde"]

    # outra conversa segue com o agente
    assert loja.is_humano("wa-5511888880000") is False

    loja.devolver(CID)
    assert loja.is_humano(CID) is False
    assert loja.listar() == {}


def test_assumir_e_idempotente_e_devolver_o_que_nao_foi_assumido_nao_quebra(tmp_path):
    loja = TakeoverStore(tmp_path)
    loja.assumir(CID)
    desde = loja.listar()[CID]["desde"]
    loja.assumir(CID)
    assert loja.listar()[CID]["desde"] == desde

    loja.devolver("wa-000")     # nunca assumida
    assert loja.is_humano(CID) is True


def test_arquivo_no_disco_tem_o_formato_do_contrato(tmp_path):
    TakeoverStore(tmp_path).assumir(CID)
    dados = json.loads((tmp_path / "atendimentos.json").read_text(encoding="utf-8"))
    assert list(dados) == [CID]
    assert set(dados[CID]) == {"modo", "desde"}


def test_hot_reload_ve_edicao_externa(tmp_path):
    """O Studio escreve; o `serve.py` (outro processo, outra instância) tem de enxergar."""
    canal = TakeoverStore(tmp_path)
    studio = TakeoverStore(tmp_path)

    assert canal.is_humano(CID) is False    # popula o cache do canal
    studio.assumir(CID)
    assert canal.is_humano(CID) is True

    studio.devolver(CID)
    assert canal.is_humano(CID) is False


def test_escrita_e_atomica_e_nao_deixa_tmp(tmp_path):
    loja = TakeoverStore(tmp_path)
    loja.assumir(CID)
    loja.devolver(CID)
    assert [p.name for p in sorted(tmp_path.iterdir())] == ["atendimentos.json"]
    assert json.loads((tmp_path / "atendimentos.json").read_text(encoding="utf-8")) == {}


def test_arquivo_corrompido_vale_vazio(tmp_path):
    """Log/config quebrado não pode calar o agente: sem takeover legível, ele responde."""
    (tmp_path / "atendimentos.json").write_text("{isso não é json", encoding="utf-8")
    loja = TakeoverStore(tmp_path)
    assert loja.listar() == {}
    assert loja.is_humano(CID) is False


def test_listar_devolve_copia(tmp_path):
    loja = TakeoverStore(tmp_path)
    loja.assumir(CID)
    mapa = loja.listar()
    mapa.clear()
    assert loja.is_humano(CID) is True
