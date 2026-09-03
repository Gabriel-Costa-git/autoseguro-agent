"""Testes da camada LLM: prompts, guardrail anti-preço e resiliência à cota do Gemini.

Sem rede, sem chave e sem SDK: os agentes recebem um dublê de `agno.Agent` e um
`sleep` falso, então o retry é testado em tempo zero.
"""
from __future__ import annotations

from datetime import date

import pytest

from agent import brain
from agent.brain import (
    CAMPOS,
    MAX_TENTATIVAS_LLM,
    ChamadaLLMFalhou,
    Extractor,
    Responder,
    _com_retry,
    _e_transitorio,
    _erro_do_run,
    _retry_delay,
    _status_do_erro,
    build_extraction_instructions,
    build_responder_instructions,
    contem_preco,
    directive_for_field,
    fallback_text,
    guard_price,
    resumo_state,
)
from agent.models import CepInfo, Extraction, Intent, LeadState, Stage, VeiculoColetado
from agent.runtime_config import ConfigStore
from tests.fakes import (
    ERRO_400,
    ERRO_429,
    ERRO_429_SEM_DELAY,
    ClockFake,
    FakeAgnoAgent,
    FakeMessage,
    FakeRun,
    SleepFake,
    quote_indisponivel,
    quote_ok,
    run_erro,
)

HOJE = date(2026, 9, 1)


def _state(**over) -> LeadState:
    base = {"conversation_id": "c1"}
    base.update(over)
    return LeadState(**base)


# --------------------------------------------------------------------------- prompt do Extractor
def test_prompt_extracao_traz_hoje_e_ano_corrente():
    prompt = build_extraction_instructions(_state(), HOJE)
    assert "2026-09-01" in prompt
    assert "2026" in prompt
    assert "ano_parece_modelo" in prompt


def test_prompt_extracao_traz_ultima_pergunta_para_desambiguar():
    prompt = build_extraction_instructions(_state(ultima_pergunta="idade"), HOJE)
    assert "idade" in prompt
    assert "Última pergunta" in prompt


def test_prompt_extracao_lista_intents_com_exemplos():
    prompt = build_extraction_instructions(_state(), HOJE)
    for intent in ("pedir_humano", "objecao_preco", "fora_de_escopo", "aceitar"):
        assert intent in prompt
    assert "tá caro" in prompt
    assert "atendente" in prompt
    assert "bati o carro" in prompt
    assert "pode emitir" in prompt


def test_prompt_extracao_manda_extrair_so_a_mensagem_atual():
    prompt = build_extraction_instructions(_state(idade=35), HOJE)
    assert "ATUAL" in prompt
    assert "data_vaga" in prompt


# --------------------------------------------------------------------------- prompt do Responder
def test_prompt_responder_tem_persona_diretiva_e_regras_duras():
    prompt = build_responder_instructions(_state(idade=35), "peça o CEP de onde o carro dorme")
    assert "AutoSeguro" in prompt and "WhatsApp" in prompt
    assert "peça o CEP de onde o carro dorme" in prompt
    assert "NUNCA cite preço" in prompt
    assert "CPF" in prompt and "placa" in prompt
    assert "desconto" in prompt


def test_prompt_responder_nunca_carrega_valor_da_cotacao():
    """O resumo do estado só diz o status da cotação: o número nunca entra no prompt."""
    state = _state(stage=Stage.APRESENTADO, quote_result=quote_ok())
    prompt = build_responder_instructions(state, "responda à dúvida do lead")
    assert "209" not in prompt
    assert "3000" not in prompt
    assert "ok" in prompt


def test_resumo_state_mostra_dados_coletados_sem_numero_de_preco():
    state = _state(
        idade=35,
        veiculo_texto="Onix",
        veiculo_ano=2019,
        cep="01310100",
        cep_info=CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"),
        plano_id="completo",
    )
    resumo = resumo_state(state)
    assert "35" in resumo and "Onix" in resumo and "01310100" in resumo and "São Paulo/SP" in resumo


def test_resumo_state_marca_cep_ausente():
    assert "não sabe" in resumo_state(_state(cep_ausente=True))


# --------------------------------------------------------------------------- diretivas
def test_directive_for_field_por_campo_e_com_motivo():
    assert "CEP" in directive_for_field("cep")
    com_motivo = directive_for_field("veiculo", motivo="ano 2027 parece ano-modelo")
    assert "ano 2027 parece ano-modelo" in com_motivo
    assert "ano de fabricação" in com_motivo


# --------------------------------------------------------------------------- guardrail
def test_contem_preco_pega_dinheiro_e_ignora_ano_e_idade():
    assert contem_preco("fica R$ 189 por mês")
    assert contem_preco("dá 209,90 na mensalidade")
    assert contem_preco("uns 200 reais")
    assert not contem_preco("seu Onix 2019, 35 anos, cep 01310100")


def test_guard_price_troca_resposta_com_preco_por_fallback_do_campo():
    state = _state(ultima_pergunta="cep")
    saida = guard_price("Fica uns R$ 180,00 por mês. Qual seu CEP?", state)
    assert saida == fallback_text("cep")
    assert not contem_preco(saida)


def test_guard_price_usa_fallback_padrao_sem_pergunta_pendente():
    assert guard_price("uns 250,00 talvez", _state()) == fallback_text(None)


def test_guard_price_dispara_mesmo_com_cotacao_indisponivel():
    state = _state(ultima_pergunta="idade", quote_result=quote_indisponivel())
    assert guard_price("deve dar uns R$ 300", state) == fallback_text("idade")


def test_guard_price_libera_quando_a_cotacao_veio_da_api():
    """Com cotação OK o valor já é público (veio do presenter): o LLM pode conversar sobre ele."""
    state = _state(ultima_pergunta="plano", quote_result=quote_ok())
    texto = "O valor de R$ 209,90 que te passei já inclui assistência."
    assert guard_price(texto, state) == texto


def test_guard_price_nao_mexe_em_texto_limpo():
    texto = "Perfeito! Qual o ano de fabricação do carro?"
    assert guard_price(texto, _state(ultima_pergunta="veiculo")) == texto


def test_fallbacks_nao_contem_preco():
    for campo in [*CAMPOS, None]:
        assert not contem_preco(fallback_text(campo))


# --------------------------------------------------------------------------- resiliência do LLM
class _Erro(RuntimeError):
    """Erro com o corpo do provedor, como o `ModelProviderError` do agno chega em `str(exc)`."""


async def _sempre_falha(mensagem: str, contador: list[int]):
    contador.append(1)
    raise _Erro(mensagem)


def test_status_do_erro_le_atributo_e_corpo():
    assert _status_do_erro(ChamadaLLMFalhou("x", status_code=429)) == 429
    assert _status_do_erro(_Erro(ERRO_429)) == 429
    assert _status_do_erro(_Erro(ERRO_400)) == 400
    assert _status_do_erro(_Erro("erro sem código")) is None


def test_classificacao_transitoria():
    assert _e_transitorio(_Erro(ERRO_429))          # cota estourada: espera e tenta de novo
    assert _e_transitorio(_Erro('{"error": {"code": 503, "status": "UNAVAILABLE"}}'))
    assert _e_transitorio(TimeoutError("deadline"))
    assert _e_transitorio(_Erro("model is overloaded, try again"))
    assert not _e_transitorio(_Erro(ERRO_400))      # payload inválido é bug nosso
    assert not _e_transitorio(_Erro("schema inválido"))


def test_retry_delay_sai_do_retryinfo_do_provedor():
    assert _retry_delay(_Erro(ERRO_429)) == 4.0
    assert _retry_delay(_Erro(ERRO_429_SEM_DELAY)) is None


@pytest.mark.asyncio
async def test_com_retry_respeita_o_retrydelay_e_acerta_na_segunda():
    sleep, chamadas = SleepFake(), []

    async def fn():
        chamadas.append(1)
        if len(chamadas) == 1:
            raise _Erro(ERRO_429)
        return "ok"

    assert await _com_retry(fn, sleep=sleep) == "ok"
    assert sleep.esperas == [4.0]                   # o provedor pediu 4s; obedecemos
    assert len(chamadas) == 2


@pytest.mark.asyncio
async def test_com_retry_sem_retrydelay_usa_backoff_2_4_8():
    sleep, chamadas = SleepFake(), []
    with pytest.raises(_Erro):
        await _com_retry(lambda: _sempre_falha(ERRO_429_SEM_DELAY, chamadas), sleep=sleep)
    assert sleep.esperas == [2.0, 4.0, 8.0]
    assert len(chamadas) == MAX_TENTATIVAS_LLM == 4  # 1 chamada + 3 novas tentativas


@pytest.mark.asyncio
async def test_com_retry_esgota_em_tres_falhas_quando_configurado():
    sleep, chamadas = SleepFake(), []
    with pytest.raises(_Erro):
        await _com_retry(lambda: _sempre_falha(ERRO_429_SEM_DELAY, chamadas), sleep=sleep, max_tentativas=3)
    assert len(chamadas) == 3 and sleep.esperas == [2.0, 4.0]


@pytest.mark.asyncio
async def test_com_retry_nao_re_tenta_erro_nosso():
    sleep, chamadas = SleepFake(), []
    with pytest.raises(_Erro):
        await _com_retry(lambda: _sempre_falha(ERRO_400, chamadas), sleep=sleep)
    assert len(chamadas) == 1 and sleep.esperas == []


@pytest.mark.asyncio
async def test_com_retry_para_quando_a_proxima_espera_estoura_o_orcamento():
    sleep, chamadas = SleepFake(), []
    with pytest.raises(_Erro):
        await _com_retry(
            lambda: _sempre_falha(ERRO_429_SEM_DELAY, chamadas),
            sleep=sleep,
            budget_s=6.0,
            clock=ClockFake(passo=3.0),
        )
    assert sleep.esperas == [2.0]                   # a 2ª espera (4s) passaria dos 6s de orçamento
    assert len(chamadas) == 2


def test_erro_do_run_le_o_status_que_o_agno_marca():
    """O agno não levanta: marca `status=ERROR` e joga `str(exc)` no `content`."""
    assert _erro_do_run(run_erro(ERRO_429)) == ERRO_429
    assert _erro_do_run(FakeRun(content="tudo certo")) is None
    assert _erro_do_run(FakeRun(content=None, status="ERROR")) == "erro sem detalhe do provedor"


# --------------------------------------------------------------------------- degradação honesta
def _extractor(respostas, sleep=None) -> Extractor:
    return Extractor(agent=FakeAgnoAgent(respostas), sleep=sleep or SleepFake())


def _responder(respostas, sleep=None) -> Responder:
    return Responder(agent=FakeAgnoAgent(respostas), sleep=sleep or SleepFake())


@pytest.mark.asyncio
async def test_extractor_re_tenta_a_cota_e_entrega_a_extracao():
    sleep = SleepFake()
    esperada = Extraction(intent=Intent.ESCOLHER_PLANO, plano_id="completo")
    ex = _extractor([run_erro(ERRO_429), FakeRun(content=esperada)], sleep)

    saida = await ex.extract("quero o completo", _state(), HOJE)
    assert saida.plano_id == "completo" and saida.indisponivel is False
    assert sleep.esperas == [4.0]


@pytest.mark.asyncio
async def test_extractor_esgotado_marca_indisponivel():
    """Sem extração, a policy pede para o lead repetir — não re-pergunta o mesmo campo."""
    ex = _extractor([run_erro(ERRO_429) for _ in range(4)])
    saida = await ex.extract("quero o completo", _state(ultima_pergunta="plano"), HOJE)

    assert saida.indisponivel is True
    assert saida.intent is Intent.OUTRO
    assert saida.observacao == "extracao_indisponivel"


@pytest.mark.asyncio
async def test_extractor_com_conteudo_fora_do_schema_nao_re_tenta():
    agent = FakeAgnoAgent([FakeRun(content="desculpe, não entendi")])
    ex = Extractor(agent=agent, sleep=SleepFake())
    saida = await ex.extract("oi", _state(), HOJE)

    assert saida.indisponivel is True
    assert len(agent.chamadas) == 1                 # queimar cota nisso não adianta


@pytest.mark.asyncio
async def test_extractor_tambem_trata_excecao_levantada():
    ex = _extractor([_Erro(ERRO_429), _Erro(ERRO_429), _Erro(ERRO_429), _Erro(ERRO_429)])
    assert (await ex.extract("oi", _state(), HOJE)).indisponivel is True


@pytest.mark.asyncio
async def test_responder_re_tenta_e_devolve_o_texto():
    sleep = SleepFake()
    resp = _responder([run_erro(ERRO_429), FakeRun(content="Qual o ano do carro?")], sleep)
    assert await resp.reply("pergunte o ano", _state(), "é um Onix") == "Qual o ano do carro?"
    assert sleep.esperas == [4.0]


@pytest.mark.asyncio
async def test_responder_esgotado_cai_no_fallback_do_campo():
    resp = _responder([run_erro(ERRO_429) for _ in range(4)])
    saida = await resp.reply("peça o CEP", _state(ultima_pergunta="cep"), "moro em SP")
    assert saida == fallback_text("cep")                # o lead nunca fica sem resposta


@pytest.mark.asyncio
async def test_responder_esgotado_sem_pergunta_pendente_usa_fallback_padrao():
    resp = _responder([_Erro(ERRO_429) for _ in range(4)])
    assert await resp.reply("responda", _state(), "e aí?") == fallback_text(None)


@pytest.mark.asyncio
async def test_responder_com_resposta_vazia_nao_devolve_vazio():
    resp = _responder([FakeRun(content="   ")])
    assert await resp.reply("peça a idade", _state(ultima_pergunta="idade"), "oi") == fallback_text("idade")


@pytest.mark.asyncio
async def test_responder_ainda_passa_pelo_guardrail_de_preco():
    resp = _responder([FakeRun(content="Fica R$ 180,00 por mês!")])
    assert await resp.reply("peça o CEP", _state(ultima_pergunta="cep"), "quanto fica?") == fallback_text("cep")


# --------------------------------------------------------------------------- store (Studio)
def _store_isolado(monkeypatch, tmp_path) -> ConfigStore:
    """Store próprio (defaults do código) no lugar do singleton, para editar sem sujar o repo."""
    novo = ConfigStore(tmp_path)
    monkeypatch.setattr(brain, "store", novo)
    return novo


def test_prompt_do_extractor_vem_do_slot_e_muda_ao_trocar_a_versao(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    assert "Você extrai dados estruturados" in build_extraction_instructions(_state(), HOJE)

    store.add_version("extractor.instructions", "curto", "Hoje é {today}. Estado: {resumo}.")
    saida = build_extraction_instructions(_state(idade=35), HOJE)
    assert saida.startswith("Hoje é 2026-09-01. Estado: idade: 35;")


def test_prompt_do_responder_vem_do_slot_com_resumo_e_diretiva(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    store.add_version("responder.instructions", "t", "TAREFA: {diretiva} | ESTADO: {resumo}")
    # `turnos=2` para não entrar a abertura: a policy incrementa o contador antes do Responder,
    # então o primeiro turno chega aqui com `turnos == 1` (F10)
    estado = _state(idade=35, turnos=2)
    saida = build_responder_instructions(estado, "peça o CEP")
    assert saida == f"TAREFA: peça o CEP | ESTADO: {resumo_state(estado)}"


def test_fallback_e_diretiva_saem_dos_slots(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    store.add_version("fallback.cep", "v2", "Me manda o CEP, por favor.")
    store.add_version("diretiva.cep", "v2", "peça o CEP do lead")

    assert fallback_text("cep") == "Me manda o CEP, por favor."
    assert directive_for_field("cep") == "peça o CEP do lead"
    assert directive_for_field("cep", "motivo x") == "peça o CEP do lead (contexto: motivo x)"


def test_exemplos_de_intent_saem_dos_slots(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    store.add_version("intent.pedir_desconto", "v2", '"faz um precinho?"')
    assert '- pedir_desconto: "faz um precinho?"' in build_extraction_instructions(_state(), HOJE)


# --------------------------------------------------------------------------- recriação do Agent
class ExtractorEspiao(Extractor):
    """Conta quantas vezes o Agent do agno foi construído (sem carregar o SDK)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.construidos = 0

    def _construir(self):
        self.construidos += 1
        return FakeAgnoAgent([])


class ResponderEspiao(Responder):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.construidos = 0

    def _construir(self):
        self.construidos += 1
        return FakeAgnoAgent([])


def test_agent_e_reconstruido_quando_o_modelo_muda(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    ex = ExtractorEspiao()

    ex.agente(), ex.agente()
    assert ex.construidos == 1                      # sem mudança, reaproveita

    store.set_overrides("settings", {"gemini_model": "gemini-outro"})
    ex.agente()
    assert ex.construidos == 2

    store.set_overrides("settings", {"extractor_temperature": 0.7})
    ex.agente()
    assert ex.construidos == 3


def test_responder_reconstroi_ao_mudar_o_tamanho_do_historico(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    resp = ResponderEspiao()
    resp.agente()
    store.set_overrides("settings", {"responder_history_runs": 2})
    resp.agente()
    assert resp.construidos == 2


def test_limites_do_retry_saem_do_store(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    store.set_overrides("settings", {"llm_max_tentativas": 2, "llm_budget_s": 9.0})
    kwargs = Extractor(agent=FakeAgnoAgent([]))._retry_kwargs()
    assert kwargs["max_tentativas"] == 2
    assert kwargs["budget_s"] == 9.0
    assert kwargs["papel"] == "extractor"


# --------------------------------------------------------------------------- hook de trace
def test_trace_do_extractor_traz_prompt_entrada_e_saida():
    eventos: list[dict] = []
    esperada = Extraction(intent=Intent.FORNECER_DADOS, idade=35)
    run = FakeRun(content=esperada)
    run.messages = [FakeMessage("system", "instruções..."), FakeMessage("user", "tenho 35")]
    ex = Extractor(agent=FakeAgnoAgent([run]), sleep=SleepFake(), trace=eventos.append)

    import asyncio

    saida = asyncio.run(ex.extract("tenho 35", _state(conversation_id="c9"), HOJE))
    assert saida.idade == 35
    assert len(eventos) == 1
    ev = eventos[0]
    assert set(ev) == {
        "papel", "modelo", "session_id", "tentativa", "instructions",
        "historico", "entrada", "saida", "status", "latency_ms", "erro",
    }
    assert ev["papel"] == "extractor" and ev["status"] == "ok" and ev["tentativa"] == 1
    assert ev["session_id"] == "extract-c9"
    assert ev["entrada"] == "tenho 35"
    assert ev["saida"]["idade"] == 35
    assert ev["instructions"] == build_extraction_instructions(_state(conversation_id="c9"), HOJE)
    assert ev["historico"] == [
        {"role": "system", "content": "instruções...", "from_history": False},
        {"role": "user", "content": "tenho 35", "from_history": False},
    ]


def test_trace_registra_cada_tentativa_e_o_fallback():
    eventos: list[dict] = []
    ex = Extractor(agent=FakeAgnoAgent([run_erro(ERRO_429)] * 4), sleep=SleepFake(), trace=eventos.append)

    import asyncio

    saida = asyncio.run(ex.extract("oi", _state(), HOJE))
    assert saida.indisponivel is True
    assert [e["status"] for e in eventos] == ["erro", "erro", "erro", "erro", "fallback"]
    assert [e["tentativa"] for e in eventos] == [1, 2, 3, 4, 4]
    assert "RESOURCE_EXHAUSTED" in eventos[0]["erro"]
    assert eventos[-1]["saida"]["indisponivel"] is True


def test_trace_do_responder_marca_fallback_com_o_texto_devolvido():
    eventos: list[dict] = []
    resp = Responder(agent=FakeAgnoAgent([run_erro(ERRO_429)] * 4), sleep=SleepFake(), trace=eventos.append)

    import asyncio

    texto = asyncio.run(resp.reply("peça o CEP", _state(ultima_pergunta="cep"), "moro em SP"))
    assert texto == fallback_text("cep")
    assert eventos[-1]["status"] == "fallback" and eventos[-1]["saida"] == texto
    assert eventos[-1]["papel"] == "responder"


def test_trace_que_explode_nao_derruba_o_turno():
    def trace_ruim(_evento: dict) -> None:
        raise RuntimeError("bug no observador")

    ex = Extractor(
        agent=FakeAgnoAgent([FakeRun(content=Extraction(idade=40))]), sleep=SleepFake(), trace=trace_ruim
    )

    import asyncio

    assert asyncio.run(ex.extract("40", _state(), HOJE)).idade == 40


# --------------------------------------------------------------------------- vários carros (F8)
def _carros(*pares) -> list[VeiculoColetado]:
    return [VeiculoColetado(texto=t, ano=a) for t, a in pares]


def test_resumo_state_com_um_carro_e_o_texto_entregue():
    """1 carro = exatamente o resumo de sempre (o golden `brain_resumo_state` prova o byte)."""
    state = _state(idade=35, veiculo_texto="Onix", veiculo_ano=2019, veiculos=_carros(("Onix", 2019)))
    assert "carro: Onix (ano 2019)" in resumo_state(state)
    assert "carros:" not in resumo_state(state)


def test_resumo_state_com_dois_carros_lista_os_dois():
    state = _state(
        idade=35, veiculo_texto="Onix 2022", veiculo_ano=2022,
        veiculos=_carros(("Onix 2022", 2022), ("HB20", 2020)),
    )
    resumo = resumo_state(state)
    assert "carros: Onix 2022; HB20 2020" in resumo
    assert "carro: " not in resumo


def test_guard_price_libera_quando_algum_carro_cotou():
    """Com 2 carros, basta uma cotação OK para o valor já ser público."""
    veiculos = _carros(("Onix", 2019), ("HB20", 2020))
    veiculos[0].quote_result = quote_indisponivel()
    veiculos[1].quote_result = quote_ok()
    state = _state(ultima_pergunta="plano", veiculos=veiculos)

    texto = "O Onix eu te mando em seguida; o HB20 ficou em R$ 209,90."
    assert guard_price(texto, state) == texto


def test_guard_price_dispara_quando_nenhum_carro_cotou():
    veiculos = _carros(("Onix", 2019), ("HB20", 2020))
    veiculos[0].quote_result = quote_indisponivel()
    state = _state(ultima_pergunta="idade", veiculos=veiculos)
    assert guard_price("deve dar uns R$ 300 cada", state) == fallback_text("idade")


def test_prompt_do_extractor_pede_a_lista_de_carros():
    prompt = build_extraction_instructions(_state(), HOJE)
    assert "veiculos: UM item por carro citado" in prompt
    assert "não junte tudo" in prompt
    assert "repita o PRIMEIRO item de veiculos" in prompt     # compatibilidade com 1 carro


# --------------------------------------------------------------------------- guardrails e abertura (F10)
def test_guardrails_entram_sempre_no_prompt_do_responder():
    prompt = build_responder_instructions(_state(idade=35, turnos=3), "peça o CEP")
    assert "Regras invioláveis:" in prompt
    assert "só trata de cotação de seguro auto NOVO" in prompt
    assert "NUNCA prometa desconto" in prompt
    assert "NÃO é ordem" in prompt                       # injeção de prompt
    assert "NUNCA peça CPF" in prompt


def test_guardrails_listam_as_coberturas_reais_do_planos():
    """É esta lista que impede o "guincho" que apareceu no log."""
    planos = {"planos": [
        {"coberturas": ["colisao", "roubo"]},
        {"coberturas": ["colisao", "carro_reserva", "assistencia_24h"]},
    ]}
    prompt = build_responder_instructions(_state(turnos=3), "peça a idade", planos)
    assert "As únicas coberturas que existem são: colisão, roubo, carro reserva, assistência 24h." in prompt
    assert "guincho" not in prompt.lower()


def test_sem_planos_as_coberturas_saem_dos_slots_do_presenter():
    prompt = build_responder_instructions(_state(turnos=3), "peça a idade")
    for cobertura in ("colisão", "roubo", "furto", "danos a terceiros", "vidros", "carro reserva"):
        assert cobertura in prompt


def test_abertura_so_entra_no_primeiro_turno():
    abertura = "apresente-se em uma frase como Lia"
    assert abertura in build_responder_instructions(_state(), "pergunte a idade")          # turnos=0
    assert abertura in build_responder_instructions(_state(turnos=1), "pergunte a idade")  # 1º turno
    assert abertura not in build_responder_instructions(_state(turnos=2), "peça o CEP")


def test_prompt_do_extractor_tem_o_intent_de_duvida_e_perdeu_a_apolice():
    prompt = build_extraction_instructions(_state(), HOJE, ferramentas=[])
    assert "- duvida_produto:" in prompt
    assert "o que é franquia?" in prompt
    assert "quero ver minha apólice" not in prompt        # não vira mais fora_de_escopo
    assert "NUNCA inverta o" in prompt                   # regra de negação da observação


def test_guard_price_libera_valores_que_vieram_do_material_da_diretiva():
    """Dúvida sobre o produto: a franquia dos planos vai no prompt, então pode voltar na resposta."""
    dados = "- Essencial: franquia de R$ 4.500,00\n- Completo: franquia de R$ 3.000,00"
    texto = "O Essencial tem franquia de R$ 4.500,00 e o Completo, R$ 3.000,00. Qual sua idade?"
    assert guard_price(texto, _state(), dados) == texto


def test_guard_price_bloqueia_valor_que_nao_estava_no_material():
    dados = "- Essencial: franquia de R$ 4.500,00"
    inventado = "Fica uns R$ 150,00 por mês. Qual seu CEP?"
    assert guard_price(inventado, _state(ultima_pergunta="cep"), dados) == fallback_text("cep")


def test_guard_price_sem_material_continua_estrito():
    texto = "O Essencial tem franquia de R$ 4.500,00."
    assert guard_price(texto, _state()) == fallback_text(None)


def test_valores_citados_le_as_formas_de_dinheiro():
    from agent.brain import valores_citados

    assert valores_citados("R$ 4.500,00 e 209,90 e 200 reais") == {4500.0, 209.9, 200.0}
    assert valores_citados("tenho 35 anos e um Onix 2019") == set()

