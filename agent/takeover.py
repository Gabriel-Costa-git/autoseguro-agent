"""Quem responde cada conversa: o agente ou um humano.

Arquivo `config/atendimentos.json`, uma chave por conversa assumida:

    {"wa-5511999990000": {"modo": "humano", "desde": "2026-09-02T20:00:00+00:00"}}

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("autoseguro.takeover")

ARQUIVO = "atendimentos.json"
MODO_HUMANO = "humano"


class TakeoverStore:
    def __init__(self, config_dir: Path) -> None:
        self.dir = Path(config_dir)
        self.path = self.dir / ARQUIVO
        self._cache: tuple[tuple[float, int], dict[str, Any]] | None = None

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
        entrada = self.listar().get(conversation_id)
        return bool(entrada) and entrada.get("modo") == MODO_HUMANO

    # ------------------------------------------------------------------ escrita
    def _gravar(self, dados: dict[str, Any]) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._cache = (self._assinatura(), dados)
        return dados

    def assumir(self, conversation_id: str) -> dict[str, Any]:
        """Marca a conversa como humana (idempotente: reassumir não mexe no `desde`)."""
        dados = dict(self.listar())
        if dados.get(conversation_id, {}).get("modo") != MODO_HUMANO:
            dados[conversation_id] = {"modo": MODO_HUMANO, "desde": datetime.now(UTC).isoformat()}
            self._gravar(dados)
        return self.listar()

    def devolver(self, conversation_id: str) -> dict[str, Any]:
        """Devolve a conversa ao agente (idempotente: devolver o que não foi assumido não faz nada)."""
        dados = dict(self.listar())
        if conversation_id in dados:
            del dados[conversation_id]
            self._gravar(dados)
        return self.listar()
