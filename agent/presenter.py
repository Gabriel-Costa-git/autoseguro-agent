"""Templates determinísticos das ações que NÃO podem passar pelo LLM.

Tudo que envolve preço, cobertura, carência ou promessa ao lead é renderizado
aqui, com os números vindos exclusivamente do `Quote` (que só nasce da API).
`AskField`/`Reply` são de propósito responsabilidade do Responder (LLM) e
`DoQuote` é do cliente HTTP — pedir render dessas ações é erro de programação.

Os textos são slots editáveis no Studio (`agent/defaults.py` guarda o default =
comportamento entregue). O template só recebe placeholders JÁ formatados: moeda,
lista de coberturas, CEP com hífen e primeiro nome continuam sendo lógica de
código, para que nenhuma edição de texto consiga produzir um valor errado.
A leitura do store é feita NA CHAMADA (hot-reload no Studio).

Tom: WhatsApp, pt-BR, curto, no máximo *negrito*.
"""
from __future__ import annotations

from typing import Any

from agent.models import (
    Action,
    AskPlan,
    ConfirmCep,
    Handoff,
    LeadState,
    PlanoResumo,
    Present,
    QuoteOutcome,
    Refuse,
    SendText,
)
from agent.runtime_config import ConfigError, store


def _t(key: str, **ctx: Any) -> str:
    """Texto ativo do slot. Lido a cada chamada para o Studio refletir na hora."""
    return store.text(key, **ctx)


def render(action: Action, state: LeadState) -> str:
    """Renderiza uma ação de template. Levanta ValueError para as ações do LLM/cliente."""
    if isinstance(action, SendText):
        return action.text
    if isinstance(action, ConfirmCep):
        return _confirm_cep(action)
    if isinstance(action, AskPlan):
        return _ask_plan(action)
    if isinstance(action, Present):
        return _present(action)
    if isinstance(action, Refuse):
        return _refuse(action)
    if isinstance(action, Handoff):
        return _handoff(action, state)
    raise ValueError(
        f"{type(action).__name__} não é ação de template: "
        "AskField e Reply são do Responder (LLM) e DoQuote é do quote_client."
    )


# --------------------------------------------------------------------------- templates
def _confirm_cep(action: ConfirmCep) -> str:
    return _t(
        "presenter.confirm_cep",
        cep=_cep_formatado(action.cep),
        cidade=action.cidade,
        uf=action.uf,
    )


def _ask_plan(action: AskPlan) -> str:
    linhas = [_t("presenter.ask_plan.cabecalho"), ""]
    linhas += [_linha_plano(p) for p in action.planos]
    linhas += ["", _t("presenter.ask_plan.rodape")]
    return "\n".join(linhas)


def _linha_plano(plano: PlanoResumo) -> str:
    return _t(
        "presenter.ask_plan.linha_plano",
        nome=plano.nome,
        franquia=_brl(plano.franquia),
        coberturas=_lista(_coberturas(plano.coberturas)),
    )


def _present(action: Present) -> str:
    quote = action.result.quote
    if action.result.outcome is not QuoteOutcome.OK or quote is None:
        raise ValueError("Present só renderiza cotação com outcome OK e quote preenchido.")

    premio = _brl(quote.premio_mensal)
    linhas = [
        _t("presenter.present.titulo", plano_nome=quote.plano_nome),
        "",
        _t("presenter.present.preco", premio=premio),
        _t("presenter.present.franquia", franquia=_brl(quote.franquia)),
        _t("presenter.present.coberturas", coberturas=_lista(_coberturas(quote.coberturas))),
    ]

    if quote.carencia_coberturas and quote.carencia_dias:
        aviso = _t(
            "presenter.present.carencia",
            coberturas_carencia=_lista(_coberturas(quote.carencia_coberturas)),
            dias=quote.carencia_dias,
        )
        linhas += ["", aviso]

    if quote.pro_rata is not None:
        linhas.append(
            _t(
                "presenter.present.pro_rata",
                valor=_brl(quote.pro_rata.valor_primeiro_pagamento),
                dias=quote.pro_rata.dias_cobrados,
                premio=premio,
            )
        )

    if action.cep_ausente:
        linhas.append(_t("presenter.present.aviso_cep_ausente"))

    linhas += ["", _t("presenter.present.cta")]
    return "\n".join(linhas)


def _refuse(action: Refuse) -> str:
    explicacao = _t("presenter.refuse", motivo=_minuscula_inicial(action.motivo))
    return f"{explicacao}\n\n{_t('presenter.refuse.fechamento')}"


def _handoff(action: Handoff, state: LeadState) -> str:
    texto = _t(f"presenter.handoff.{action.reason.value}")
    if state.lead_nome:
        return f"{state.lead_nome.split()[0]}, {_minuscula_inicial(texto)}"
    return texto


# --------------------------------------------------------------------------- formatação
def _brl(valor: float) -> str:
    """Formato brasileiro: milhar com ponto, decimal com vírgula."""
    return f"R$ {valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _lista(itens: list[str]) -> str:
    """Junta com vírgula e 'e' no último — texto de vendedor, não de sistema."""
    if not itens:
        return "as coberturas do plano"
    if len(itens) == 1:
        return itens[0]
    return f"{', '.join(itens[:-1])} e {itens[-1]}"


def _coberturas(chaves: list[str]) -> list[str]:
    return [_cobertura(c) for c in chaves]


def _cobertura(chave: str) -> str:
    """Nome legível da cobertura; cobertura nova da API cai no fallback sem quebrar."""
    try:
        return _t(f"presenter.cobertura.{chave}")
    except ConfigError:
        return chave.replace("_", " ")


def _cep_formatado(cep: str) -> str:
    return f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep


def _minuscula_inicial(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        return texto
    frase = texto[0].lower() + texto[1:]
    return frase if frase.endswith((".", "!", "?")) else f"{frase}."


__all__ = ["render"]
