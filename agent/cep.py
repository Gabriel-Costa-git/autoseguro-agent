"""Consulta soft ao ViaCEP: só confirma cidade/UF para o lead, nunca bloqueia
o fluxo. Qualquer falha (rede, timeout, 4xx/5xx) vira `existe=None`, que a
política trata como "não deu pra confirmar, seguir sem checar"."""
from __future__ import annotations

import httpx

from agent.models import CepInfo
from agent.runtime_config import store


async def lookup_cep(
    cep8: str,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
    url: str | None = None,
) -> CepInfo:
    """Consulta `{viacep_url}/{cep8}/json/`. Nunca levanta: erro vira `existe=None`.

    `timeout_s`/`url` em `None` vêm do store (`tools.viacep.*`) na hora da chamada, para
    o Studio poder apontar para outro endpoint ou afrouxar o timeout sem reiniciar.
    """
    base = (url if url is not None else store.param("tools.viacep.url")).rstrip("/")
    if timeout_s is None:
        timeout_s = float(store.param("tools.viacep.timeout_s"))
    url = f"{base}/{cep8}/json/"
    owns_client = client is None
    http_client = client or httpx.AsyncClient()
    try:
        resp = await http_client.get(url, timeout=timeout_s)
    except (httpx.TimeoutException, httpx.TransportError):
        return CepInfo(cep=cep8, existe=None)
    finally:
        if owns_client:
            await http_client.aclose()

    if resp.status_code != 200:
        return CepInfo(cep=cep8, existe=None)

    try:
        data = resp.json()
    except ValueError:
        return CepInfo(cep=cep8, existe=None)

    if data.get("erro"):
        return CepInfo(cep=cep8, existe=False)

    return CepInfo(cep=cep8, existe=True, cidade=data.get("localidade"), uf=data.get("uf"))
