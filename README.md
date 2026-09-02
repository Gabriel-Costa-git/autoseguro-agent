# AutoSeguro Agent — Desafio FDE Namastex

Agente de vendas de seguro auto por chat (WhatsApp). Conversa com o lead, qualifica
(idade, carro, CEP, plano), **cota na API de cotação instável sem travar nem inventar
preço** e **escala para um humano com critério explícito e auditável**.

O agente não fecha a venda: não existe API de contratação, então quando o lead aceita a
cotação um consultor humano recebe os dados e a cotação prontos para emitir.

## Como rodar

```bash
# 1. API de cotação do desafio (repo clonado ao lado)
cd ../namastex-fde-challenge && docker compose up --build -d   # http://localhost:8000

# 2. Dependências e configuração
cd ../autoseguro-agent
uv sync
cp .env.example .env            # preencha GOOGLE_API_KEY (Gemini)

# 3. Conversa no terminal (gera logs/<conversation_id>.jsonl)
uv run python -m agent.chat
uv run python -m agent.chat --script scripts/roteiro-feliz.txt --delay 15   # roteiro sem interação

# 4. Testes (sem rede, sem LLM, sem docker)
uv run pytest -q

# 5. Canal WhatsApp (Evolution API v2) — opcional, exige instância pareada
uv run python -m agent.serve    # webhook em POST /webhook, porta $PORT (3000)
```

Comandos do chat: `/estado` (estado mascarado), `/audio` (simula mensagem de mídia), `/sair`.

## Arquitetura em uma frase

**O LLM extrai dados e conversa; código decide.** Cada turno passa por uma máquina de
estados pura que escolhe a próxima ação; o modelo nunca decide cotar, recusar ou escalar,
e nunca escreve um preço.

```
canal (CLI | Evolution) ──Inbound──▶ conversation.handle
                                        │
                     brain.Extractor ◀──┤  (Gemini, saída estruturada: campos + intent)
                                        ▼
                                  policy.next_action  ── função pura ──▶ [Action...]
                                        │
          ┌─────────────────────────────┼──────────────────────────────┐
          ▼                             ▼                              ▼
   presenter.render            brain.Responder                 quote_client.quote
   (templates: cotação,        (Gemini, só para perguntar      (retry + timeout +
    recusa, handoff)            e conversar; guardrail          taxonomia de erro)
                                anti-preço)
          └─────────────────────────────┴──────────────────────────────┘
                                        ▼
                     observability (JSONL por conversa, PII mascarada)
```

| Módulo | Responsabilidade |
|---|---|
| `agent/models.py` | Contratos: `LeadState`, `Extraction`, `Action`, `HandoffReason`, `QuoteResult`. |
| `agent/quote_client.py` | Cliente resiliente da `/quote`: timeout, retry com backoff e jitter, orçamento total, classificação de erro. |
| `agent/rules.py` | Pré-validação **derivada** do `GET /planos` (nada hardcoded). |
| `agent/cep.py` | Consulta suave ao ViaCEP para confirmar cidade/UF. |
| `agent/policy.py` | Máquina de estados: coleta, cotação, recusa, handoff. Pura e testada sem LLM. |
| `agent/presenter.py` | Templates das mensagens determinísticas (cotação, recusa, handoff). |
| `agent/brain.py` | Extractor e Responder em agno + Gemini; guardrail anti-preço. |
| `agent/conversation.py` | Orquestra o turno e emite os eventos de log. |
| `agent/observability.py`, `agent/pii.py` | Log JSONL com ids; mascaramento de PII na borda. |
| `agent/channels/cli.py`, `agent/channels/evolution.py` | Adaptadores de canal (terminal e WhatsApp). |
| `scripts/export_ai_logs.py` | Exporta e higieniza as sessões de IA para `ai-logs/`. |
| `agent/runtime_config.py`, `agent/defaults.py`, `config/` | Textos e parâmetros editáveis com versões, overrides e hot-reload (Studio). |
| `agent/studio/` | Studio local: Lab, Prompts, Tools, Config (FastAPI + estático, só 127.0.0.1). |

## Decisões e porquês

### 1. O preço nunca sai do modelo
- A mensagem de cotação é um **template renderizado por código** a partir do JSON da API
  (`presenter.py`). O LLM não recebe o valor: o resumo de estado que entra no prompt diz
  apenas `cotação: ok`, nunca o prêmio.
- O Responder tem um **guardrail mecânico** (`brain.guard_price`): se a saída contiver `R$`,
  `1.234,56` ou "reais" sem uma cotação bem-sucedida no estado, a resposta é trocada por um
  fallback determinístico e o evento fica no log. Roda como `post_hook` do agno (antes de
  entrar no histórico da sessão) e de novo na chamada, porque a regra não pode depender de
  um hook.
- Prompt proíbe valores, descontos e promessas. Prompt é empurrão; o template e o guardrail
  são a garantia.

### 2. Como a `/quote` falha, e o que o cliente faz
Lendo o código do serviço e medindo 60 chamadas: por chamada, 20 % devolvem 5xx e 10 %
dormem exatos 8 s **e depois respondem normalmente**. A latência é bimodal (50 ms ou 8 s),
as falhas são independentes entre chamadas e `POST /quote` é pura e idempotente. Disso:

| Resposta | Classificação | Ação |
|---|---|---|
| 200 | `OK` | apresentar |
| 500/502/503 | infra | retry |
| lenta (> 3,5 s) | infra (timeout) | retry (a chamada abandonada não tem efeito colateral) |
| 422 `cotacao_recusada` | `RECUSA` (regra de negócio) | sem retry; informar o lead |
| 422 `detail` (Pydantic) ou 400 `payload_invalido` | `BUG` (erro nosso) | sem retry; handoff `ERRO_INTERNO` |
| esgotou tentativas/orçamento | `INDISPONIVEL` | handoff `COTACAO_INDISPONIVEL` com os dados coletados |

Política: **timeout 3,5 s por tentativa, até 4 tentativas, backoff exponencial com jitter
±50 %, orçamento total de 15 s.** Esperar os 8 s garantiria a resposta da chamada lenta, mas
cortar em 3,5 s e tentar de novo custa menos em média (70 % de chance de resposta em 50 ms).
O preço disso é contar o timeout como falha: com 3 tentativas, 0,3³ = 2,7 % das cotações
escalariam por infra; com 4, 0,3⁴ ≈ 0,8 %. No primeiro timeout o lead recebe "só um instante".
`/health` é sempre OK e não reflete a instabilidade, então não serve como sinal.

### 3. Pré-validação local é atalho, não contrato
`rules.py` deriva limites (idade 18–75, veículo com até 20 anos pelo ano corrente) das
regras publicadas em `GET /planos`. Serve para responder na hora sem gastar chamada, mas a
API continua a autoridade: o 422 de recusa é tratado do mesmo jeito. Ano do veículo maior
que o corrente vira pergunta ("é ano-modelo? qual o de fabricação?"), porque em 2026 um
carro 2005 é recusado e um 2006 aceito, e a fronteira muda em 1º de janeiro.

A API **não valida CEP** (um CEP inválido é aceito com multiplicador 1,0, subprecificando em
silêncio). Por isso o agente valida o formato localmente e confirma cidade/UF via ViaCEP uma
vez. ViaCEP é validação suave: timeout de 2 s, e se cair a cotação segue. Lead sem CEP é
cotado sem CEP com aviso de que o valor é estimativa e **pode subir**.

### 4. Critério de handoff (explícito, enumerado, testado)
`HandoffReason` em `models.py`; cada um tem teste em `tests/test_policy.py`:

| Motivo | Quando | Por quê |
|---|---|---|
| `LEAD_ACEITOU` | lead quer fechar a cotação apresentada | não há API de contratação; humano emite com dados + cotação + `quote_id` |
| `LEAD_PEDIU_HUMANO` | pediu atendente, em qualquer etapa | respeitar sempre |
| `COTACAO_INDISPONIVEL` | `/quote` esgotou tentativas | verdade ao lead, dados guardados, humano cota depois |
| `ERRO_INTERNO` | 400/422 de validação ou exceção inesperada | bug nosso; o lead não paga por ele |
| `FORA_DE_ESCOPO` | sinistro, apólice existente, outro produto | o agente só vende seguro auto novo |
| `NEGOCIACAO` | pedido explícito de desconto, ou 2ª objeção de preço | desconto é decisão comercial |
| `SEM_PROGRESSO` | 3 turnos sem avançar a coleta | insistir irrita; humano destrava |

**Recusa de negócio não é handoff.** Idade fora da faixa ou veículo velho demais recebem uma
resposta honesta ("não temos plano para o seu perfil"), agradecimento e encerramento. No
dataset do desafio, 11 % dos leads têm mais de 75 anos e 21 % têm carro com mais de 20 anos;
mandar todos para a fila humana seria desperdício.

### 5. Rastreabilidade
A API não devolve nenhum id, então o agente gera os seus. Cada conversa vira um
`logs/<conversation_id>.jsonl` com eventos `inbound`, `extraction`, `decision`,
`quote_attempt` (um por tentativa, com `quote_id`, status HTTP, latência e classificação),
`quote_result`, `cep_lookup`, `llm_call`, `outbound`, `handoff`, `refusal`, `error`. Toda
mensagem tem `message_id`; toda saída aponta `in_reply_to`.

### 6. Dados sensíveis
- O agente **não pede CPF, e-mail, telefone nem placa**: a API não usa nada disso. O dataset
  mostra o vendedor pedindo CPF; é dado desnecessário para cotar.
- PII que o lead manda por conta própria é mascarada **na borda do log** (`pii.py`): CPF,
  e-mail, telefone, placa. O CEP é insumo da cotação, então o log guarda o prefixo e mascara
  os três últimos dígitos. O ViaCEP devolve endereço completo, mas só cidade/UF entram no
  estado e no log.
- `scripts/export_ai_logs.py` remove valores do `.env`, chaves Google e `apikey` das sessões
  exportadas, e `--check` falha se sobrar algo.

### 7. Sobre o dataset de conversas
Ele é sintético e gerado por template: o preço dito pelo vendedor é sorteado e as coberturas
citadas não batem com os planos, então **não serve como referência de preço ou cobertura**.
Foi usado para tom, fluxo, taxonomia de objeções e padrões de PII. Os timestamps não são
monotônicos; a ordem correta é `message_index`.

### 8. Framework e modelo
agno 3.x com Gemini (`gemini-3.5-flash-lite`, configurável por `GEMINI_MODEL`) faz duas coisas: extração com saída estruturada
(sem histórico, temperatura 0, contexto e última pergunta no prompt para desambiguar
"sim"/"35"/"2019") e resposta conversacional com histórico por sessão em SQLite. Tudo que
decide está fora do framework, em Python puro, para ser testável e trocável.

O provedor de LLM é **outra dependência instável**, e foi tratado como a `/quote`: a primeira
rodada real estourou a cota gratuita do Gemini (5 req/min e 20 req/dia no `gemini-2.5-flash`),
o agno não re-tenta por conta própria e devolve o erro dentro do `RunOutput` em vez de levantar.
Por isso `brain.py` tem retry que honra o `retryDelay` do provedor (backoff 2/4/8 s quando ele
não diz, até 3 novas tentativas, 30 s de orçamento) e degradação honesta: extração indisponível
vira "não consegui ler sua mensagem, pode repetir?" em vez de repetir a última pergunta, e três
seguidas escalam para humano. O CLI tem `--delay` para roteiros dentro da cota gratuita.

### 9. Canal é adaptador
O núcleo expõe `Conversation.handle(inbound, emit)`. O CLI é o canal de desenvolvimento e
gera o log da entrega. O adaptador Evolution (`channels/evolution.py`) traduz o webhook
`messages.upsert`, ignora grupos e mensagens próprias, responde 200 imediatamente, processa
em background com um lock por conversa e envia "digitando" antes de cada resposta. Mídia sem
texto (áudio, imagem, documento) recebe pedido para escrever.

## Testes
`uv run pytest -q` roda 172 testes sem rede, sem LLM e sem docker: transporte HTTP mockado
para `/quote` e ViaCEP, relógio e sleep injetáveis para o retry, `FakeLLM` para o turno,
policy e presenter puros. Há um teste que faz grep no `presenter.py` para garantir que nenhum
valor de preço está fixo em template.

## Log de uma execução completa
Três execuções reais (Gemini `gemini-3.5-flash-lite` + API de cotação em docker), geradas pelo
CLI em modo roteiro e commitadas em `logs/entrega/`:

| Arquivo | Cenário | Desfecho |
|---|---|---|
| `logs/entrega/caminho-feliz.jsonl` | `scripts/roteiro-feliz.txt`: saudação → idade (com CPF, e-mail e telefone não solicitados) → Onix 2022 → CEP confirmado via ViaCEP → escolhe Completo → cotação → "fechado" | `present` com R$ 209,90/mês vindo da API, depois `handoff` `lead_aceitou` |
| `logs/entrega/recusa-negocio.jsonl` | `scripts/roteiro-recusa.txt`: lead de 78 anos | `refusal` honesto, sem handoff, sem chamada à API |
| `logs/entrega/quote-indisponivel-handoff.jsonl` | mesmo roteiro feliz contra a API com `QUOTE_FAILURE_RATE=1` | 4 `quote_attempt` (500/502/503/502) em 2,9 s → `quote_result` `indisponivel` → `handoff` `cotacao_indisponivel`, **nenhum valor na conversa** |

Cada linha do JSONL é um evento com `ts`, `conversation_id`, `event`, `message_id`, `quote_id`
e `data`. O trecho do handoff do caminho feliz (resumido) mostra o que o consultor humano recebe:

```json
{"event": "handoff", "message_id": "m8",
 "data": {"reason": "lead_aceitou",
          "payload": {"dados": {"idade": 35, "veiculo_texto": "Onix 2022", "veiculo_ano": 2022,
                                "cep": "01310-***", "cep_cidade": "São Paulo", "cep_uf": "SP",
                                "plano_id": "completo", "data_inicio": null},
                      "cotacao": {"quote_id": "q…", "outcome": "ok",
                                  "quote": {"plano_nome": "Completo", "premio_mensal": 209.9,
                                            "franquia": 3000.0, "carencia_dias": 30, "...": "..."},
                                  "attempts": [{"attempt": 1, "status": "ok", "http_status": 200, "latency_ms": 20}]},
                      "motivo": "lead_aceitou", "conversation_id": "demo-feliz-v3"}}}
```

A mensagem com CPF, e-mail e telefone aparece no log já mascarada (`***.***.***-**`,
`***@gmail.com`, `+55 ** *****-****`), porque a máscara roda na borda do log. A degradação
honesta quando o próprio LLM falha ("não consegui ler sua mensagem, pode repetir?") apareceu
em rodadas de desenvolvimento sob a cota gratuita e está coberta por teste; o CLI em modo
roteiro reenvia a linha quando o agente pede.

**Gate de PII da entrega:** `uv run python scripts/check_logs_pii.py "logs/entrega/*.jsonl"`
varre os três logs por CPF, e-mail, telefone, placa e CEP completo e falha se achar qualquer um
em claro. Resultado na entrega: `3 arquivo(s) verificado(s), 0 ocorrência(s) de PII em claro`.

## Transparência de uso de IA
Construído com Claude Code orquestrando executores paralelos (Orq). Em `ai-logs/`:
`briefs/` (as instruções dadas a cada executor), `reports/` (o que cada um entregou e decidiu)
e `sessions/` (transcripts `.jsonl` exportados e higienizados por `scripts/export_ai_logs.py`).

## Studio (edição e teste local do agente)

```bash
uv run python -m agent.studio          # http://127.0.0.1:8765
scripts/quote_api_falha.sh             # opcional: API de cotação com falha forçada na 8001
```

Painel de operador, só em `127.0.0.1`, fora do canal Evolution (`agent/serve.py` não sabe que
ele existe). Tema dark, sem framework, sem bundler e sem nada vindo de CDN: HTML, CSS e dois
módulos ES servidos do disco. Uma barra superior de 56 px carrega a marca, o breadcrumb
(`Prompts / <slot>`, `Lab / <sessão>`) e as quatro abas como links segmentados, com o
indicador de saúde à direita; o hash da URL (`#lab`, `#prompts`, `#tools`, `#config`) é quem
manda. Lab e "Testar prompt" dividem o mesmo componente de chat e a MESMA sessão do Lab —
uma por aba do navegador, que sobrevive ao reload em vez de abrir outra. Quatro abas:

- **Lab** — conversa como lead usando o mesmo `Conversation.handle` da entrega (nunca uma
  cópia). O chat ocupa o centro (bolhas do lead à direita, do agente à esquerda com a etiqueta
  `template`/`llm`) e o painel de 380 px à direita tem três sub-abas: **Eventos** ao vivo do
  turno (extração, decisão da policy, cada tentativa da `/quote` com status e latência, ViaCEP,
  handoff), **Contexto** — o payload exato de cada chamada ao modelo, com as instruções
  renderizadas, o histórico enviado, a entrada e a saída — e **Estado**, o `LeadState` da
  sessão. Clicar numa bolha seleciona o turno. Na barra do chat, o seletor de API de cotação
  (docker na 8000, falha forçada na 8001) mostra a URL em uso pela sessão.
- **Prompts** — uma página de editor: dropdown de slot com busca (`/` foca) e itens agrupados,
  dropdown de versão, selo `Ativa`/`Rascunho`/`Default · imutável`, e os botões **Ativar** e
  **Salvar** (`Cmd/Ctrl+S`). O corpo alterna entre editor mono, preview de markdown e os dois
  lado a lado; os chips dos placeholders do slot inserem `{campo}` no cursor, e *Diff vs
  default* abre em painel lateral. Cada texto do agente é um *slot* com versões nomeadas e uma
  ativa: prompts do Extractor e do Responder, exemplos de intent, diretivas por campo,
  fallbacks anti-preço, textos da policy, templates do presenter (cotação, planos, recusa,
  handoff por motivo) e textos da orquestração. Salvar aplica na hora, sem reiniciar. No rodapé,
  **Testar prompt** abre o chat do Lab embutido para conversar sem trocar de aba.
- **Tools** — ficha de cada componente, em grade de dois campos por linha: `quote_client`
  (endpoints, timeout, tentativas, orçamento, backoff), ViaCEP (liga/desliga, URL, timeout),
  policy (limites de estagnação, tentativas de CEP, objeções até handoff) e regras
  (pré-validação local liga/desliga). Cada campo mostra a origem do valor efetivo em um selo
  (`default`, `env:VAR`, `override`) e, quando há override, um botão para voltar ao padrão.
- **Config** — mesma ficha para os `settings`: modelo do Gemini, janela de contexto do
  Responder (quantas mensagens do histórico vão em cada chamada; o Extractor é sem histórico
  por desenho), temperaturas, retry do LLM, delay do roteiro e caminho do banco de sessão.

Como isso não altera o comportamento entregue:

- Tudo vive em `config/`. `prompts.json` guarda as versões; a versão `default` de cada slot
  é imutável e igual ao texto em `agent/defaults.py`. `tools.json` e `settings.json` guardam
  **só overrides**: valor efetivo = override > `.env` > default do código, e a UI mostra a
  origem de cada valor. Sem override, o agente é exatamente o da entrega.
- `tests/test_golden_textos.py` compara 26 saídas reais (todas as mensagens do presenter, os
  prompts renderizados) com snapshots gerados do código entregue antes do refactor.
- Hot-reload por `mtime`: nada é lido no import, todo consumidor lê o store na chamada.
- `guard_price` não tem toggle. A formatação de preço e as listas continuam em código; os
  templates só recebem valores já formatados vindos da API.

## Limitações conhecidas
- Estado da conversa em memória (`InMemoryStateStore`); um canal em produção trocaria por
  Redis/DB.
- Um plano por cotação: o agente pergunta o plano antes de cotar. Trocar de plano recota.
- A Evolution API usa protocolo não oficial (Baileys); há risco de bloqueio do número.
