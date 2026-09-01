"""Templates determinísticos das ações que NÃO podem passar pelo LLM.

Tudo que envolve preço, cobertura, carência ou promessa ao lead é renderizado
aqui, com os números vindos exclusivamente do `Quote` (que só nasce da API).
`AskField`/`Reply` são de propósito responsabilidade do Responder (LLM) e
`DoQuote` é do cliente HTTP — pedir render dessas ações é erro de programação.

Tom: WhatsApp, pt-BR, curto, no máximo *negrito*.
"""
from __future__ import annotations

from agent.models import (
    Action,
    AskPlan,
    ConfirmCep,
    Handoff,
    HandoffReason,
    LeadState,
    PlanoResumo,
    Present,
    QuoteOutcome,
    Refuse,
    SendText,
)

# Nomes de cobertura como o lead fala, não como a API guarda.
COBERTURAS_LEGIVEIS: dict[str, str] = {
    "colisao": "colisão",
    "roubo": "roubo",
    "furto": "furto",
    "terceiros": "danos a terceiros",
    "vidros": "vidros",
    "carro_reserva": "carro reserva",
    "assistencia_24h": "assistência 24h",
}

TEXTO_HANDOFF: dict[HandoffReason, str] = {
    HandoffReason.LEAD_ACEITOU: (
        "Perfeito! Um consultor vai finalizar com você — já passei seus dados e a "
        "cotação pra ele. É só aguardar aqui mesmo que ele te chama."
    ),
    HandoffReason.LEAD_PEDIU_HUMANO: (
        "Claro! Já chamei um consultor e passei o que a gente conversou. "
        "Ele assume por aqui em instantes."
    ),
    HandoffReason.COTACAO_INDISPONIVEL: (
        "Nosso sistema de cotação está instável agora e eu não vou te passar um valor "
        "chutado. Guardei seus dados e um consultor te retorna com o valor certinho."
    ),
    HandoffReason.ERRO_INTERNO: (
        "Deu um problema técnico aqui na hora de cotar. Pra não te deixar esperando, "
        "passei seu caso pra um consultor — ele te retorna."
    ),
    HandoffReason.FORA_DE_ESCOPO: (
        "Isso aqui eu não consigo resolver: cuido só de cotação de seguro auto novo. "
        "Já estou passando pra um consultor que te ajuda com isso."
    ),
    HandoffReason.NEGOCIACAO: (
        "Condição especial quem pode avaliar é um consultor. Já passei sua cotação "
        "pra ele, ele fala com você por aqui."
    ),
    HandoffReason.SEM_PROGRESSO: (
        "Acho que por aqui não estou conseguindo te ajudar direito. Vou chamar um "
        "consultor pra falar com você."
    ),
}


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
    return (
        f"Achei aqui: {_cep_formatado(action.cep)} — {action.cidade}/{action.uf}. "
        "É aí que o carro fica?"
    )


def _ask_plan(action: AskPlan) -> str:
    linhas = ["Tenho três planos. Olha o que cada um cobre:", ""]
    linhas += [_linha_plano(p) for p in action.planos]
    linhas += ["", "Qual deles quer que eu cote pra você?"]
    return "\n".join(linhas)


def _linha_plano(plano: PlanoResumo) -> str:
    return (
        f"*{plano.nome}* — franquia de {_brl(plano.franquia)}. "
        f"Cobre {_lista(_coberturas(plano.coberturas))}."
    )


def _present(action: Present) -> str:
    quote = action.result.quote
    if action.result.outcome is not QuoteOutcome.OK or quote is None:
        raise ValueError("Present só renderiza cotação com outcome OK e quote preenchido.")

    linhas = [
        f"Cotei aqui o plano *{quote.plano_nome}*:",
        "",
        f"• *{_brl(quote.premio_mensal)}/mês*",
        f"• Franquia de {_brl(quote.franquia)}",
        f"• Cobre {_lista(_coberturas(quote.coberturas))}",
    ]

    if quote.carencia_coberturas and quote.carencia_dias:
        carencia = _lista(_coberturas(quote.carencia_coberturas))
        aviso = (
            f"Importante: {carencia} só passam a valer {quote.carencia_dias} dias depois "
            "do início da vigência (carência)."
        )
        linhas += ["", aviso]

    if quote.pro_rata is not None:
        pro_rata = quote.pro_rata
        linhas.append(
            f"O primeiro pagamento fica em {_brl(pro_rata.valor_primeiro_pagamento)}, "
            f"referente a {pro_rata.dias_cobrados} dias, e depois "
            f"{_brl(quote.premio_mensal)}/mês."
        )

    if action.cep_ausente:
        linhas.append(
            "Como não tenho seu CEP, esse valor é uma estimativa e pode subir quando a "
            "gente confirmar a região."
        )

    linhas += ["", "Quer fechar? Um consultor finaliza com você. Ou prefere ver outro plano?"]
    return "\n".join(linhas)


def _refuse(action: Refuse) -> str:
    return (
        "Vou ser sincero com você: não temos um plano que se encaixe no seu perfil. "
        f"O motivo é que {_minuscula_inicial(action.motivo)}\n\n"
        "Agradeço muito o contato e espero te atender numa outra oportunidade!"
    )


def _handoff(action: Handoff, state: LeadState) -> str:
    texto = TEXTO_HANDOFF[action.reason]
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
    return [COBERTURAS_LEGIVEIS.get(c, c.replace("_", " ")) for c in chaves]


def _cep_formatado(cep: str) -> str:
    return f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep


def _minuscula_inicial(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        return texto
    frase = texto[0].lower() + texto[1:]
    return frase if frase.endswith((".", "!", "?")) else f"{frase}."


__all__ = ["COBERTURAS_LEGIVEIS", "TEXTO_HANDOFF", "render"]
