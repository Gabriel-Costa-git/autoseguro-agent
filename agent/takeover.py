"""Quem responde cada conversa: o agente ou um humano.

Arquivo `config/atendimentos.json`, uma chave por conversa assumida:

    {"wa-5511999990000": {"modo": "humano", "desde": "...", "por": "agente", "ultima_humana": null}}

`por` diz QUEM assumiu: `operador` (clicou em Atendimentos) ou `agente` (handoff automático).
A distinção existe por causa da devolução automática: um takeover que o agente marcou e que
ninguém foi atender vira uma conversa morta — passados `tools.handoff.auto_devolver_apos_min`
minutos sem mensagem humana, o agente volta a responder (evento `takeover_expirado`). O que o
operador assumiu na mão NÃO expira: ele sabe o que fez, e pode estar só levando tempo.

Ausência do arquivo (o caso normal) = `{}` = o agente responde tudo, exatamente como
na entrega. Dois processos diferentes tocam este arquivo — o Studio escreve (o operador
clica "Assumir") e o `serve.py` lê a cada mensagem que chega — por isso a leitura é com
hot-reload por mtime (barata) e a escrita é atômica (`tmp` + `os.replace`), o mesmo
padrão do `ConfigStore` em `agent/runtime_config.py`.

Fora deste módulo não existe estado: quem quiser saber quem responde chama `is_humano`
na hora. Arquivo corrompido não derruba o canal — vale `{}` (o agente responde) e o
operador vê o efeito na hora em que a conversa volta a ser respondida.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("autoseguro.takeover")

ARQUIVO = "atendimentos.json"
MODO_HUMANO = "humano"
POR_AGENTE = "agente"        # handoff automático (expira)
POR_OPERADOR = "operador"    # alguém clicou em Assumir (não expira)


class TakeoverStore:
    def __init__(
        self,
        config_dir: Path,
        *,
        store: Any = None,
        logger_factory: Any = None,
        log_dir: Path | None = None,
        agora: Any = None,
    ) -> None:
        self.dir = Path(config_dir)
        self.path = self.dir / ARQUIVO
        self._cache: tuple[tuple[float, int], dict[str, Any]] | None = None
        self._store = store                    # ConfigStore; None = o global, lido na hora
        self._logger_factory = logger_factory  # onde gravar `takeover_expirado`
        self._log_dir = log_dir
        self._agora = agora if agora is not None else (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ leitura
    def _assinatura(self) -> tuple[float, int]:
        """`(mtime, size)` do arquivo; `(-1, -1)` quando ele não existe."""
        try:
            st = self.path.stat()
        except OSError:
            return (-1.0, -1)
        return (st.st_mtime, st.st_size)

    def listar(self) -> dict[str, Any]:
        """Mapa `conversation_id -> {modo, desde}` (cópia). Recarrega sozinho se o arquivo mudou."""
        assinatura = self._assinatura()
        if self._cache is not None and self._cache[0] == assinatura:
            return dict(self._cache[1])
        dados: dict[str, Any] = {}
        if assinatura != (-1.0, -1):
            try:
                bruto = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("%s ilegível (%s): tratando como vazio", self.path.name, type(exc).__name__)
                bruto = None
            if isinstance(bruto, dict):
                dados = {cid: v for cid, v in bruto.items() if isinstance(v, dict)}
        self._cache = (assinatura, dados)
        return dict(dados)

    def is_humano(self, conversation_id: str) -> bool:
        """Quem responde AGORA. Também é onde o takeover automático esquecido expira: a
        pergunta acontece a cada mensagem que chega, então não há timer nem processo extra.
        """
        entrada = self.listar().get(conversation_id)
        if not entrada or entrada.get("modo") != MODO_HUMANO:
            return False
        if self._expirou(entrada):
            self._expirar(conversation_id, entrada)
            return False
        return True

    # ------------------------------------------------------------------ expiração
    def _minutos_para_devolver(self) -> int | None:
        try:
            store = self._store
            if store is None:
                from agent.runtime_config import store as store_global

                store = store_global
            valor = store.param("tools.handoff.auto_devolver_apos_min")
        except Exception as exc:  # noqa: BLE001 — config torta não pode calar nem soltar a conversa
            log.warning("auto_devolver_apos_min ilegível (%s)", type(exc).__name__)
            return None
        return int(valor) if valor else None

    def _expirou(self, entrada: dict[str, Any]) -> bool:
        if entrada.get("por") != POR_AGENTE:
            return False                      # operador assumiu na mão: não expira
        minutos = self._minutos_para_devolver()
        if not minutos:
            return False
        marco = _quando(entrada.get("ultima_humana")) or _quando(entrada.get("desde"))
        if marco is None:
            return False                      # sem data legível, não devolve por conta própria
        return self._agora() - marco > timedelta(minutes=minutos)

    def _expirar(self, conversation_id: str, entrada: dict[str, Any]) -> None:
        self.devolver(conversation_id)
        try:
            self._logger(conversation_id).event(
                "takeover_expirado",
                desde=entrada.get("desde"),
                ultima_humana=entrada.get("ultima_humana"),
                minutos=self._minutos_para_devolver(),
                por=entrada.get("por"),
            )
        except Exception as exc:  # noqa: BLE001 — log é observabilidade, não pode travar o canal
            log.error("falha ao registrar takeover_expirado de %s (%s)", conversation_id, type(exc).__name__)

    def _logger(self, conversation_id: str) -> Any:
        if self._logger_factory is not None:
            return self._logger_factory(self._log_dir, conversation_id)
        from agent.config import settings
        from agent.observability import ConversationLogger

        return ConversationLogger(self._log_dir or settings.log_dir, conversation_id)

    # ------------------------------------------------------------------ escrita
    def _gravar(self, dados: dict[str, Any]) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._cache = (self._assinatura(), dados)
        return dados

    def assumir(self, conversation_id: str, *, automatico: bool = False) -> dict[str, Any]:
        """Marca a conversa como humana (idempotente: reassumir não mexe no `desde`).

        `automatico=True` é o handoff do agente — o único que a devolução automática pega.
        O padrão (`False`) é o clique do operador, que fica até ele devolver.
        """
        dados = dict(self.listar())
        if dados.get(conversation_id, {}).get("modo") != MODO_HUMANO:
            dados[conversation_id] = {
                "modo": MODO_HUMANO,
                "desde": self._agora().isoformat(),
                "por": POR_AGENTE if automatico else POR_OPERADOR,
                "ultima_humana": None,
            }
            self._gravar(dados)
        return self.listar()

    def registrar_humano(self, conversation_id: str) -> dict[str, Any]:
        """Marca que um humano falou agora: reinicia o relógio da devolução automática.

        Quem chama é o painel de Atendimentos, ao enviar uma mensagem pelo operador.
        Conversa não assumida é ignorada (não faz sentido cronometrar o que não existe).
        """
        dados = dict(self.listar())
        entrada = dados.get(conversation_id)
        if not entrada or entrada.get("modo") != MODO_HUMANO:
            return self.listar()
        entrada = dict(entrada)
        entrada["ultima_humana"] = self._agora().isoformat()
        dados[conversation_id] = entrada
        self._gravar(dados)
        return self.listar()

    def devolver(self, conversation_id: str) -> dict[str, Any]:
        """Devolve a conversa ao agente (idempotente: devolver o que não foi assumido não faz nada)."""
        dados = dict(self.listar())
        if conversation_id in dados:
            del dados[conversation_id]
            self._gravar(dados)
        return self.listar()


def _quando(valor: Any) -> datetime | None:
    """ISO → datetime com fuso; naive (arquivo escrito à mão) vale como UTC."""
    if not isinstance(valor, str):
        return None
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
