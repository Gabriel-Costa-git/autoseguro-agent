"""Goldens: com a configuração padrão, cada texto/prompt é byte-idêntico ao entregue.

Se um golden quebrar, ou o comportamento mudou de propósito (regenere com
`uv run python -c "from tests.golden_cases import casos; ..."` e explique no commit) ou
o refactor do Studio alterou um default sem querer — e é isso que este teste existe para pegar.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.golden_cases import casos

GOLDEN_DIR = Path(__file__).parent / "golden"
CASOS = casos()


@pytest.mark.parametrize("nome", sorted(CASOS))
def test_texto_igual_ao_golden(nome: str) -> None:
    esperado = (GOLDEN_DIR / f"{nome}.txt").read_text(encoding="utf-8")
    assert CASOS[nome] == esperado


def test_todos_os_goldens_tem_caso() -> None:
    arquivos = {p.stem for p in GOLDEN_DIR.glob("*.txt")}
    assert arquivos == set(CASOS)
