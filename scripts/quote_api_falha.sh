#!/usr/bin/env bash
# Sobe o quote-service do desafio na porta 8001 sempre falhando (100% 5xx, nunca lento) —
# pra testar no Lab do Studio como o agente se comporta com a API de cotação indisponível
# (retry esgotando → INDISPONIVEL → handoff), sem esperar a taxa de falha real (20%) acontecer.
#
# Uso:
#   ./scripts/quote_api_falha.sh
#   uv run bash scripts/quote_api_falha.sh
#
# Rode a partir da raiz do repo autoseguro-agent (o caminho do quote-service é relativo a ela).
# Deixe rodando num terminal à parte; aponte o Studio pra http://localhost:8001 (tools.quote_client.base_url).
set -euo pipefail

QUOTE_SERVICE_DIR="../namastex-fde-challenge/quote-service"

if [ ! -d "$QUOTE_SERVICE_DIR" ]; then
    echo "erro: não achei $QUOTE_SERVICE_DIR — rode este script a partir da raiz do repo autoseguro-agent." >&2
    exit 1
fi

echo "subindo quote-service na porta 8001 com QUOTE_FAILURE_RATE=1 QUOTE_SLOW_RATE=0 (100% de 5xx, nunca lento)"
cd "$QUOTE_SERVICE_DIR"
QUOTE_FAILURE_RATE=1 QUOTE_SLOW_RATE=0 uv run uvicorn app.main:app --port 8001
