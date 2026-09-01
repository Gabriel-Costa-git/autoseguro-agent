# AutoSeguro Agent — Desafio FDE Namastex

Agente de vendas de seguro auto por chat: qualifica o lead, cota na API de cotação
(instável de propósito) **sem travar nem inventar preço**, e escala para um humano
com critério explícito.

> **Status: em construção.** Este README será completado na entrega com as decisões
> de engenharia e seus porquês.

## Como rodar

```bash
# 1. API de cotação (repo do desafio, clonado ao lado)
cd ../namastex-fde-challenge && docker compose up --build -d

# 2. Agente
uv run python -m agent.chat
```

## Estrutura

- `agent/` — código do agente (cliente resiliente, qualificação, loop conversacional, observabilidade)
- `tests/` — testes determinísticos (usam `QUOTE_SEED` / taxas de falha forçadas)
- `logs/` — logs estruturados de execução (JSONL, PII mascarado)
- `ai-logs/` — sessões de IA usadas na construção (exigência do desafio)

## Decisões (a preencher na entrega)

- Timeout, retry e classificação de erros do cliente da `/quote`
- Critérios de handoff para humano (enumerados, com porquê de negócio)
- Pré-validação local com as regras do `GET /planos`
- Mascaramento de PII na borda do log
