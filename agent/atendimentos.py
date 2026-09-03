"""Catálogo das conversas do agente, lido dos logs JSONL.

O `LeadState` é volátil (`InMemoryStateStore`) e o Studio roda em outro processo que o
canal — então a única fonte durável de "o que aconteceu com este lead" é o log de cada
conversa: `logs/*.jsonl` (produção) e `logs/studio/*.jsonl` (Lab), no formato de
`agent/observability.py`, já mascarado. `logs/entrega/` fica de fora: são cenários fixos
da entrega, não atendimento.

**O nome do arquivo não é mais o `conversation_id`.** Uma conversa de WhatsApp
mora em `wa-<sha1(telefone)[:10]>.jsonl` (`pii.nome_arquivo_log`): o telefone não fica no
disco onde não há máscara possível. O id verdadeiro está DENTRO de cada evento, e é ele
que o takeover e o canal usam — daí a leitura tirar o id dos eventos e cair para o nome do
arquivo só quando não houver evento nenhum (log vazio, ou de antes deste formato).

Este módulo só LÊ e resume — não decide nada e não escreve log. Quem responde cada
conversa é o `TakeoverStore` (`agent/takeover.py`), consultado na hora de montar o
resumo, porque isso muda sem o arquivo de log mudar.

Custo: um arquivo é relido só quando `(mtime, size)` mudam, então a lista da UI pode
fazer polling à vontade. Linha ilegível é ignorada (log é append-only com flush, mas a
última linha pode estar truncada se o processo morreu no meio da escrita).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.pii import nome_arquivo_log

# Prefixo do id → origem, para conversas anteriores ao campo `data.origem`.
ORIGEM_POR_PREFIXO = {"wa-": "whatsapp", "cli-": "cli", "lab-": "lab"}

# Etapas em que a conversa não anda mais sozinha (ver `Stage` em `agent/models.py`).
STAGES_TERMINAIS = frozenset({"encerrado_recusa", "handoff", "encerrado"})

EVENTOS_DE_MENSAGEM = ("inbound", "outbound")

# `conversation_id` vira nome de arquivo: nada de barra, `..` ou espaço.
_CID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,120}")

_EPOCA = datetime.min.replace(tzinfo=UTC)


def _ts(valor: Any) -> datetime | None:
    """ISO → datetime com fuso (o log grava em UTC; naive antigo é lido como UTC)."""
    if not isinstance(valor, str):
        return None
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def origem_inferida(conversation_id: str) -> str | None:
    """Origem de conversa antiga (sem `data.origem`): sai do prefixo do id."""
    for prefixo, origem in ORIGEM_POR_PREFIXO.items():
        if conversation_id.startswith(prefixo):
            return origem
    return None


@dataclass
class _Registro:
    """Um arquivo de log já lido: os eventos crus e o resumo que não depende do takeover."""

    conversation_id: str
    eventos: list[dict[str, Any]] = field(default_factory=list)
    base: dict[str, Any] = field(default_factory=dict)
    encerrado: bool = False
    ordem: datetime = _EPOCA


def _ler(path: Path) -> _Registro:
    eventos: list[dict[str, Any]] = []
    for linha in path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            evento = json.loads(linha)
        except ValueError:
            continue          # linha truncada (crash no meio da escrita) — ignora
        if isinstance(evento, dict):
            eventos.append(evento)
    return _resumir(_conversation_id(path, eventos), eventos)


def _conversation_id(path: Path, eventos: list[dict[str, Any]]) -> str:
    """O id vem dos eventos; o nome do arquivo é só o plano B.

    Com o nome hasheado, `path.stem` daria `wa-ab12cd34ef` — um id que o `TakeoverStore`
    e o canal não conhecem, então o painel mostraria o status errado e o link não abriria.
    """
    for evento in eventos:
        cid = evento.get("conversation_id")
        if isinstance(cid, str) and cid:
            return cid
    return path.stem


def _resumir(conversation_id: str, eventos: list[dict[str, Any]]) -> _Registro:
    origem = origem_inferida(conversation_id)
    nome: str | None = None
    lead_nome: str | None = None
    ultima_msg: str | None = None
    stage: str | None = None
    handoff_reason: str | None = None
    turnos = 0
    encerrado = False

    for evento in eventos:
        tipo = evento.get("event")
        data = evento.get("data")
        data = data if isinstance(data, dict) else {}
        if tipo == "inbound":
            turnos += 1
            if nome is None and data.get("sender_name"):
                nome = data["sender_name"]
        if data.get("origem"):
            origem = data["origem"]
        if tipo == "extraction" and data.get("lead_nome"):
            lead_nome = data["lead_nome"]
        if tipo in EVENTOS_DE_MENSAGEM and data.get("text"):
            ultima_msg = data["text"]
        if tipo == "decision" and data.get("stage"):
            stage = data["stage"]
        if tipo == "handoff":
            encerrado = True
            if data.get("reason"):
                handoff_reason = data["reason"]
        if tipo == "refusal":
            encerrado = True

    if stage in STAGES_TERMINAIS:
        encerrado = True

    inicio = eventos[0].get("ts") if eventos else None
    ultimo_ts = eventos[-1].get("ts") if eventos else None
    return _Registro(
        conversation_id=conversation_id,
        eventos=eventos,
        base={
            "conversation_id": conversation_id,
            "origem": origem,
            "nome": nome or lead_nome,
            "inicio": inicio,
            "ultimo_ts": ultimo_ts,
            "ultima_msg": ultima_msg,
            "turnos": turnos,
            "stage": stage,
            "handoff_reason": handoff_reason,
        },
        encerrado=encerrado,
        ordem=_ts(ultimo_ts) or _EPOCA,
    )


class Catalogo:
    def __init__(self, log_dir: Path, studio_log_dir: Path | None = None, takeover: Any = None) -> None:
        self.log_dir = Path(log_dir)
        self.studio_log_dir = Path(studio_log_dir) if studio_log_dir is not None else self.log_dir / "studio"
        self.takeover = takeover
        self._cache: dict[Path, tuple[tuple[float, int], _Registro]] = {}

    # ------------------------------------------------------------------ leitura
    def _arquivos(self) -> list[Path]:
        """Só o nível de cima de cada diretório: `logs/entrega/` nunca entra."""
        achados: list[Path] = []
        for diretorio in (self.log_dir, self.studio_log_dir):
            if diretorio.is_dir():
                achados.extend(sorted(diretorio.glob("*.jsonl")))
        return achados

    def _registro(self, path: Path) -> _Registro | None:
        try:
            st = path.stat()
        except OSError:
            self._cache.pop(path, None)
            return None
        assinatura = (st.st_mtime, st.st_size)
        hit = self._cache.get(path)
        if hit is not None and hit[0] == assinatura:
            return hit[1]
        try:
            registro = _ler(path)
        except OSError:
            return None
        self._cache[path] = (assinatura, registro)
        return registro

    def _path(self, conversation_id: str) -> Path | None:
        """Arquivo de uma conversa: o legado (nome em claro) ou o derivado (hash do número).

        Os dois existem em produção. A ordem é a MESMA do `ConversationLogger`: quando o
        arquivo antigo existe, é nele que a conversa continua sendo escrita, então é ele
        que o painel tem de abrir.
        """
        if not _CID_RE.fullmatch(conversation_id):
            return None
        nomes = dict.fromkeys((conversation_id, nome_arquivo_log(conversation_id)))
        for diretorio in (self.log_dir, self.studio_log_dir):
            for nome in nomes:
                path = diretorio / f"{nome}.jsonl"
                if path.is_file():
                    return path
        return None

    # ------------------------------------------------------------------ resumos
    def _status(self, registro: _Registro) -> str:
        if self.takeover is not None and self.takeover.is_humano(registro.conversation_id):
            return "humano"
        return "encerrado" if registro.encerrado else "agente"

    def _resumo(self, registro: _Registro) -> dict[str, Any]:
        base = registro.base
        return {
            "conversation_id": base["conversation_id"],
            "origem": base["origem"],
            "nome": base["nome"],
            "inicio": base["inicio"],
            "ultimo_ts": base["ultimo_ts"],
            "ultima_msg": base["ultima_msg"],
            "turnos": base["turnos"],
            "stage": base["stage"],
            "status": self._status(registro),
            "handoff_reason": base["handoff_reason"],
        }

    def resumo(self, conversation_id: str) -> dict[str, Any]:
        """Resumo de uma conversa. `KeyError` se não existe log dela."""
        registro = self._registro_de(conversation_id)
        return self._resumo(registro)

    def _registro_de(self, conversation_id: str) -> _Registro:
        path = self._path(conversation_id)
        registro = self._registro(path) if path is not None else None
        if registro is None:
            raise KeyError(conversation_id)
        return registro

    def listar(
        self, origem: str | None = None, status: str | None = None, q: str | None = None
    ) -> list[dict[str, Any]]:
        """Resumos ordenados do mais recente para o mais antigo, já filtrados.

        `origem` casa exato ou por canal (`whatsapp` pega `whatsapp:<instância>`);
        `q` é busca simples (sem acento removido) no id, no nome e na última mensagem.
        """
        itens: list[tuple[datetime, dict[str, Any]]] = []
        for path in self._arquivos():
            registro = self._registro(path)
            if registro is None:
                continue
            resumo = self._resumo(registro)
            if not _combina(resumo, origem, status, q):
                continue
            itens.append((registro.ordem, resumo))
        itens.sort(key=lambda par: par[0], reverse=True)
        return [resumo for _, resumo in itens]

    def transcricao(self, conversation_id: str, since: int = 0) -> dict[str, Any]:
        """Resumo + eventos a partir da linha `since` (polling incremental da UI)."""
        registro = self._registro_de(conversation_id)
        inicio = max(0, since)
        return {
            "resumo": self._resumo(registro),
            "eventos": registro.eventos[inicio:],
            "total": len(registro.eventos),
        }


def _combina(resumo: dict[str, Any], origem: str | None, status: str | None, q: str | None) -> bool:
    if origem:
        atual = resumo["origem"] or ""
        if atual != origem and not atual.startswith(f"{origem}:"):
            return False
    if status and resumo["status"] != status:
        return False
    if q:
        alvo = " ".join(
            str(resumo[campo] or "") for campo in ("conversation_id", "nome", "ultima_msg")
        ).lower()
        if q.lower() not in alvo:
            return False
    return True
