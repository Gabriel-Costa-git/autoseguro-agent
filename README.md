# AutoSeguro Agent — Desafio FDE Namastex

Agente de vendas de seguro auto por chat (WhatsApp). Conversa com o lead, qualifica
(idade, carro, CEP, plano), **cota na API de cotação instável sem travar nem inventar
preço** e **escala para um humano com critério explícito e auditável**.

O agente não fecha a venda: não existe API de contratação, então quando o lead aceita a
cotação um consultor humano recebe os dados e a cotação prontos para emitir.

## Como rodar

Pré-requisitos: **[uv](https://docs.astral.sh/uv/)**, **Python ≥ 3.12** (o `uv sync` instala o
interpretador se faltar) e, opcionalmente, **Docker** — só para subir a API de cotação do
desafio do jeito mais curto. Sem Docker, ela roda direto com `uv`.

```bash
# 1. API de cotação do desafio (repo do desafio clonado ao lado)
cd ../namastex-fde-challenge && docker compose up --build -d          # http://localhost:8000
# ...ou, sem Docker:
cd ../namastex-fde-challenge/quote-service && uv run uvicorn app.main:app --port 8000

# 2. Dependências e configuração
cd ../autoseguro-agent
uv sync
cp .env.example .env            # preencha GOOGLE_API_KEY (Gemini)

# 3. Conversa no terminal (gera logs/<conversation_id>.jsonl)
uv run python -m agent.chat
uv run python -m agent.chat --script scripts/roteiro-feliz.txt --delay 15   # roteiro sem interação

# 4. Testes (sem rede, sem LLM, sem docker) e lint
uv run pytest -q
uv run ruff check agent tests scripts

# 5. Canal WhatsApp (Evolution API v2) — opcional, exige instância pareada
uv run python -m agent.serve    # webhook em POST /webhook, porta $PORT (3000)

# 6. Studio, o painel local de edição e teste (opcional)
uv run python -m agent.studio   # http://127.0.0.1:$STUDIO_PORT (8765)
```

Comandos do chat: `/estado` (estado mascarado), `/audio` (simula mensagem de mídia), `/sair`.

Variáveis que valem a pena conhecer (todas com padrão; o `.env.example` traz o resto):

| Variável | Padrão | Para quê |
|---|---|---|
| `GOOGLE_API_KEY` | — | chave do Gemini; sem ela o CLI sai com mensagem clara |
| `QUOTE_API_URL` | `http://localhost:8000` | onde está a API de cotação do desafio |
| `LOG_DIR` | `logs` | onde o JSONL de cada conversa é gravado |
| `PORT` | `3000` | porta do webhook da Evolution (`agent.serve`) |
| `STUDIO_PORT` | `8765` | porta do Studio; troque se a 8765 estiver ocupada |

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
| `agent/takeover.py`, `agent/atendimentos.py` | Quem responde cada conversa (`config/atendimentos.json`, lido também pelo `serve.py`) e o catálogo de atendimentos lido dos logs. |
| `agent/handoff.py` | Avisa o consultor no handoff: assume a conversa, manda o resumo no WhatsApp e chama o webhook do CRM. |
| `agent/tools_runtime.py` | Executa as tools criadas no painel (`http`/`sql`) e as entrega ao Responder como function calling; segredo só por `${env:X}`, SQL somente leitura. |
| `agent/studio/` | Studio local: Atendimentos, Lab (Conversa · Prompts · Tools) e Config (FastAPI + estático, só 127.0.0.1). |

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

Política: **timeout 3,5 s por tentativa, até 4 tentativas em 5xx, backoff exponencial com
jitter ±50 %, orçamento total de 15 s.** As duas travas competem: um 5xx volta na hora, então
cabem as 4 tentativas; um timeout gasta 3,5 s cada, e aí o orçamento de 15 s corta na 3ª — é
o que se vê quando a API está 100 % lenta. Esperar os 8 s garantiria a resposta da chamada
lenta, mas cortar em 3,5 s e tentar de novo custa menos em média (70 % de chance de resposta
em 50 ms). O preço disso é contar o timeout como falha: com 3 tentativas, 0,3³ = 2,7 % das
cotações escalariam por infra; com 4, 0,3⁴ ≈ 0,8 %. No primeiro timeout o lead recebe "só um
instante".
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
mensagem tem `message_id`, e o `Outbound` carrega `in_reply_to` para ligar cada resposta à
mensagem que a provocou.

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
agno 3.x com Gemini (`gemini-3.5-flash-lite`, escolhido no seletor da aba Config do Studio e gravado
em `config/settings.json`; `GEMINI_MODEL` no `.env` continua valendo como fallback) faz duas coisas: extração com saída estruturada
(sem histórico, temperatura 0, contexto e última pergunta no prompt para desambiguar
"sim"/"35"/"2019") e resposta conversacional com histórico por sessão em SQLite. Tudo que
decide está fora do framework, em Python puro, para ser testável e trocável.

O provedor de LLM é **outra dependência instável**, e foi tratado como a `/quote`: a primeira
rodada real estourou a cota gratuita do Gemini (429 `RESOURCE_EXHAUSTED`; os limites do plano
gratuito são por projeto e visíveis no AI Studio, não vale fixar número aqui), o agno não
re-tenta por conta própria e devolve o erro dentro do `RunOutput` em vez de levantar.
Por isso `brain.py` tem retry que honra o `retryDelay` do provedor (backoff 2/4/8 s quando ele
não diz, até 3 novas tentativas, 30 s de orçamento) e degradação honesta: extração indisponível
vira "não consegui ler sua mensagem, pode repetir?" em vez de repetir a última pergunta, e três
seguidas escalam para humano. O CLI tem `--delay` para roteiros dentro da cota gratuita.

### 9. Canal é adaptador
O núcleo expõe `Conversation.handle(inbound, emit)`. O CLI é o canal de desenvolvimento e
gera o log da entrega. O adaptador Evolution (`channels/evolution.py`) traduz o webhook
`messages.upsert`, ignora grupos, mensagens próprias e eventos sem texto (recibos de
protocolo — ver "Falhas tratadas"), responde 200 imediatamente, processa
em background com um lock por conversa e envia "digitando" antes de cada resposta. Mídia sem
texto (áudio, imagem, documento) recebe pedido para escrever.

## Testes
`uv run pytest -q` roda <!-- n_testes -->1019 testes sem rede, sem LLM e sem docker: transporte HTTP mockado
para `/quote` e ViaCEP, relógio e sleep injetáveis para o retry, `FakeLLM` para o turno,
policy e presenter puros. Há um teste que faz grep no `presenter.py` para garantir que nenhum
valor de preço está fixo em template.

## Log de uma execução completa
Três execuções reais (Gemini `gemini-3.5-flash-lite` + API de cotação em docker), geradas pelo
CLI em modo roteiro e commitadas em `logs/entrega/`.

> Regravados em 03/09 com o agente desta versão: a data de início é perguntada antes de cotar
> (a pro-rata só aparece explicada), o `outbound` carrega `in_reply_to`, e a resposta do LLM
> passa pelo guard pós-modelo. Para regravar: `uv run python -m agent.chat --script scripts/roteiro-feliz.txt --delay 3`.

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

## Falhas tratadas

O que quebrou em uso real virou regra no código.

### Falhas tratadas no canal
A Evolution manda `messages.upsert` para coisas que não são mensagem (recibos, chaves de
sessão, reações, edições): o webhook descarta todo upsert sem conteúdo, e só áudio, imagem,
documento, sticker e texto viram turno. Cada conversa tem teto de respostas por minuto
(`tools.canal.max_respostas_por_minuto`, 6) e nunca recebe o mesmo texto duas vezes seguidas
sem ter escrito algo no meio; o que é barrado vira um evento `outbound_suprimido` com o
motivo, porque silêncio sem rastro é pior que ruído. Mensagens picadas do mesmo lead dentro
de `tools.canal.debounce_s` (2 s) viram um turno só, em vez de uma resposta para cada linha.
O aviso ao consultor agora sabe se chegou — a Evolution devolve sucesso/falha e o webhook do
CRM tem três tentativas — e a conversa **só** é passada para o humano depois de pelo menos um
aviso entregue: se todos falharem, o agente continua respondendo e o log diz por quê.
Takeover automático que ninguém foi atender volta ao agente depois de
`tools.handoff.auto_devolver_apos_min` (4 h), com um evento `takeover_expirado`.

Os três parâmetros são editáveis na aba Config do Studio (origem do valor à vista):

- `tools.canal.max_respostas_por_minuto` (padrão 6) — teto de respostas por conversa por minuto; o que passa disso não é enviado e vira `outbound_suprimido`.
- `tools.canal.debounce_s` (padrão 2) — segundos em que mensagens picadas do mesmo lead se juntam num turno só; `0` desliga.
- `tools.handoff.auto_devolver_apos_min` (padrão 240) — minutos sem mensagem humana até um takeover automático voltar ao agente, com evento `takeover_expirado`.

**Loop de respostas no smoke do WhatsApp.** No primeiro teste com um número real o agente
respondeu à saudação e, em seguida, recebeu 23 `messages.upsert` sem texto e sem remetente —
recibos de protocolo do WhatsApp, um a cada ~3 s, disparados pelos próprios envios do agente.
O parser tratou cada um como turno do lead: o primeiro virou "manda por escrito", o segundo
virou handoff por falta de progresso e, com a conversa já em estado terminal, todos os
seguintes receberam o mesmo texto de encerramento — 23 mensagens idênticas em 80 s para uma
pessoa real. Três mudanças: (1) o adaptador **ignora upsert sem conteúdo de texto** — recibo,
reação e `protocolMessage` não são turno; (2) há um **limite de respostas por conversa por
minuto**, para que uma cascata de eventos não vire uma cascata de mensagens; (3) **estado
terminal responde uma vez e depois silencia**, porque depois do handoff quem fala é o humano.
O payload real do incidente virou teste de regressão.

## Handoff (quando entra um humano)

Quando a policy decide escalar (lead aceitou, pediu atendente, negociação, cotação indisponível,
erro interno, fora de escopo, sem progresso), o lead recebe o texto de sempre — e
**o outro lado também fica sabendo**. `agent/handoff.py` dispara três canais independentes, cada um
desligável e cada um virando um evento `handoff_notice` (`{canal, status, destino}`) no JSONL da
conversa:

| Canal | O que faz | Configuração |
|---|---|---|
| `takeover` | marca a conversa como **humana** (`config/atendimentos.json`): o webhook passa a registrar o `inbound` com `modo="humano"` e o agente para de responder aquele lead. Só acontece depois de pelo menos um aviso entregue; sem nenhum, sai `status: "nao_assumido"` e o agente continua respondendo | `tools.handoff.auto_assumir` (padrão ligado), `tools.handoff.auto_devolver_apos_min` (padrão 240) |
| `whatsapp` | manda ao consultor um resumo com motivo, nome, telefone, origem, dados coletados, **o preço de cada carro** e o link direto de Atendimentos | `tools.handoff.consultor_number` (vem de `CONSULTOR_NUMBER`) e `tools.handoff.studio_url` |
| `webhook` | `POST` JSON para um CRM (`{conversation_id, origem, motivo, dados, cotacoes, link, ts}`), 5 s de timeout | `tools.handoff.webhook_url` e `tools.handoff.webhook_headers` (valores aceitam `${env:X}`) |

Detalhes que importam:

- **O aviso é o único texto com preço que não vai para o lead.** Ele é um slot
  (`presenter.handoff.aviso_consultor`), editável no Studio, e os valores continuam vindo do
  `Quote` da API pela mesma formatação da mensagem do lead — preço nasce num lugar só.
- **Nada disso derruba o turno**: cada canal tem o seu try/except e o pior caso é um
  `handoff_notice` com `status: "erro"`. Sem `CONSULTOR_NUMBER`/webhook, o status é `"desligado"`.
- **Turno que quebrou também avisa**: o erro interno já era handoff, agora com aviso.
- **No Lab nada sai**: a conversa de teste roda o notificador em modo `simulado` — o painel Eventos
  mostra o texto que teria ido ao consultor, sem WhatsApp e sem marcar `lab-*` como assumida.
- O telefone do consultor vai **mascarado** para o log; a URL do webhook aparece só como host.

## Studio (edição e teste local do agente)

`uv run python -m agent.studio` sobe um painel de operador em `127.0.0.1`, fora do caminho da
entrega: `serve.py` nunca importa `agent.studio` e, com `config/` vazia, o agente é o do commit.

- **Atendimentos** — todas as conversas reais numa lista só, para ver o que o agente fez e
  **assumir** a conversa quando o handoff pede um humano.
- **Lab · Conversa** — conversar como lead pelo mesmo `Conversation.handle` da entrega (nunca
  uma cópia), com eventos, payload de cada chamada ao modelo e estado ao lado.
- **Lab · Prompts** — cada texto do agente é um slot versionado: editar e ativar sem
  reiniciar, comparando com o default imutável.
- **Lab · Tools** e **Config** — integrações e parâmetros com a origem de cada valor à vista
  (`default`, `env:VAR`, `override`) e botão de voltar ao padrão.

Detalhe de cada tela e o contrato de `config/`: **[`docs/STUDIO.md`](docs/STUDIO.md)**.

## Limites de escopo

- **WhatsApp real é opcional.** A entrega roda inteira pelo CLI; o adaptador Evolution existe
  e foi testado num número real, mas depende de uma instância pareada que o avaliador não
  precisa ter.
- **Vigência assumida.** <!-- data_inicio --> Se o lead não informa a data de início, a
  cotação usa a data de hoje.
- **Cota do Gemini.** O plano gratuito tem limite por projeto; ele foi atingido em
  desenvolvimento e o `--delay` do CLI existe por isso. Os números do limite mudam e não estão
  fixados aqui de propósito.
- **Sem API de contratação**, então o agente não fecha venda: o handoff entrega dados e
  cotação prontos para um humano emitir.

## Limitações conhecidas
- Estado da conversa em memória (`InMemoryStateStore`); um canal em produção trocaria por
  Redis/DB.
- Um plano por cotação: o agente pergunta o plano antes de cotar. Trocar de plano recota.
- A Evolution API usa protocolo não oficial (Baileys); há risco de bloqueio do número.
- Tool criada no painel vale só para o **Responder** (a conversa). O Extractor, a policy e a
  cotação não a enxergam — de propósito: quem decide é código, não o modelo.
- Ao **devolver ao agente** uma conversa assumida, o Responder não viu a troca humana: o
  histórico do agno só tem o que o próprio agente falou. Ele retoma do `LeadState`, então pode
  repetir algo que o operador já resolveu no meio.
- O Studio **envia** pela Evolution quando o operador assume, mas não recebe webhook: as
  respostas do lead continuam chegando pelo `serve.py`, que as registra sem chamar o agente.
- Depois de um handoff, **"Devolver ao agente" faz o agente voltar a responder** — e, como a etapa
  está terminal, ele manda uma vez o texto de encerramento ("um consultor já está com o seu
  caso") e depois silencia. Para retomar a coleta é preciso uma conversa nova.

## Transparência de uso de IA

Construído com **orquestração multi-agente com Claude Code**: um terminal orquestrador e
executores paralelos, cada um com escopo de arquivos fechado. Em `ai-logs/`: `briefs/` (as
instruções dadas a cada executor), `reports/` (o que cada um entregou e decidiu) e
`sessions/` (os transcripts `.jsonl`, exportados e higienizados por
`scripts/export_ai_logs.py`). `ai-logs/README.md` explica o que foi redigido, por quê, e como
validar (`--check`).
