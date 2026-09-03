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
    assert "Você extrai dados de UMA mensagem" in build_extraction_instructions(_state(), HOJE)

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
        "evento", "papel", "modelo", "session_id", "tentativa", "instructions",
        "historico", "entrada", "saida", "status", "latency_ms", "erro", "usage",
    }
    assert ev["evento"] == "llm_call"
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
    chamadas = [e for e in eventos if e["evento"] == "llm_call"]
    assert [e["status"] for e in chamadas] == ["erro", "erro", "erro", "erro", "fallback"]
    assert [e["tentativa"] for e in chamadas] == [1, 2, 3, 4, 4]
    assert "RESOURCE_EXHAUSTED" in chamadas[0]["erro"]
    assert chamadas[-1]["saida"]["indisponivel"] is True
    # e, ao lado deles, o rastro que faltava no log da Fase 3: cada espera e a desistência
    assert [e["evento"] for e in eventos if e["evento"] != "llm_call"] == [
        "llm_retry", "llm_retry", "llm_retry", "llm_error",
    ]
    assert [e["espera_s"] for e in eventos if e["evento"] == "llm_retry"] == [4.0, 4.0, 4.0]
    assert all(e["status"] == 429 for e in eventos if e["evento"] != "llm_call")


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
    assert "não viram um item só" in prompt
    assert "Repita o PRIMEIRO item em veiculo_texto" in prompt   # compatibilidade com 1 carro


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
    abertura = "apresente-se em UMA linha como Lia"
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


# --------------------------------------------------------------------------- histórico (fix C)
def test_historico_kwargs_e_o_contrato_do_responder():
    kwargs = brain.historico_kwargs()
    assert kwargs["add_history_to_context"] is True
    assert kwargs["num_history_runs"] == brain.RESPONDER_HISTORY_RUNS == 4
    assert kwargs["system_message_role"] == "system"


def test_historico_nunca_traz_system_do_passado():
    """Nenhum system prompt de run antigo entra no histórico — provado contra a API do agno.

    `num_history_runs` e `num_history_messages` são mutuamente exclusivos no agno 3.0.5, então
    quem garante isso é o `system_message_role`, que o `get_run_messages` usa como `skip_roles`.
    Se um upgrade mudar essa regra, este teste quebra antes de a conta de tokens subir.
    """
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession

    kwargs = brain.historico_kwargs()
    runs = [
        RunOutput(
            run_id=f"r{n}",
            messages=[
                Message(role="system", content=f"PROMPT ANTIGO {n}"),
                Message(role="user", content=f"mensagem {n}"),
                Message(role="assistant", content=f"resposta {n}"),
            ],
        )
        for n in range(1, 7)
    ]
    historico = AgentSession(session_id="s", runs=runs).get_messages(
        last_n_runs=kwargs["num_history_runs"],
        limit=None,
        skip_roles=[kwargs["system_message_role"]],
    )

    assert [m.role for m in historico] == ["user", "assistant"] * 4      # 4 runs, não 6
    assert not any("PROMPT ANTIGO" in str(m.content) for m in historico)
    assert brain.system_do_historico(
        [{"role": m.role, "content": m.content, "from_history": True} for m in historico]
    ) == []


def test_system_do_historico_denuncia_prompt_antigo():
    """O detector que roda em produção: se o agno voltar a mandar system, o trace mostra."""
    historico = [
        {"role": "system", "content": "prompt DESTE turno", "from_history": False},
        {"role": "system", "content": "prompt ANTIGO", "from_history": True},
        {"role": "user", "content": "oi", "from_history": True},
    ]
    assert brain.system_do_historico(historico) == [
        {"role": "system", "content": "prompt ANTIGO", "from_history": True}
    ]


def test_history_runs_respeita_override_do_studio(monkeypatch, tmp_path):
    store = _store_isolado(monkeypatch, tmp_path)
    assert brain.responder_history_runs() == 4
    store.set_overrides("settings", {"responder_history_runs": 6})
    assert brain.responder_history_runs() == 6


# --------------------------------------------------------------------------- validação pós-LLM
def test_guard_descarta_pergunta_de_campo_que_ja_esta_no_estado():
    """s01 real: diretiva era "pergunte o CEP" e o modelo perguntou a idade, que já tinha."""
    state = _state(idade=35, ultima_pergunta="cep")
    texto, achado = brain.guard_resposta(
        "Perfeito, Onix 2022 anotado. Qual é a sua idade?", state, directive="pergunte o CEP"
    )
    assert texto == fallback_text("cep")
    assert achado == {"regra": "campo_ja_preenchido:idade", "trecho": "Qual é a sua idade?"}


def test_guard_deixa_repetir_o_campo_quando_a_diretiva_pediu():
    """Confirmação de ano-modelo e correção de CEP pedem justamente perguntar de novo."""
    state = _state(veiculo_ano=2027, ultima_pergunta="veiculo")
    texto = "Esse 2027 parece ano-modelo. Qual o ano de fabricação?"
    assert brain.guard_resposta(texto, state, directive=directive_for_field("veiculo")) == (texto, None)


def test_guard_descarta_historico_inexistente_no_primeiro_turno():
    """s03 real: "Como te disse antes" na PRIMEIRA mensagem da conversa."""
    state = _state(turnos=1, ultima_pergunta="idade")
    texto, achado = brain.guard_resposta(
        "Oi! Como te disse antes, não precisa desses dados agora.", state, directive="pergunte a idade"
    )
    assert texto == fallback_text("idade")
    assert achado is not None and achado["regra"] == "historico_inexistente"


def test_guard_aceita_referencia_ao_historico_depois_do_primeiro_turno():
    state = _state(turnos=5, ultima_pergunta="plano")
    texto = "Como te disse, a franquia muda por plano."
    assert brain.guard_resposta(texto, state, directive="pergunte o plano") == (texto, None)


@pytest.mark.parametrize(
    "resposta",
    [
        "Entendo perfeitamente. Posso ajustar a franquia para caber melhor no seu bolso, o que acha?",
        "Consigo um desconto pra você fechar hoje.",
        "Faço por menos se você fechar agora.",
        "Consigo negociar essa parcela com você.",
    ],
)
def test_guard_descarta_promessa_de_ajuste_ou_desconto(resposta: str):
    """s10a real: "Posso ajustar a franquia" — a franquia é fixa por plano."""
    state = _state(idade=35, ultima_pergunta="plano", turnos=4)
    texto, achado = brain.guard_resposta(resposta, state, directive="ofereça ver outro plano")
    assert texto == fallback_text("plano")
    assert achado is not None and achado["regra"] == "promessa"


@pytest.mark.parametrize(
    "resposta",
    [
        "Isso, o plano essencial é bem completo.",
        "O Premium é o melhor custo-benefício da casa.",
        "Esse é o ideal para você.",
        "Vale muito a pena.",
    ],
)
def test_guard_descarta_qualificacao_de_plano(resposta: str):
    """s05b real: "o plano essencial é bem completo" — confunde com o plano Completo."""
    state = _state(idade=35, ultima_pergunta="plano", turnos=3)
    texto, achado = brain.guard_resposta(resposta, state, directive="pergunte o plano")
    assert texto == fallback_text("plano")
    assert achado is not None and achado["regra"] == "qualifica_plano"


@pytest.mark.parametrize(
    "resposta",
    [
        "Deve ficar em R$ 180,00 por mês.",
        "Fica 209.90 por mês.",
        "Fica uns 200 reais.",
        "Costuma ficar uns 250 no seu perfil.",
        "Varia de 150 a 300 por mês.",
        "Fica em torno de duzentos por mês.",
    ],
)
def test_guard_descarta_valor_fora_da_cotacao(resposta: str):
    """Guard ampliado: valor exato, forma vaga e número por extenso, todos sem cotação OK."""
    state = _state(ultima_pergunta="cep", turnos=3)
    texto, achado = brain.guard_resposta(resposta, state, directive="peça o CEP")
    assert texto == fallback_text("cep")
    assert achado is not None and achado["regra"] == "valor_inventado"


def test_guard_libera_valor_quando_a_cotacao_veio_da_api():
    state = _state(ultima_pergunta="plano", turnos=4, quote_result=quote_ok())
    texto = "O valor de R$ 209,90 que te passei já inclui assistência."
    assert brain.guard_resposta(texto, state, directive="retome a dúvida") == (texto, None)


def test_guard_libera_franquia_que_estava_no_material_da_diretiva():
    dados = "- Essencial: franquia de R$ 4.500,00\n- Completo: franquia de R$ 3.000,00"
    texto = "O Essencial tem franquia de R$ 4.500,00 e o Completo, R$ 3.000,00."
    assert brain.guard_resposta(texto, _state(turnos=3), directive=dados) == (texto, None)


def test_guard_nao_mexe_em_resposta_boa():
    state = _state(idade=35, ultima_pergunta="veiculo", turnos=2)
    texto = "Anotado! Qual o modelo e o ano de fabricação do carro?"
    assert brain.guard_resposta(texto, state, directive=directive_for_field("veiculo")) == (texto, None)


def test_contem_valor_pega_as_formas_vagas_e_contem_preco_nao():
    assert brain.contem_valor("uns 200") and not contem_preco("uns 200")
    assert brain.contem_valor("de 150 a 300") and not contem_preco("de 150 a 300")
    assert brain.contem_valor("quinhentos") and not contem_preco("quinhentos")
    assert not brain.contem_valor("seu Onix 2019, 35 anos, cep 01310100")
    assert not brain.contem_valor("01.310-100")          # CEP com ponto não é dinheiro


# --------------------------------------------------------------------------- usage / tentativas
class _Metrics:
    """`RunOutput.metrics` no que o brain lê."""

    def __init__(self, entrada: int, saida: int, cache: int = 0) -> None:
        self.input_tokens = entrada
        self.output_tokens = saida
        self.total_tokens = entrada + saida
        self.cache_read_tokens = cache


def _run_com_usage(content, entrada: int, saida: int, cache: int = 0) -> FakeRun:
    run = FakeRun(content=content)
    run.metrics = _Metrics(entrada, saida, cache)
    return run


@pytest.mark.asyncio
async def test_extractor_expoe_usage_e_tentativas_para_o_turno():
    ex = _extractor([_run_com_usage(Extraction(idade=35), 1250, 40, cache=0)])
    assert (await ex.extract("tenho 35", _state(conversation_id="c7"), HOJE)).idade == 35

    uso = ex.drenar_usage("c7")
    assert uso["usage"] == {"input": 1250, "output": 40, "total": 1290, "cache_read": 0}
    assert uso["tentativas"] == 1 and uso["source"] == "llm"
    assert uso["model"]                                  # o log do JSONL não tinha o modelo
    assert ex.drenar_usage("c7") == {}                   # drenou, esqueceu


@pytest.mark.asyncio
async def test_usage_soma_as_tentativas_porque_todas_foram_pagas():
    ex = _extractor([run_erro(ERRO_429), _run_com_usage(Extraction(idade=40), 1200, 30)])
    await ex.extract("40", _state(conversation_id="c8"), HOJE)
    uso = ex.drenar_usage("c8")
    assert uso["tentativas"] == 2
    assert uso["usage"]["input"] == 1200                 # o run que falhou não contou tokens


@pytest.mark.asyncio
async def test_responder_marca_o_fallback_como_fallback_e_nao_como_llm():
    """Fase 3: o fallback determinístico de s07b saiu no log rotulado `source=llm`."""
    resp = _responder([run_erro(ERRO_429) for _ in range(4)])
    saida = await resp.reply("peça o CEP", _state(conversation_id="c9", ultima_pergunta="cep"), "moro em SP")

    assert saida == fallback_text("cep")
    assert saida.source == "fallback"
    uso = resp.drenar_usage("c9")
    assert uso["source"] == "fallback" and uso["tentativas"] == 4


@pytest.mark.asyncio
async def test_responder_ok_sai_como_llm_e_guard_como_fallback():
    ok = _responder([FakeRun(content="Qual o ano do carro?")])
    saida = await ok.reply("pergunte o ano", _state(conversation_id="ca"), "é um Onix")
    assert saida.source == "llm" and ok.drenar_usage("ca")["guard"] is None

    ruim = _responder([FakeRun(content="Posso ajustar a franquia pra você.")])
    state = _state(conversation_id="cb", ultima_pergunta="plano", turnos=4)
    saida = await ruim.reply("ofereça outro plano", state, "tá caro")
    assert saida == fallback_text("plano") and saida.source == "fallback"
    assert ruim.drenar_usage("cb")["guard"]["regra"] == "promessa"


@pytest.mark.asyncio
async def test_guard_emite_evento_llm_guard_no_trace():
    eventos: list[dict] = []
    resp = Responder(
        agent=FakeAgnoAgent([FakeRun(content="Fica uns 200 reais.")]), sleep=SleepFake(), trace=eventos.append
    )
    await resp.reply("peça o CEP", _state(ultima_pergunta="cep", turnos=3), "quanto fica?")

    guards = [e for e in eventos if e["evento"] == "llm_guard"]
    assert len(guards) == 1
    assert guards[0]["regra"] == "valor_inventado"
    assert "uns 200" in guards[0]["trecho"]


# --------------------------------------------------------------------------- timeout por chamada
@pytest.mark.asyncio
async def test_timeout_por_chamada_conta_como_transitorio_e_avisa_uma_vez(monkeypatch):
    """Fase 3 mediu chamadas de 51 s e 70 s com o lead em silêncio. Agora há teto e aviso."""
    monkeypatch.setattr(brain, "LLM_TIMEOUT_S", 0.01)
    avisos: list[int] = []

    class AgentLento:
        def __init__(self) -> None:
            self.chamadas = 0

        async def arun(self, entrada, **kwargs):
            self.chamadas += 1
            import asyncio as aio

            await aio.sleep(0.5)
            return FakeRun(content="tarde demais")

    agent = AgentLento()
    resp = Responder(agent=agent, sleep=SleepFake())

    async def on_slow() -> None:
        avisos.append(1)

    saida = await resp.reply(
        "peça o CEP", _state(ultima_pergunta="cep", turnos=2), "moro em SP", on_slow=on_slow
    )

    assert saida == fallback_text("cep") and saida.source == "fallback"
    assert agent.chamadas == MAX_TENTATIVAS_LLM        # timeout é transitório: re-tenta
    assert avisos == [1]                               # "só um instante" no máximo uma vez


@pytest.mark.asyncio
async def test_sem_on_slow_o_timeout_nao_quebra_o_turno(monkeypatch):
    monkeypatch.setattr(brain, "LLM_TIMEOUT_S", 0.01)

    class AgentLento:
        async def arun(self, entrada, **kwargs):
            import asyncio as aio

            await aio.sleep(0.5)

    saida = await Responder(agent=AgentLento(), sleep=SleepFake()).reply("peça o CEP", _state(), "oi")
    assert saida == fallback_text(None)


def test_parametros_novos_nao_quebram_com_store_sem_a_chave(monkeypatch, tmp_path):
    """`settings.llm_timeout_s` ainda não existe em `runtime_config` (fora do escopo do brief C)."""
    _store_isolado(monkeypatch, tmp_path)
    assert brain.llm_timeout_s() == brain.LLM_TIMEOUT_S == 12.0
    assert brain._param_settings("chave_que_nao_existe", 7) == 7


# --------------------------------------------------------------------------- abertura por template
@pytest.mark.asyncio
async def test_abertura_do_turno_1_e_template_e_nao_gasta_chamada():
    agent = FakeAgnoAgent([])
    resp = Responder(agent=agent, sleep=SleepFake())
    state = _state(conversation_id="cc", turnos=1)

    saida = await resp.reply(directive_for_field("idade"), state, "oi, quero cotar meu carro")

    assert saida == brain.store.text("responder.abertura")
    assert saida.source == "template"
    assert saida.startswith("Oi! Sou a Lia, da AutoSeguro")
    assert len(saida.splitlines()) == 2                # no máximo 2 linhas, como pediu o dono
    assert agent.chamadas == []                        # zero token gasto no turno mais comum
    assert resp.drenar_usage("cc") == {"model": resp._modelo(), "usage": None, "tentativas": 0, "source": "template"}


@pytest.mark.asyncio
async def test_abertura_nao_vale_quando_o_lead_ja_deu_a_idade_ou_o_turno_passou():
    resp = Responder(agent=FakeAgnoAgent([FakeRun(content="Qual o carro?")]), sleep=SleepFake())
    # o lead abriu dizendo a idade: a policy já pede o veículo, e o LLM cuida da abertura
    assert resp.abertura(directive_for_field("veiculo"), _state(turnos=1, idade=35)) is None
    assert resp.abertura(directive_for_field("idade"), _state(turnos=1, idade=35)) is None
    assert resp.abertura(directive_for_field("idade"), _state(turnos=3)) is None
    assert resp.abertura("responda a dúvida do lead", _state(turnos=1)) is None


# --------------------------------------------------------------------------- {planos} no Extractor
def test_prompt_do_extractor_lista_os_planos_da_vitrine_corrente():
    planos = {"planos": [{"id": "essencial"}, {"id": "completo"}, {"id": "premium"}, {"id": "moto"}]}
    prompt = build_extraction_instructions(_state(), HOJE, ferramentas=[], planos=planos)
    assert "só se ele nomear um plano (essencial, completo, premium, moto)" in prompt


def test_sem_planos_o_extractor_usa_o_catalogo_entregue():
    prompt = build_extraction_instructions(_state(), HOJE, ferramentas=[])
    assert f"({brain.PLANOS_PADRAO})" in prompt


def test_bloco_dinamico_do_extractor_fica_no_fim():
    """Prefixo estável em toda chamada é a única economia possível (cache não compensa)."""
    prompt = build_extraction_instructions(_state(idade=35), HOJE, ferramentas=[])
    assert prompt.rstrip().endswith('"2019" é ano se foi o veículo).')
    assert prompt.index("Regras:") < prompt.index("Hoje é 2026-09-01")
    assert prompt.index("intent (exatamente um):") < prompt.index("Já coletado:")


def test_extractor_instructions_cabe_no_orcamento():
    """Teto de 1.200 chars no slot: o Extractor roda em TODA mensagem e era 81 % dos tokens."""
    from agent.defaults import SLOTS as SLOTS_DEF

    assert len(SLOTS_DEF["extractor.instructions"]["default"]) <= 1200


# --------------------------------------------------------------------------- forma das respostas
def test_guardrails_trazem_as_regras_de_forma():
    prompt = build_responder_instructions(_state(turnos=3), "peça o CEP")
    assert "No máximo 2 frases" in prompt
    assert "nunca dois seguidos" in prompt              # emoji em sequência
    assert "Não repita o que outra mensagem já disse" in prompt
    assert "Apresente-se só no primeiro turno, em no máximo 2 linhas." in prompt
    assert "NUNCA qualifique um plano" in prompt
    assert 'NUNCA diga "como te disse"' in prompt


def test_guard_price_pos_cotacao_bloqueia_valor_que_nao_e_o_cotado():
    """Cotação OK torna público o valor cotado, não qualquer valor: negociar preço novo é invenção."""
    state = _state(stage=Stage.APRESENTADO, ultima_pergunta="plano", quote_result=quote_ok())
    inventado = "Consigo fazer por R$ 99,00 no seu caso."
    assert guard_price(inventado, state) != inventado
    cotado = "Fica R$ 209,90 por mês, como te mostrei."
    assert guard_price(cotado, state) == cotado
