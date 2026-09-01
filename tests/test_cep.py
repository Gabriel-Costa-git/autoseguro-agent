import httpx
import pytest

from agent.cep import lookup_cep


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_cep_inexistente_devolve_existe_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"erro": "true"})

    resultado = await lookup_cep("00000000", client=_client(handler))
    assert resultado.existe is False
    assert resultado.cidade is None


@pytest.mark.asyncio
async def test_cep_valido_devolve_cidade_e_uf():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"cep": "01310-100", "logradouro": "Av. Paulista", "localidade": "São Paulo", "uf": "SP"},
        )

    resultado = await lookup_cep("01310100", client=_client(handler))
    assert resultado.existe is True
    assert resultado.cidade == "São Paulo"
    assert resultado.uf == "SP"


@pytest.mark.asyncio
async def test_cep_malformado_400_devolve_existe_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>bad request</html>")

    resultado = await lookup_cep("123", client=_client(handler))
    assert resultado.existe is None
    assert resultado.cidade is None


@pytest.mark.asyncio
async def test_cep_timeout_devolve_existe_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=request)

    resultado = await lookup_cep("01310100", client=_client(handler))
    assert resultado.existe is None


@pytest.mark.asyncio
async def test_cep_erro_de_rede_devolve_existe_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    resultado = await lookup_cep("01310100", client=_client(handler))
    assert resultado.existe is None
