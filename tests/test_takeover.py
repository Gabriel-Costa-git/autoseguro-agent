"""Testes do interruptor "quem responde": `config/atendimentos.json`.

Tudo em `tmp_path` — o `config/` do repo nunca é tocado. O que importa aqui é o
contrato que o canal (`serve.py`) depende: ausência de arquivo = agente responde,
edição externa é vista sem reiniciar o processo e a escrita não deixa lixo nem
arquivo pela metade.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from agent.takeover import TakeoverStore
from tests.fakes import FakeLogger, logger_factory_unico

CID = "wa-5511999990000"

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _Loja:
    """`ConfigStore` mínimo: só `tools.handoff.auto_devolver_apos_min` interessa aqui."""

    def __init__(self, minutos: int | None = 240) -> None:
        self.minutos = minutos

    def param(self, path: str):
        assert path == "tools.handoff.auto_devolver_apos_min"
        return self.minutos


def _relogio(instantes: list[datetime]):
    """Relógio que anda pela lista: a última hora fica valendo para as chamadas seguintes."""
    def agora() -> datetime:
        return instantes[0] if len(instantes) == 1 else instantes.pop(0)
    return agora


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
    assert set(dados[CID]) == {"modo", "desde", "por", "ultima_humana"}
    assert dados[CID]["por"] == "operador"      # o padrão é o clique do operador


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


# --------------------------------------------------------------------------- devolução automática
def _store(tmp_path, agora, minutos=240, logger=None) -> TakeoverStore:
    return TakeoverStore(
        tmp_path,
        store=_Loja(minutos),
        logger_factory=logger_factory_unico(logger) if logger is not None else None,
        log_dir=tmp_path,
        agora=agora,
    )


def test_takeover_automatico_expira_e_volta_para_o_agente(tmp_path):
    """Handoff que ninguém foi atender não pode deixar o lead falando sozinho para sempre."""
    logger = FakeLogger(tmp_path, CID)
    loja = _store(tmp_path, _relogio([T0, T0 + timedelta(minutes=241)]), logger=logger)
    loja.assumir(CID, automatico=True)

    assert loja.is_humano(CID) is False              # passou de 240 min
    assert loja.listar() == {}                       # devolvida de fato, não só ignorada

    evento = next(e for e in logger.eventos() if e["event"] == "takeover_expirado")
    assert evento["data"]["por"] == "agente"
    assert evento["data"]["minutos"] == 240


def test_takeover_automatico_dentro_do_prazo_continua_humano(tmp_path):
    loja = _store(tmp_path, _relogio([T0, T0 + timedelta(minutes=239)]))
    loja.assumir(CID, automatico=True)
    assert loja.is_humano(CID) is True


def test_takeover_do_operador_nunca_expira(tmp_path):
    """Quem clicou em Assumir sabe o que fez; pode estar só demorando para responder."""
    loja = _store(tmp_path, _relogio([T0, T0 + timedelta(days=30)]))
    loja.assumir(CID)
    assert loja.is_humano(CID) is True


def test_mensagem_humana_reinicia_o_relogio(tmp_path):
    loja = _store(tmp_path, _relogio([T0, T0 + timedelta(minutes=200), T0 + timedelta(minutes=400)]))
    loja.assumir(CID, automatico=True)
    loja.registrar_humano(CID)                       # o operador respondeu aos 200 min
    assert loja.is_humano(CID) is True               # aos 400, faz 200 desde a última humana


def test_registrar_humano_em_conversa_nao_assumida_nao_faz_nada(tmp_path):
    loja = _store(tmp_path, _relogio([T0]))
    assert loja.registrar_humano(CID) == {}
    assert not (tmp_path / "atendimentos.json").exists()


def test_sem_parametro_de_devolucao_nada_expira(tmp_path):
    """`auto_devolver_apos_min` vazio = comportamento entregue (takeover é para sempre)."""
    loja = _store(tmp_path, _relogio([T0, T0 + timedelta(days=365)]), minutos=None)
    loja.assumir(CID, automatico=True)
    assert loja.is_humano(CID) is True


def test_entrada_antiga_sem_o_campo_por_nao_expira(tmp_path):
    """Arquivo escrito antes desta versão: sem `por`, trata como operador (conservador)."""
    (tmp_path / "atendimentos.json").write_text(
        json.dumps({CID: {"modo": "humano", "desde": "2020-01-01T00:00:00+00:00"}}), encoding="utf-8"
    )
    loja = _store(tmp_path, _relogio([T0]))
    assert loja.is_humano(CID) is True
