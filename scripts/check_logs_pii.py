"""Gate da entrega: falha (exit 1) se algum log JSONL tiver PII em claro.

Procura CPF, e-mail, telefone BR, placa e CEP completo (8 dígitos) — os mesmos
padrões que `agent/pii.py` mascara. Uso: `uv run python scripts/check_logs_pii.py logs/entrega/*.jsonl`
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PADROES = {
    "cpf": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "telefone": re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9\d{4}-?\d{4}\b"),
    "placa": re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b"),
    "cep_completo": re.compile(r"\b\d{5}-?\d{3}\b"),
}


def main(paths: list[str]) -> int:
    achados = 0
    arquivos = [p for a in paths for p in (Path().glob(a) if any(c in a for c in "*?[") else [Path(a)])]
    for arq in arquivos:
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            for nome, rx in PADROES.items():
                for m in rx.finditer(linha):
                    achados += 1
                    print(f"{arq}:{n}: {nome} em claro: {m.group(0)}")
    print(f"{len(arquivos)} arquivo(s) verificado(s), {achados} ocorrência(s) de PII em claro")
    return 1 if achados else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["logs/entrega/*.jsonl"]))
