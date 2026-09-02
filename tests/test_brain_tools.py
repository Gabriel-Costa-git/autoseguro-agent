"""O Responder com as tools do painel: construção do Agent, cache e o evento `tool_call`.

Nada de SDK do agno nem de rede: `agno.agent.Agent` é substituído por um gravador de kwargs, e o
`Agent` do Responder, quando o turno roda, é o dublê de `tests/fakes.py`. O ponto destes testes é
o INVARIANTE da fase: sem tool cadastrada, o Agent é construído exatamente como na entrega.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from agent import brain
from agent.conversation import Conversation, InMemoryStateStore
from agent.models import Extraction, Inbound, Intent, Reply
from agent.observability import ConversationLogger
from agent.runtime_config import ConfigStore
from tests.fakes import (
    FakeExtractor,
    FakeQuoteClient,
    FakeRules,
    FakeRun,
    ScriptedPolicy,
    render_fake,
)

HOJE = date(2026, 9, 1)

TOOL = {
    "tipo": "http",
    "descricao": "Consulta a apólice do cliente pelo CPF.",
    "instrucoes": "Confirme só os 4 últimos dígitos.",
    "parametros": {"cpf": {"tipo": "string", "obrigatorio": True}},
    "http": {"metodo": "GET", "url": "https://api.exemplo.test/apolices/{cpf}"},
}

EVENTO = {
    "tool": "consulta_apolice",
    "args": {"cpf": "12345678901"},
    "status": "ok",
    "latency_ms": 12,
    "resultado": '{"numero": "AP-1"}',
}


@pytest.fixture
def loja(tmp_path, monkeypatch) -> ConfigStore:
    """Store de config isolado, injetado no módulo `brain` (é dele que o Responder lê)."""
    store = ConfigStore(tmp_path / "config")
    store.ensure_files()
    monkeypatch.setattr(brain, "store", store)
    return store


@pytest.fixture
def agentes_construidos(monkeypatch) -> list[dict]:
    """Grava os kwargs de cada `Agent(...)` construído, sem carregar o SDK de verdade."""
    chamadas: list[dict] = []

    class AgentGravador:
        def __init__(self, **kwargs):
            chamadas.append(kwargs)

    import agno.agent

    monkeypatch.setattr(agno.agent, "Agent", AgentGravador)
    monkeypatch.setattr(brain._AgenteLLM, "_gemini", lambda self, t: f"gemini(temp={t})")
    monkeypatch.setattr(brain._AgenteLLM, "_sqlite_db", lambda self: "sqlite-db")
    return chamadas


# --------------------------------------------------------------------------- construção do Agent
def test_sem_tool_o_agent_e_o_da_entrega(loja, agentes_construidos):
    brain.Responder().agente()

    (kwargs,) = agentes_construidos
    assert set(kwargs) == {
        "name", "model", "db", "instructions", "post_hooks",
        "add_history_to_context", "num_history_runs", "markdown", "telemetry",
    }
    assert "tools" not in kwargs
    assert kwargs["name"] == "autoseguro-responder"
    assert kwargs["model"] == "gemini(temp=0.4)"
    assert kwargs["db"] == "sqlite-db"
    assert kwargs["instructions"] is brain._responder_instructions
    assert kwargs["post_hooks"] == [brain._price_guard_hook]
    assert kwargs["add_history_to_context"] is True
    assert kwargs["num_history_runs"] == 8
    assert kwargs["markdown"] is False
    assert kwargs["telemetry"] is False


def test_com_tool_muda_so_o_kwarg_tools(loja, agentes_construidos):
    brain.Responder().agente()                       # antes: sem registro
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    brain.Responder().agente()                       # depois: uma tool habilitada

    sem, com = agentes_construidos
    tools = com.pop("tools")
    assert com == sem                                # nada mais no Agent mudou
    assert [t.name for t in tools] == ["consulta_apolice"]
    assert tools[0].instructions == "Confirme só os 4 últimos dígitos."


def test_tool_desabilitada_nao_vai_para_o_agent(loja, agentes_construidos):
    loja.upsert_custom_tool("consulta_apolice", {**TOOL, "enabled": False})
    brain.Responder().agente()
    assert "tools" not in agentes_construidos[0]


def test_extractor_nunca_recebe_tools(loja, agentes_construidos):
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    brain.Extractor().agente()
    assert "tools" not in agentes_construidos[0]


# --------------------------------------------------------------------------- cache / hot-reload
def test_chave_do_responder_muda_quando_o_registro_muda(loja, agentes_construidos):
    responder = brain.Responder()
    chave_sem = responder._chave()
    responder.agente()

    loja.upsert_custom_tool("consulta_apolice", TOOL)
    assert responder._chave() != chave_sem
    responder.agente()
    assert len(agentes_construidos) == 2             # reconstruiu no hot-reload

    responder.agente()
    assert len(agentes_construidos) == 2             # sem mudança, reusa o Agent em cache


def test_chave_do_extractor_ignora_as_tools(loja):
    extractor = brain.Extractor()
    chave = extractor._chave()
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    assert extractor._chave() == chave


# --------------------------------------------------------------------------- evento tool_call
def _responder_que_chama_tool(loja) -> brain.Responder:
    """Responder com um agno falso que, como o de verdade, executa a tool durante o `arun`."""
    responder = brain.Responder(agent=None)

    class AgnoQueChamaTool:
        def __init__(self) -> None:
            self.chamadas: list[str] = []

        async def arun(self, entrada, **kwargs):
            self.chamadas.append(entrada)
            responder._registrar_tool_call(dict(EVENTO))
            return FakeRun(content="Sua apólice terminada em 1 está ativa.")

    responder._agent_injetado = AgnoQueChamaTool()
    return responder


@pytest.mark.asyncio
async def test_reply_coleta_as_tool_calls_do_turno(loja):
    from agent.models import LeadState

    responder = _responder_que_chama_tool(loja)
    state = LeadState(conversation_id="c-1")

    texto = await responder.reply("fale da apólice", state, "e a minha apólice?")

    assert texto == "Sua apólice terminada em 1 está ativa."
    assert responder.drenar_tool_calls("c-1") == [EVENTO]
    assert responder.drenar_tool_calls("c-1") == []      # drenou, esqueceu
    assert responder.drenar_tool_calls("outra") == []    # não vaza para outra conversa


@pytest.mark.asyncio
async def test_turno_grava_tool_call_no_jsonl_com_pii_mascarada(loja, tmp_path):
    responder = _responder_que_chama_tool(loja)
    conv = Conversation(
        rules=FakeRules(),
        quote_client=FakeQuoteClient([]),
        extractor=FakeExtractor([Extraction()]),
        responder=responder,
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        next_action=ScriptedPolicy([lambda s, e: (s, [Reply(directive="fale da apólice")])]),
        render=render_fake,
        logger_factory=ConversationLogger,
    )

    async def emit(_out):
        return None

    await conv.handle(Inbound(conversation_id="c-1", message_id="m1", text="e a minha apólice?"), emit)

    eventos = [
        json.loads(linha)
        for linha in (tmp_path / "c-1.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [e["event"] for e in eventos]
    assert "tool_call" in kinds
    # a tool rodou DENTRO da chamada do responder: o evento vem logo antes do `llm_call` dele
    llm_responder = max(i for i, e in enumerate(eventos) if e["event"] == "llm_call")
    assert kinds.index("tool_call") == llm_responder - 1
    assert eventos[llm_responder]["data"]["papel"] == "responder"

    tool_call = next(e for e in eventos if e["event"] == "tool_call")
    assert tool_call["message_id"] == "m1"
    assert tool_call["data"]["tool"] == "consulta_apolice"
    assert tool_call["data"]["status"] == "ok"
    assert tool_call["data"]["latency_ms"] == 12
    assert tool_call["data"]["args"]["cpf"] == "***.***.***-**"   # PII mascarada como no resto do log


@pytest.mark.asyncio
async def test_responder_sem_tools_nao_gera_evento(loja, tmp_path):
    class AgnoMudo:
        async def arun(self, entrada, **kwargs):
            return FakeRun(content="Claro, me diz sua idade?")

    responder = brain.Responder(agent=AgnoMudo())
    conv = Conversation(
        rules=FakeRules(),
        quote_client=FakeQuoteClient([]),
        extractor=FakeExtractor([Extraction()]),
        responder=responder,
        log_dir=tmp_path,
        store=InMemoryStateStore(),
        today=lambda: HOJE,
        next_action=ScriptedPolicy([lambda s, e: (s, [Reply(directive="pergunte a idade")])]),
        render=render_fake,
        logger_factory=ConversationLogger,
    )

    async def emit(_out):
        return None

    await conv.handle(Inbound(conversation_id="c-2", message_id="m1", text="oi"), emit)

    linhas = (tmp_path / "c-2.jsonl").read_text(encoding="utf-8").splitlines()
    assert "tool_call" not in [json.loads(linha)["event"] for linha in linhas]


# --------------------------------------------------------------------------- prompt do Extractor
def _prompt(**kw) -> str:
    from agent.models import LeadState

    return brain.build_extraction_instructions(LeadState(conversation_id="c-1"), HOJE, **kw)


def _intents_do_prompt(prompt: str) -> list[str]:
    bloco = prompt.split("intent (escolha exatamente um):\n", 1)[1]
    return [linha[2:].split(":", 1)[0] for linha in bloco.splitlines() if linha.startswith("- ")]


def test_sem_tool_o_prompt_do_extractor_nao_tem_consulta(loja):
    prompt = _prompt()
    assert "- consulta:" not in prompt
    # todos os outros intents continuam lá, na ordem do enum
    assert _intents_do_prompt(prompt) == [i.value for i in Intent if i is not Intent.CONSULTA]


def test_com_tool_o_prompt_ganha_o_intent_consulta_com_as_ferramentas(loja):
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    loja.upsert_custom_tool("consulta_cep", {
        **TOOL, "descricao": "Descobre a cidade de um CEP.",
        "parametros": {"cep": {"tipo": "string", "obrigatorio": True}},
        "http": {"metodo": "GET", "url": "https://x.test/{cep}"},
    })
    prompt = _prompt()

    assert "- consulta: pergunta do lead que UMA DESTAS ferramentas" in prompt
    assert "  - consulta_apolice: Consulta a apólice do cliente pelo CPF." in prompt
    assert "  - consulta_cep: Descobre a cidade de um CEP." in prompt
    # a linha entra ANTES de fora_de_escopo (mesma ordem do enum)
    assert _intents_do_prompt(prompt) == [i.value for i in Intent]


def test_tool_desligada_nao_entra_no_prompt(loja):
    loja.upsert_custom_tool("consulta_apolice", {**TOOL, "enabled": False})
    assert "- consulta:" not in _prompt()


def test_ferramentas_injetadas_vencem_o_store(loja):
    """`ferramentas=[]` é como os goldens fixam o comportamento entregue."""
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    assert "- consulta:" in _prompt()
    assert "- consulta:" not in _prompt(ferramentas=[])


def test_texto_do_intent_consulta_vem_do_slot(loja):
    loja.upsert_custom_tool("consulta_apolice", TOOL)
    loja.add_version("intent.consulta", "v2", "o lead pergunta sobre: {ferramentas}")
    assert "- consulta: o lead pergunta sobre:   - consulta_apolice:" in _prompt()

