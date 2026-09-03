# Studio — edição e teste local do agente

Detalhe do painel de operador. O README traz o resumo e o porquê de cada peça; aqui está
como cada tela funciona. O Studio é ferramenta de desenvolvimento: **nada aqui muda o
comportamento entregue enquanto `config/` estiver vazia.**

```bash
uv run python -m agent.studio          # http://127.0.0.1:8765
scripts/quote_api_falha.sh             # opcional: API de cotação com falha forçada na 8001
```

Painel de operador, só em `127.0.0.1`, fora do canal Evolution (`agent/serve.py` não sabe que
ele existe). Tema dark, sem framework, sem bundler e sem nada vindo de CDN: HTML, CSS e dois
módulos ES servidos do disco. Uma barra superior de 56 px carrega a marca, o breadcrumb
(`Atendimentos / <conversa>`, `Lab / Prompts / <slot>`) e as três abas de topo como links
segmentados, com o indicador de saúde à direita; o hash da URL (`#atendimentos`,
`#lab/conversa`, `#lab/prompts`, `#lab/tools`, `#config`) é quem manda, e a tela inicial é
`#atendimentos`. Prompts e Tools são ferramentas do Lab — o workbench do agente —, então são
sub-abas dele, não abas de topo. Lab e "Testar prompt" dividem o mesmo componente de chat e a
MESMA sessão do Lab — uma por aba do navegador, que sobrevive ao reload em vez de abrir outra.

- **Atendimentos** — a visão de operação: todas as conversas reais do agente numa lista só,
  ordenadas pela última mensagem. Cada linha traz a **origem** do lead (`whatsapp:<instância>`,
  `cli` ou `lab`), o status (`agente`, `humano`, `encerrado`), a etapa, a última mensagem e o
  tempo relativo; filtros por origem, status e busca. Abrir uma conversa mostra a transcrição,
  os eventos e o estado reconstruído do log. **Assumir** passa a conversa para o operador: o
  agente para de responder aquele lead na hora (o webhook só registra o `inbound` com
  `modo="humano"`) e o composer libera o envio pela Evolution, que entra no log como `outbound`
  com `source="humano"`. **Devolver ao agente** desfaz. Só conversas de WhatsApp (`wa-*`) podem
  receber mensagem do operador — Lab e CLI não têm para onde enviar.
- **Lab · Conversa** — conversa como lead usando o mesmo `Conversation.handle` da entrega (nunca uma
  cópia). O chat ocupa o centro (bolhas do lead à direita, do agente à esquerda com a etiqueta
  `template`/`llm`) e o painel de 380 px à direita tem três painéis: **Eventos** ao vivo do
  turno (extração, decisão da policy, cada tentativa da `/quote` com status e latência, ViaCEP,
  handoff), **Contexto** — o payload exato de cada chamada ao modelo, com as instruções
  renderizadas, o histórico enviado, a entrada e a saída — e **Estado**, o `LeadState` da
  sessão. Clicar numa bolha seleciona o turno. Na barra do chat ficam o seletor de API de
  cotação (docker na 8000, falha forçada na 8001), que mostra a URL em uso pela sessão, e o
  seletor de modelo, com a mesma lista da aba Config.
- **Lab · Prompts** — uma página de editor: dropdown de slot com busca (`/` foca) e itens agrupados,
  dropdown de versão, selo `Ativa`/`Rascunho`/`Default · imutável`, e os botões **Ativar** e
  **Salvar** (`Cmd/Ctrl+S`). O corpo alterna entre editor mono, preview de markdown e os dois
  lado a lado; os chips dos placeholders do slot inserem `{campo}` no cursor, e *Diff vs
  default* abre em painel lateral. Cada texto do agente é um *slot* com versões nomeadas e uma
  ativa: prompts do Extractor e do Responder, exemplos de intent, diretivas por campo,
  fallbacks anti-preço, textos da policy, templates do presenter (cotação, planos, recusa,
  handoff por motivo) e textos da orquestração. Salvar aplica na hora, sem reiniciar. No rodapé,
  **Testar prompt** abre o chat do Lab embutido para conversar sem trocar de aba.
- **Lab · Tools** — as **integrações** do agente, em lista + detalhe. As embutidas (selo
  `builtin`): `quote_client` (endpoints, timeout, tentativas, orçamento, backoff) e ViaCEP
  (liga/desliga, URL, timeout), cada campo com a origem do valor efetivo num selo (`default`,
  `env:VAR`, `override`) e o botão de voltar ao padrão. Cada uma traz também **Instruções e
  textos**: os slots de Prompts que falam por ela (o texto de instabilidade da cotação, a
  confirmação de CEP, a diretiva de pedir o CEP…), com a versão ativa, prévia e link para editar.
  Abaixo delas ficam as **tools criadas no painel** — ver a seção logo adiante.
- **Config** — mesma ficha para os `settings`: modelo do Gemini, janela de contexto do
  Responder (quantas mensagens do histórico vão em cada chamada; o Extractor é sem histórico
  por desenho), temperaturas, retry do LLM, delay do roteiro e caminho do banco de sessão. O
  modelo é um **seletor** com botão **Atualizar modelos**, que consulta a API do Google
  (`models.list`, só os que fazem `generateContent`) e guarda a lista em `config/models.json`;
  a mesma lista aparece na barra do chat do Lab. Escolher grava override em
  `config/settings.json` e vale no próximo turno, sem reiniciar. Aqui embaixo ficam também as
  fichas de **policy** (limites de estagnação, tentativas de CEP, objeções até handoff) e de
  **regras** (pré-validação local liga/desliga): elas ajustam a decisão do agente, não uma
  integração, então saíram de Tools — os caminhos da API (`tools.policy.*`, `tools.rules.*`)
  continuam os mesmos.

## Tools criadas no painel

Em **Lab · Tools**, *Nova tool* cria uma integração que o agente passa a ter à mão: ela vira uma
função que o **Responder** pode chamar no meio da conversa (function calling do Gemini), com o
nome, a descrição e os parâmetros que você escreveu. A descrição é o que o modelo lê para decidir
*quando* chamar; o campo **Instruções** vai para o system prompt (ex.: "nunca leia o número da
apólice inteiro; confirme só os 4 últimos dígitos").

Dois tipos:

- **`http`** — um request: método, URL, headers, query e body. `{parametro}` é substituído pelo
  argumento que o modelo mandou (escapado na URL), e a resposta volta como JSON compacto ou texto.
- **`sql`** — uma query **somente leitura** num sqlite (caminho do arquivo) ou num Postgres
  (`postgresql://…`, só se `psycopg` estiver instalado — não é dependência do projeto). O
  parâmetro é nomeado (`:cpf`), nunca interpolado na string.

O que o painel garante, e por quê:

- **Segredo nunca entra em `config/`.** Onde vai a chave, escreve-se `${env:APOLICE_KEY}`; o valor
  fica no `.env` e é resolvido só na hora da chamada. A API do Studio devolve a referência literal,
  e qualquer valor resolvido é apagado (`***`) do resultado e do log. O seletor `${env:X}` do
  formulário lista só os **nomes** das variáveis do ambiente.
- **SQL é só leitura, duas vezes.** O registro recusa query que não comece com `SELECT`/`WITH`, que
  tenha `;` ou palavra de escrita (`update`, `delete`, `drop`, `pragma`, `into`…); e o sqlite ainda
  é aberto em `mode=ro`, para o caso de alguém editar o JSON à mão.
- **A tool nunca derruba o turno.** Timeout (`asyncio.wait_for`), rede fora, variável de ambiente
  ausente ou SQL inválido viram a string `erro: …` devolvida ao modelo, que segue a conversa. O
  resultado é truncado em *máx. caracteres* antes de voltar para o prompt.
- **Cada execução vira um evento `tool_call`** no JSONL da conversa — `{tool, args, status:
  ok|erro|timeout, latency_ms, resultado}`, com a PII mascarada como no resto do log. Ele aparece
  no painel Eventos do Lab e na transcrição de Atendimentos (`tool_call consulta_apolice: ok ·
  210 ms`, clicável para abrir args e resultado).
- **Botão Testar**: roda a tool salva com os argumentos que você digitar, sem LLM e sem gravar
  conversa nenhuma — devolve resultado e latência.

Exemplo do que fica em `config/custom_tools.json` (o `${env:…}` é literal no arquivo):

```json
{"tools": {"consulta_apolice": {
  "nome": "consulta_apolice", "tipo": "http", "enabled": true,
  "descricao": "Consulta a apólice do cliente pelo CPF. Use quando o lead perguntar sobre uma apólice existente.",
  "instrucoes": "Nunca leia o número da apólice em voz alta; confirme só os 4 últimos dígitos.",
  "parametros": {"cpf": {"tipo": "string", "descricao": "CPF só dígitos", "obrigatorio": true}},
  "timeout_s": 5, "max_chars": 2000,
  "http": {"metodo": "GET", "url": "https://api.exemplo.com/apolices/{cpf}",
           "headers": {"Authorization": "Bearer ${env:APOLICE_KEY}"}, "resposta": "json"}
}}}
```

Como isso não altera o comportamento entregue:

- Tudo vive em `config/`. `prompts.json` guarda as versões; a versão `default` de cada slot
  é imutável e igual ao texto em `agent/defaults.py`. `tools.json` e `settings.json` guardam
  **só overrides**: valor efetivo = override > `.env` > default do código, e a UI mostra a
  origem de cada valor. Sem override, o agente é exatamente o da entrega.
- `tests/test_golden_textos.py` compara 26 saídas reais (todas as mensagens do presenter, os
  prompts renderizados) com snapshots gerados do código entregue antes do refactor.
- **Sem tool cadastrada, o Responder é o da entrega**: o `Agent` do agno é construído sem o
  argumento `tools` (há teste que compara os kwargs da construção), e nenhum evento `tool_call`
  existe. O Extractor e a policy nunca recebem tools — quem decide continua sendo código.
- Atendimentos é **só leitura de log** — a fonte é `logs/*.jsonl` e `logs/studio/*.jsonl`, e
  `logs/entrega/` fica de fora. Quem responde cada conversa está em `config/atendimentos.json`:
  arquivo ausente ou vazio = o agente responde tudo, exatamente como na entrega.
- Hot-reload por `mtime`: nada é lido no import, todo consumidor lê o store na chamada. Vale
  também para o takeover, que é escrito pelo Studio e lido pelo `serve.py` — outro processo.
- `guard_price` não tem toggle. A formatação de preço e as listas continuam em código; os
  templates só recebem valores já formatados vindos da API.
