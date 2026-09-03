"""Mascaramento de PII para logs. Nada aqui decide negócio, só protege dados sensíveis
antes de tocar disco (ver `observability.py`, que aplica `mask_obj` em cada evento)."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
# Cobre "+55 (11) 91234-5678", "55 11 91234-5678" e a variante local sem
# código de país "(11) 91234-5678" — o prefixo "55" é opcional.
_TELEFONE_RE = re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-?\d{4}\b")
_PLACA_RE = re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b")
_CEP_RE = re.compile(r"\b(\d{5})-?(\d{3})\b")

# `wa-<telefone>` é o id interno da conversa; só o NOME DO ARQUIVO de log é derivado.
_CID_WHATSAPP_RE = re.compile(r"wa-(\d+)")


def mask_text(texto: str) -> str:
    """Mascara CPF, e-mail, telefone BR, placa e CEP em texto livre.

    A ordem importa: CPF antes de telefone evita que um CPF sem separador
    (11 dígitos) seja capturado pelo regex de telefone (também numérico).
    """
    texto = _CPF_RE.sub("***.***.***-**", texto)
    texto = _TELEFONE_RE.sub("+55 ** *****-****", texto)
    texto = _EMAIL_RE.sub(lambda m: f"***@{m.group(1)}", texto)
    texto = _PLACA_RE.sub("***-****", texto)
    texto = _CEP_RE.sub(lambda m: f"{m.group(1)}-***", texto)
    return texto


def mask_obj(obj: Any) -> Any:
    """Aplica `mask_text` recursivamente em dict/list/str; outros tipos ficam intactos.

    Idade e ano não são mascarados aqui de propósito: não são PII neste contexto
    (são insumos numéricos da cotação, úteis para debugar sem identificar ninguém).
    """
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(v) for v in obj]
    return obj


def nome_arquivo_log(conversation_id: str) -> str:
    """Nome (sem extensão) do arquivo de log de uma conversa: `wa-<sha1(numero)[:10]>`.

    O número mascarado continua DENTRO dos eventos; o que sai daqui é o nome no disco,
    onde não há máscara possível — um `ls logs/` mostrava a agenda telefônica inteira.
    Só `wa-<dígitos>` é derivado; `cli-*`, `lab-*` e ids sem telefone passam intactos.

    O hash é um identificador estável, não um segredo: 13 dígitos são força-bruta trivial
    para quem tiver o arquivo. Ele tira o telefone da listagem do diretório, do backup e da
    captura de tela — não substitui permissão de diretório.
    """
    m = _CID_WHATSAPP_RE.fullmatch(conversation_id)
    if m is None:
        return conversation_id
    digest = hashlib.sha1(m.group(1).encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"wa-{digest[:10]}"
