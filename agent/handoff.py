"""O que acontece quando a conversa vira handoff: avisar o consultor e passar a bola.

Até aqui o handoff só falava com o LEAD ("um consultor vai te chamar") e virava evento no log —
ninguém do outro lado ficava sabendo. Este módulo é o outro lado, em três canais independentes:

1. **takeover** — marca a conversa como humana (`config/atendimentos.json`). A partir daí o webhook
   do WhatsApp registra o `inbound` com `modo="humano"` e NÃO chama o agente: quem responde é a
   pessoa, pelo painel de Atendimentos.
2. **WhatsApp do consultor** — manda o resumo (`presenter.aviso_consultor`) para
   `tools.handoff.consultor_number`. É o único texto do sistema que pode citar preço para alguém
   que não é o lead: quem lê é quem vai fechar a venda.
3. **webhook** — `POST` opcional para um CRM, com o payload do handoff em JSON.

Regras: nenhum canal derruba o turno (cada um é try/except próprio e vira um evento
`handoff_notice` com `status`), cada canal é desligável por config, e no Lab o notificador roda em
modo `simulado` — grava o que TERIA sido enviado e não toca WhatsApp nem takeover.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from agent.config import settings
from agent.models import Handoff, LeadState
from agent.pii import mask_text
from agent.presenter import aviso_consultor
from agent.runtime_config import store as config_store

log = logging.getLogger("autoseguro.handoff")

TIMEOUT_WEBHOOK_S = 5.0


class HandoffNotifier:
    """Callable de `Conversation(on_handoff=...)`. Um por processo; nunca levanta."""

    def __init__(
        self,
        sender: Any = None,
        takeover: Any = None,
        store: Any = None,
        logger_factory: Any = None,
        *,
        log_dir: Path | None = None,
        client: Any = None,
        simulado: bool = False,
    ) -> None:
        self._sender = sender
        self._takeover = takeover
        self._store = store if store is not None else config_store
        if logger_factory is None:
            from agent.observability import ConversationLogger

            logger_factory = ConversationLogger
        self._logger_factory = logger_factory
        self._log_dir = Path(log_dir) if log_dir is not None else Path(settings.log_dir)
        self._client = client
        self._simulado = simulado

    # ------------------------------------------------------------------ entrada
    async def __call__(self, state: LeadState, action: Handoff) -> None:
        logger = self._logger_factory(self._log_dir, state.conversation_id)
        self._assumir(state, logger)
        await self._avisar_whatsapp(state, action, logger)
        await self._chamar_webhook(state, action, logger)

    def _param(self, nome: str) -> Any:
        try:
            return self._store.param(f"tools.handoff.{nome}")
        except Exception as exc:  # noqa: BLE001 — config torta não pode derrubar o turno
            log.warning("parâmetro handoff.%s ilegível (%s)", nome, type(exc).__name__)
            return None

    @staticmethod
    def _evento(logger: Any, canal: str, status: str, **extra: Any) -> None:
        logger.event("handoff_notice", canal=canal, status=status, **extra)

    # ------------------------------------------------------------------ 1. takeover
    def _assumir(self, state: LeadState, logger: Any) -> None:
        cid = state.conversation_id
        if self._simulado:
            self._evento(logger, "takeover", "simulado", destino=cid)
            return
        if self._takeover is None or not self._param("auto_assumir"):
            self._evento(logger, "takeover", "desligado", destino=cid)
            return
        try:
            self._takeover.assumir(cid)
        except Exception as exc:  # noqa: BLE001 — arquivo de config sem permissão, disco cheio...
            log.error("falha ao assumir %s (%s)", cid, type(exc).__name__)
            self._evento(logger, "takeover", "erro", destino=cid, erro=_curto(exc))
            return
        self._evento(logger, "takeover", "ok", destino=cid)

    # ------------------------------------------------------------------ 2. WhatsApp do consultor
    async def _avisar_whatsapp(self, state: LeadState, action: Handoff, logger: Any) -> None:
        numero = self._param("consultor_number")
        if not numero:
            self._evento(logger, "whatsapp", "desligado", destino=None)
            return
        texto = aviso_consultor(state, action, telefone=_telefone(state), link=self._link(state))
        destino = _destino(str(numero))
        if self._simulado or self._sender is None:
            # No Lab o operador vê o que teria ido para o consultor, sem mandar nada.
            self._evento(logger, "whatsapp", "simulado", destino=destino, texto=texto)
            return
        try:
            await self._sender.send_text(str(numero), texto)
        except Exception as exc:  # noqa: BLE001 — WhatsApp fora não derruba a conversa
            log.error("falha ao avisar o consultor (%s)", type(exc).__name__)
            self._evento(logger, "whatsapp", "erro", destino=destino, erro=_curto(exc))
            return
        self._evento(logger, "whatsapp", "ok", destino=destino)

    # ------------------------------------------------------------------ 3. webhook do CRM
    async def _chamar_webhook(self, state: LeadState, action: Handoff, logger: Any) -> None:
        url = self._param("webhook_url")
        if not url:
            self._evento(logger, "webhook", "desligado", destino=None)
            return
        destino = urlparse(str(url)).netloc or str(url)   # host, nunca a URL com token
        if self._simulado:
            self._evento(logger, "webhook", "simulado", destino=destino)
            return
        try:
            headers = self._headers()
            corpo = self._payload(state, action)
        except Exception as exc:  # noqa: BLE001 — `${env:X}` ausente é erro de configuração
            self._evento(logger, "webhook", "erro", destino=destino, erro=_curto(exc))
            return

        proprio = self._client is None
        http = self._client or httpx.AsyncClient()
        try:
            resp = await http.post(str(url), json=corpo, headers=headers or None, timeout=TIMEOUT_WEBHOOK_S)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 — CRM fora do ar não derruba a conversa
            log.error("falha no webhook de handoff (%s)", type(exc).__name__)
            self._evento(logger, "webhook", "erro", destino=destino, erro=_curto(exc))
            return
        finally:
            if proprio:
                await http.aclose()
        self._evento(logger, "webhook", "ok", destino=destino)

    def _headers(self) -> dict[str, str]:
        from agent.tools_runtime import resolver_env

        brutos = self._param("webhook_headers") or {}
        return {k: resolver_env(str(v)) for k, v in brutos.items()}

    def _payload(self, state: LeadState, action: Handoff) -> dict[str, Any]:
        """JSON do CRM: os dados JÁ estruturados do handoff, não o texto do WhatsApp."""
        payload = action.payload or {}
        return {
            "conversation_id": state.conversation_id,
            "origem": state.origem,
            "motivo": action.reason.value,
            "dados": payload.get("dados"),
            "cotacoes": payload.get("cotacoes"),
            "link": self._link(state),
            "ts": datetime.now(UTC).isoformat(),
        }

    def _link(self, state: LeadState) -> str:
        base = str(self._param("studio_url") or "").rstrip("/")
        return f"{base}/#atendimentos/{state.conversation_id}"


def _destino(numero: str) -> str:
    """Telefone do consultor NO LOG, sempre mascarado.

    `pii.mask_text` cobre o formato brasileiro; um número em outro formato (DDI estrangeiro,
    id da Evolution) não casaria com a regex e iria inteiro para o disco — daí o corte pelos
    4 últimos dígitos como rede de segurança.
    """
    mascarado = mask_text(numero)
    if re.search(r"\d{6,}", mascarado):
        return f"***{numero[-4:]}"
    return mascarado


def _telefone(state: LeadState) -> str:
    """O número do lead só existe quando a conversa é de WhatsApp (`wa-<digitos>`)."""
    cid = state.conversation_id
    return cid.removeprefix("wa-") if cid.startswith("wa-") else ""


def _curto(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]
