import random
import uuid

import httpx
import pytest

from agent.models import QuoteOutcome, QuoteRequest
from agent.quote_client import QuoteClient, QuotePlanosUnavailable


class FakeClock:
    """Relógio controlável: só avança quando algo chama `.advance` (via fake sleep)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_fake_sleep(clock: FakeClock):
    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)

    return _sleep


class ZeroJitterRandom(random.Random):
    """rng determinístico: jitter sempre 0, backoff vira exatamente `base * 2**(n-1)`."""

    def uniform(self, a: float, b: float) -> float:
        return 0.0


def _req(**overrides) -> QuoteRequest:
    base = {
        "plano_id": "completo",
        "idade": 30,
        "veiculo_ano": 2020,
        "cep": "01310100",
        "data_inicio": "2026-09-02",
    }
    base.update(overrides)
    return QuoteRequest(**base)


QUOTE_200_BODY = {
    "plano_id": "completo",
    "plano_nome": "Completo",
    "premio_mensal": 209.9,
    "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto"],
    "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "obs"},
    "moeda": "BRL",
}


def _client(handler, **kwargs) -> tuple[QuoteClient, FakeClock]:
    clock = kwargs.pop("clock", None) or FakeClock()
    sleep = kwargs.pop("sleep", None) or _make_fake_sleep(clock)
    rng = kwargs.pop("rng", None) or ZeroJitterRandom()
    client = QuoteClient(
        base_url="http://quote-api.test",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        clock=clock,
        rng=rng,
        **kwargs,
    )
    return client, clock


@pytest.mark.asyncio
async def test_5xx_depois_200_retry_com_sucesso_duas_tentativas():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "upstream_unavailable"})
        return httpx.Response(200, json=QUOTE_200_BODY)

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.OK
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "http_5xx"
    assert result.attempts[1].status == "ok"
    assert result.quote.premio_mensal == 209.9


@pytest.mark.asyncio
async def test_timeout_chama_on_slow_uma_vez_e_depois_200():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("slow", request=request)
        return httpx.Response(200, json=QUOTE_200_BODY)

    on_slow_calls = {"n": 0}

    async def on_slow() -> None:
        on_slow_calls["n"] += 1

    client, _ = _client(handler)
    result = await client.quote(_req(), on_slow=on_slow)

    assert result.outcome == QuoteOutcome.OK
    assert on_slow_calls["n"] == 1
    assert result.attempts[0].status == "timeout"


@pytest.mark.asyncio
async def test_422_recusa_para_sem_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"error": "cotacao_recusada", "motivo": "Idade acima do limite."})

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.RECUSA
    assert result.motivo_recusa == "Idade acima do limite."
    assert calls["n"] == 1
    assert len(result.attempts) == 1


@pytest.mark.asyncio
async def test_422_detail_pydantic_vira_bug_sem_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"detail": [{"loc": ["idade"], "msg": "field required"}]})

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.BUG
    assert result.erro is not None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_400_payload_invalido_vira_bug_sem_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "payload_invalido", "detalhe": "data invalida"})

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.BUG
    assert "data invalida" in result.erro
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_4x_5xx_esgota_tentativas_e_fica_indisponivel():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream_unavailable"})

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.INDISPONIVEL
    assert len(result.attempts) == 4
    assert all(a.status == "http_5xx" for a in result.attempts)


@pytest.mark.asyncio
async def test_orcamento_estourado_interrompe_antes_da_quarta_tentativa():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream_unavailable"})

    client, _ = _client(handler, budget_s=6.0)
    result = await client.quote(_req())

    assert result.outcome == QuoteOutcome.INDISPONIVEL
    assert len(result.attempts) == 3


def test_backoff_com_jitter_dentro_da_faixa():
    client = QuoteClient(base_url="http://quote-api.test", rng=random.Random(42))
    for n in range(1, 5):
        base = 0.5 * (2 ** (n - 1))
        for _ in range(30):
            valor = client._backoff_seconds(n)
            assert base * 0.5 <= valor <= base * 1.5


@pytest.mark.asyncio
async def test_quote_id_gerado_uma_vez_por_chamada_nao_por_tentativa(monkeypatch):
    original_uuid4 = uuid.uuid4
    calls = {"n": 0}

    def counting_uuid4():
        calls["n"] += 1
        return original_uuid4()

    monkeypatch.setattr("agent.quote_client.uuid.uuid4", counting_uuid4)

    request_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        request_n["n"] += 1
        if request_n["n"] < 2:
            return httpx.Response(500, json={"error": "upstream_unavailable"})
        return httpx.Response(200, json=QUOTE_200_BODY)

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert calls["n"] == 1
    assert result.outcome == QuoteOutcome.OK
    assert len(result.attempts) == 2


@pytest.mark.asyncio
async def test_parse_200_com_pro_rata():
    body = dict(QUOTE_200_BODY)
    body["primeiro_pagamento_pro_rata"] = {
        "dias_no_mes": 30,
        "dias_cobrados": 16,
        "valor_primeiro_pagamento": 111.95,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.quote.pro_rata is not None
    assert result.quote.pro_rata.valor_primeiro_pagamento == 111.95


@pytest.mark.asyncio
async def test_parse_200_sem_pro_rata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=QUOTE_200_BODY)

    client, _ = _client(handler)
    result = await client.quote(_req())

    assert result.quote.pro_rata is None
    assert result.quote.carencia_dias == 30
    assert result.quote.carencia_coberturas == ["roubo", "furto"]


@pytest.mark.asyncio
async def test_get_planos_cacheia_uma_requisicao_para_duas_chamadas():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"planos": [], "regras": {}})

    client, _ = _client(handler)
    p1 = await client.get_planos()
    p2 = await client.get_planos()

    assert p1 is p2
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_planos_falha_levanta_excecao_dedicada():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client, _ = _client(handler)
    with pytest.raises(QuotePlanosUnavailable):
        await client.get_planos()
