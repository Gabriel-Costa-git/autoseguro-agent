# Backlog — AutoSeguro agent

Itens conhecidos, ainda não planejados. Ordem = prioridade sugerida. Ao iniciar um item, vira frente com brief em `ai-logs/briefs/`.

1. **Cotação de múltiplos veículos** (reportado pelo Gabriel em 02/09/2026, teste manual): o lead pede cotação de mais de um carro e o agente
   não lida bem com o pedido. Hoje `LeadState` guarda UM veículo (`veiculo_texto`/`veiculo_ano`) e a policy cota um por vez.
   Investigar: como o Extractor reage a "quero cotar dois carros" / lista de veículos; como a policy deveria enfileirar (cotar um, oferecer o próximo)
   ou recusar com clareza; impacto nos goldens e no presenter. Reproduzir no Lab antes de desenhar.
