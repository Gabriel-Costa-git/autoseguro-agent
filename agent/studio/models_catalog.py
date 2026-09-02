"""Catálogo de modelos do Gemini para o seletor do Studio.

O modelo do agente sai do `.env` e vira escolha do operador: a lista de opções é
consultada na API do Google sob demanda (botão "Atualizar") e fica cacheada em
`config/models.json` — o Studio não pode depender de rede a cada abertura de página,
e a lista muda de mês em mês, não de minuto em minuto.

    {"atualizado_em": "<iso>", "modelos": [{"id": "gemini-2.5-flash", "nome": "Gemini 2.5 Flash"}]}

Quem SELECIONA o modelo é o `ConfigStore` (`PUT /api/config {"gemini_model": ...}`);
aqui só existe a lista. Escrita atômica (`tmp` + `os.replace`), o mesmo padrão do
`ConfigStore`. O cliente é injetável (`client_factory`) para o teste não tocar a rede.

O filtro é mais estrito que `generateContent`: um refresh real trouxe 40 modelos, e TTS,
geração de imagem, embedding, vídeo, áudio ao vivo e afins também declaram essa ação —
mas nenhum deles serve para o Extractor/Responder conversarem. Como a família muda a cada
release, o corte é por marca no id (o que a API chama de `models/<id>`), não por lista fixa.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ARQUIVO = "models.json"
ACAO_NECESSARIA = "generateContent"

# Marcas no id que denunciam um modelo que não é de texto/chat (ex.: `gemini-2.5-flash-preview-tts`,
# `gemini-2.5-flash-image`, `gemini-live-2.5-flash-preview`, `gemini-robotics-er-1.5-preview`).
MARCAS_NAO_TEXTO = (
    "tts", "image", "imagen", "embedding", "veo", "audio", "live", "native-audio",
    "computer-use", "robotics",
    "banana", "lyria", "transcribe", "deep-research", "antigravity", "omni",
)

# Gemma: as variantes de texto ficam (`gemma-3-27b-it`, `codegemma-*`); saem as que o próprio
# nome denuncia como outra coisa — visão (`paligemma`) e classificador de segurança (`shieldgemma`).
MARCAS_GEMMA_NAO_TEXTO = ("paligemma", "shieldgemma")


class ModelsError(RuntimeError):
    """Falha ao atualizar o catálogo (sem chave, rede fora, resposta inesperada) → 400 no Studio."""


def _client_padrao(api_key: str) -> Any:
    # Import tardio: o SDK só é carregado quando alguém aperta "Atualizar".
    from google import genai

    return genai.Client(api_key=api_key)


def _path(config_dir: Path) -> Path:
    return Path(config_dir) / ARQUIVO


def listar(config_dir: Path) -> dict[str, Any] | None:
    """Catálogo em cache, ou `None` se ainda não foi atualizado (ou o arquivo está ilegível)."""
    path = _path(config_dir)
    if not path.is_file():
        return None
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(dados, dict) or not isinstance(dados.get("modelos"), list):
        return None
    modelos = [
        {"id": m["id"], "nome": m.get("nome") or m["id"]}
        for m in dados["modelos"]
        if isinstance(m, dict) and m.get("id")
    ]
    return {"atualizado_em": dados.get("atualizado_em"), "modelos": modelos}


def e_modelo_de_texto(ident: str) -> bool:
    """`False` para o que responde `generateContent` mas não conversa (TTS, imagem, áudio...)."""
    baixo = ident.lower()
    return not any(marca in baixo for marca in MARCAS_NAO_TEXTO + MARCAS_GEMMA_NAO_TEXTO)


def _entrada(modelo: Any) -> dict[str, str] | None:
    """Um `Model` da API vira `{id, nome}`; devolve `None` para o que não serve ao agente."""
    acoes = getattr(modelo, "supported_actions", None) or []
    if ACAO_NECESSARIA not in acoes:
        return None
    nome_api = getattr(modelo, "name", None) or ""
    ident = nome_api.removeprefix("models/")
    if not ident or not e_modelo_de_texto(ident):
        return None
    return {"id": ident, "nome": getattr(modelo, "display_name", None) or ident}


def atualizar(
    config_dir: Path,
    api_key: str | None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Consulta a API, filtra o que serve para conversar e grava o cache. Levanta `ModelsError`."""
    if not api_key:
        raise ModelsError("GOOGLE_API_KEY não configurada: não dá para consultar a lista de modelos.")
    fabrica = client_factory or _client_padrao
    try:
        cliente = fabrica(api_key)
        brutos = list(cliente.models.list())
    except Exception as exc:
        # Rede, credencial inválida, mudança de SDK: tudo vira 400 com a causa no texto.
        raise ModelsError(f"falha ao consultar os modelos ({type(exc).__name__}): {exc}") from exc

    modelos = [entrada for entrada in (_entrada(m) for m in brutos) if entrada is not None]
    if not modelos:
        raise ModelsError("a API não devolveu nenhum modelo de texto com generateContent.")

    dados = {"atualizado_em": datetime.now(UTC).isoformat(), "modelos": modelos}
    _gravar(Path(config_dir), dados)
    return dados


def _gravar(config_dir: Path, dados: dict[str, Any]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = _path(config_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
