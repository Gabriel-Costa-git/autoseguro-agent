"""Log estruturado da conversa, um arquivo JSONL por `conversation_id`.

Cada evento é uma linha JSON independente (append-only, flush imediato) para
sobreviver a crash do processo sem perder histórico. `data` sempre passa por
`pii.mask_obj` antes de tocar disco — o log é nosso, mas não é lugar de PII.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.models import EventKind
from agent.pii import mask_obj


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
    def __init__(self, log_dir: Path, conversation_id: str) -> None:
        self.log_dir = Path(log_dir)
        self.conversation_id = conversation_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.log_dir / f"{conversation_id}.jsonl"

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
