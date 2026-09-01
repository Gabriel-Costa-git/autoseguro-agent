"""Mascaramento de PII para logs. Nada aqui decide negócio, só protege dados sensíveis
antes de tocar disco (ver `observability.py`, que aplica `mask_obj` em cada evento)."""
from __future__ import annotations

import re
from typing import Any

_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
# Cobre "+55 (11) 91234-5678", "55 11 91234-5678" e a variante local sem
# código de país "(11) 91234-5678" — o prefixo "55" é opcional.
_TELEFONE_RE = re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-?\d{4}\b")
_PLACA_RE = re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b")
_CEP_RE = re.compile(r"\b(\d{5})-?(\d{3})\b")


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
