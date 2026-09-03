"""Log estruturado da conversa, um arquivo JSONL por `conversation_id`.

Cada evento é uma linha JSON independente (append-only, flush imediato) para
sobreviver a crash do processo sem perder histórico. `data` sempre passa por
`pii.mask_obj` antes de tocar disco — o log é nosso, mas não é lugar de PII.

O NOME do arquivo também não é lugar de PII: `wa-<telefone>` vira `wa-<sha1(telefone)[:10]>`
(`pii.nome_arquivo_log`). O `conversation_id` dentro dos eventos continua sendo o id interno
(`wa-<número>`): o painel de Atendimentos e o takeover precisam dele para responder o lead.
É PII em claro por decisão, e por isso `logs/*.jsonl` fica fora do git e o gate
`scripts/check_logs_pii.py` só é verde para conversas de demonstração (`logs/entrega/`); um log real
de WhatsApp SEMPRE falha no gate — a proteção do número no repositório é o `.gitignore`, não a máscara
(`wa-<dígitos>`), que é o que o resto do sistema — takeover, Atendimentos, canal — usa.
Arquivo antigo com o nome em claro continua sendo o destino da conversa dele: renomear
histórico é outra tarefa (migração), e partir a conversa em dois arquivos seria pior.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.models import EventKind
from agent.pii import mask_obj, nome_arquivo_log


def _to_jsonable(valor: Any) -> Any:
    """Converte modelos Pydantic (inclusive aninhados em dict/list) para tipos JSON puros."""
    if isinstance(valor, BaseModel):
        return valor.model_dump(mode="json")
    if isinstance(valor, dict):
        return {k: _to_jsonable(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_to_jsonable(v) for v in valor]
    return valor


class ConversationLogger:
    def __init__(self, log_dir: Path, conversation_id: str, nome_arquivo: str | None = None) -> None:
        self.log_dir = Path(log_dir)
        self.conversation_id = conversation_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._escolher_path(nome_arquivo)

    def _escolher_path(self, nome_arquivo: str | None) -> Path:
        """Nome explícito manda; senão o derivado — com o arquivo legado tendo prioridade."""
        if nome_arquivo is not None:
            return self.log_dir / f"{nome_arquivo}.jsonl"
        legado = self.log_dir / f"{self.conversation_id}.jsonl"
        if legado.exists():
            return legado
        return self.log_dir / f"{nome_arquivo_log(self.conversation_id)}.jsonl"

    @property
    def path(self) -> Path:
        """Arquivo onde esta conversa é gravada (quem exporta/varre logs precisa saber)."""
        return self._path

    def event(
        self,
        event: EventKind,
        message_id: str | None = None,
        quote_id: str | None = None,
        **data: Any,
    ) -> None:
        linha = {
            "ts": datetime.now(UTC).isoformat(),
            "conversation_id": self.conversation_id,
            "event": event,
            "message_id": message_id,
            "quote_id": quote_id,
            "data": mask_obj(_to_jsonable(data)),
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
            f.flush()
