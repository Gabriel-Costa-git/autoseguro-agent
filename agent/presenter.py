"""Templates determinísticos das ações que NÃO podem passar pelo LLM.

Tudo que envolve preço, cobertura, carência ou promessa ao lead é renderizado
aqui, com os números vindos exclusivamente do `Quote` (que só nasce da API).
`AskField`/`Reply` são de propósito responsabilidade do Responder (LLM) e
`DoQuotes` é do cliente HTTP — pedir render dessas ações é erro de programação.

Os textos são slots editáveis no Studio (`agent/defaults.py` guarda o default =
comportamento entregue). O template só recebe placeholders JÁ formatados: moeda,
lista de coberturas, CEP com hífen e primeiro nome continuam sendo lógica de
código, para que nenhuma edição de texto consiga produzir um valor errado.
A leitura do store é feita NA CHAMADA (hot-reload no Studio).

Tom: WhatsApp, pt-BR, curto, no máximo *negrito*.

Formato por CANAL (`render(action, state, canal)`): os blocos são os mesmos nos três canais;
o que muda é a marcação. No WhatsApp o `*negrito*` do rótulo é o que dá hierarquia à mensagem;
no CLI e no Lab o asterisco seria lixo na tela, então ele cai. O canal sai do `state.origem`
(que o `Inbound` já traz) quando ninguém passa `canal` — nenhum chamador precisa mudar.
"""
from __future__ import annotations

import re
from typing import Any

from agent.models import (
    Action,
    AskPlan,
    ConfirmCep,
    Handoff,
    HandoffReason,
    LeadState,
    PlanoResumo,
    Present,
    PresentMany,
    Quote,
    QuoteOutcome,
    QuoteResult,
    Refuse,
    SendText,
    VeiculoColetado,
)
from agent.runtime_config import ConfigError, store


def _t(key: str, **ctx: Any) -> str:
    """Texto ativo do slot. Lido a cada chamada para o Studio refletir na hora."""
    return store.text(key, **ctx)


CANAIS = ("whatsapp", "cli", "lab")

# `*negrito*` de rótulo: um par de asteriscos sem quebra de linha no meio.
_NEGRITO_RE = re.compile(r"\*(\S[^*\n]*?\S|\S)\*")


def canal_de(origem: str | None) -> str:
    """Canal de formatação a partir de `Inbound.origem` (`whatsapp:<instância>`, `cli`, `lab`).

    Origem desconhecida (ou ausente, em replay de log antigo) formata como WhatsApp: é o
    canal do produto, e um asterisco a mais no terminal é menos grave que a mensagem chapada
    para o lead.
    """
    valor = (origem or "").lower()
    if valor.startswith("whatsapp"):
        return "whatsapp"
    return valor if valor in CANAIS else "whatsapp"


def _para_canal(texto: str, canal: str) -> str:
    return texto if canal == "whatsapp" else _NEGRITO_RE.sub(r"\1", texto)


def render(action: Action, state: LeadState, canal: str | None = None) -> str:
    """Renderiza uma ação de template. Levanta ValueError para as ações do LLM/cliente.

    `canal` é opcional: sem ele, sai do `state.origem`.
    """
    return _para_canal(_texto(action, state), canal or canal_de(state.origem))


def _texto(action: Action, state: LeadState) -> str:
    if isinstance(action, SendText):
        return action.text
    if isinstance(action, ConfirmCep):
        return _confirm_cep(action)
    if isinstance(action, AskPlan):
        return vitrine(action.planos)
    if isinstance(action, Present):
        return _present(action, state)
    if isinstance(action, PresentMany):
        return _present_many(action, state)
    if isinstance(action, Refuse):
        return _refuse(action)
    if isinstance(action, Handoff):
        return _handoff(action, state)
    raise ValueError(
        f"{type(action).__name__} não é ação de template: "
        "AskField e Reply são do Responder (LLM) e DoQuotes é do quote_client."
    )


# --------------------------------------------------------------------------- templates
def _confirm_cep(action: ConfirmCep) -> str:
    return _t(
        "presenter.confirm_cep",
        cep=_cep_formatado(action.cep),
        cidade=action.cidade,
        uf=action.uf,
    )


def vitrine(planos: list[PlanoResumo], canal: str | None = None) -> str:
    """A vitrine dos planos: uma linha por plano e a pergunta. SEM preço — ele só sai da cotação.

    Pública porque é a resposta certa para "me fala sobre os planos" venha ela pelo `AskPlan` da
    policy ou de uma dúvida do lead: o catálogo é dado, não texto de modelo, e o lead tem de ver
    sempre a MESMA lista. Nunca peça isso ao LLM.
    """
    linhas = [_linha_plano(p) for p in planos]
    linhas.append(_t("presenter.ask_plan.rodape"))
    return _para_canal("\n".join(linhas), canal or "whatsapp")


def nomes_de_coberturas(chaves: list[str]) -> list[str]:
    """Nomes legíveis das coberturas (`carro_reserva` → "carro reserva"), sem repetir.

    Público porque o prompt do Responder precisa da MESMA lista que o lead vê nas mensagens:
    é ela que diz ao modelo o que existe — e, por tabela, o que ele não pode inventar.
    """
    vistos: list[str] = []
    for nome in _coberturas(chaves):
        if nome not in vistos:
            vistos.append(nome)
    return vistos


def resumo_dos_planos(planos: list[PlanoResumo], carencia: dict | None = None) -> str:
    """Os planos em texto para o PROMPT (dúvida sobre o produto): franquia e coberturas, sem preço.

    Fica aqui, e não na policy, pela mesma razão de sempre: quem escreve dinheiro (a franquia) é o
    presenter, com `_brl`. Preço mensal não entra — ele só existe depois da cotação.
    """
    linhas = [
        f"- {p.nome}: franquia de {_brl(p.franquia)}; cobre {_lista(nomes_de_coberturas(p.coberturas))}"
        for p in planos
    ]
    # `/planos` chama de `coberturas_com_carencia`; a resposta do `/quote` chama de `coberturas`.
    dados = carencia or {}
    coberturas_carencia = dados.get("coberturas_com_carencia") or dados.get("coberturas") or []
    dias = dados.get("dias")
    if coberturas_carencia and dias:
        linhas.append(
            f"- carência: {_lista(nomes_de_coberturas(list(coberturas_carencia)))} só passam a valer "
            f"{dias} dias depois do início da vigência"
        )
    return "\n".join(linhas)


def _linha_plano(plano: PlanoResumo) -> str:
    return _t(
        "presenter.ask_plan.linha_plano",
        nome=plano.nome,
        franquia=_brl(plano.franquia),
        coberturas=_lista(_coberturas(plano.coberturas)),
    )


def _present(action: Present, state: LeadState) -> str:
    """Bloco único, uma informação por linha e sem linha em branco — leitura de WhatsApp."""
    quote = action.result.quote
    if action.result.outcome is not QuoteOutcome.OK or quote is None:
        raise ValueError("Present só renderiza cotação com outcome OK e quote preenchido.")

    linhas = [_t("presenter.present.titulo", plano_nome=quote.plano_nome, premio=_brl(quote.premio_mensal))]
    linhas += _corpo_da_cotacao(quote, _vigencia(action.result))
    if getattr(action, "data_assumida", False):
        linhas.append(_t("presenter.present.vigencia_assumida"))
    if action.cep_ausente:
        linhas.append(_t("presenter.present.aviso_cep_ausente"))
    linhas.append(_cta(state, quote))
    return "\n".join(linhas)


def _present_many(action: PresentMany, state: LeadState) -> str:
    """Vários carros numa mensagem: um bloco por carro cotado, e UM fechamento no fim."""
    cotados = [v for v in action.resultados if _quote_ok(v) is not None]
    if not cotados:
        raise ValueError("PresentMany só renderiza com pelo menos uma cotação OK.")

    primeiro = _quote_ok(cotados[0])
    assert primeiro is not None
    linhas = [
        _t("presenter.present_many.cabecalho", n=len(action.resultados), plano_nome=primeiro.plano_nome)
    ]
    for veiculo in action.resultados:
        linhas.append("")
        linhas += _bloco_do_carro(veiculo)

    if action.cep_ausente:
        linhas += ["", _t("presenter.present.aviso_cep_ausente")]
    linhas += ["", _cta(state, primeiro)]
    return "\n".join(linhas)


def _bloco_do_carro(veiculo: VeiculoColetado) -> list[str]:
    """Bloco de um carro: cotação, recusa com o motivo, ou 'te mando em seguida'."""
    carro = veiculo.rotulo()
    resultado = veiculo.quote_result
    quote = _quote_ok(veiculo)
    if quote is None:
        if resultado is not None and resultado.outcome is QuoteOutcome.RECUSA:
            motivo = _minuscula_inicial(resultado.motivo_recusa or "não cotamos esse perfil")
            return [_t("presenter.present_many.linha_recusa", carro=carro, motivo=motivo)]
        return [_t("presenter.present_many.linha_pendente", carro=carro)]
    assert resultado is not None
    titulo = _t("presenter.present_many.titulo_carro", carro=carro, premio=_brl(quote.premio_mensal))
    return [titulo, *_corpo_da_cotacao(quote, _vigencia(resultado))]


def _vigencia(resultado: QuoteResult) -> str:
    """Data em que a apólice começa a valer: a que FOI enviada no request, não uma suposição."""
    return _data_br(resultado.request.data_inicio)


def _quote_ok(veiculo: VeiculoColetado) -> Quote | None:
    resultado = veiculo.quote_result
    if resultado is None or resultado.outcome is not QuoteOutcome.OK:
        return None
    return resultado.quote


def _corpo_da_cotacao(quote: Quote, vigencia: str) -> list[str]:
    """Franquia, coberturas, carência e pro-rata — o mesmo corpo para 1 ou N carros.

    O preço não entra aqui: ele é a linha 1, junto do nome do plano (ou do carro).
    A linha de pro-rata só existe quando a API mandou pro-rata, e é a única que fala de
    vigência — é ela que explica por que o primeiro pagamento é diferente.
    """
    linhas = [
        _t("presenter.present.franquia", franquia=_brl(quote.franquia)),
        _t("presenter.present.coberturas", coberturas=_lista(_coberturas(quote.coberturas))),
    ]
    if quote.carencia_coberturas and quote.carencia_dias:
        linhas.append(
            _t(
                "presenter.present.carencia",
                coberturas_carencia=_lista(_coberturas(quote.carencia_coberturas)),
                dias=quote.carencia_dias,
            )
        )
    if quote.pro_rata is not None:
        linhas.append(
            _t(
                "presenter.present.pro_rata",
                valor=_brl(quote.pro_rata.valor_primeiro_pagamento),
                dias=quote.pro_rata.dias_cobrados,
                premio=_brl(quote.premio_mensal),
                vigencia=vigencia,
            )
        )
    return linhas


def _cta(state: LeadState, quote: Quote) -> str:
    """Fechamento. Quando o plano foi assumido pela policy, o texto convida a trocar."""
    if state.plano_assumido:
        return _t("presenter.present.cta_plano_assumido", plano_nome=quote.plano_nome)
    return _t("presenter.present.cta")


def _refuse(action: Refuse) -> str:
    """Duas frases: a explicação (com o motivo) e o agradecimento. Recusa não vira handoff."""
    explicacao = _t("presenter.refuse", motivo=_minuscula_inicial(action.motivo))
    return f"{explicacao}\n{_t('presenter.refuse.fechamento')}"


def _handoff(action: Handoff, state: LeadState) -> str:
    texto = _t(f"presenter.handoff.{action.reason.value}")
    if state.lead_nome:
        return f"{state.lead_nome.split()[0]}, {_minuscula_inicial(texto)}"
    return texto


# --------------------------------------------------------------------------- aviso ao consultor
# Rótulo curto de cada motivo. Não é texto para o lead (esse é `presenter.handoff.<motivo>`, slot):
# é o nome do motivo dentro do aviso interno, e o operador reescreve a frase inteira no slot
# `presenter.handoff.aviso_consultor` se quiser.
MOTIVOS_HANDOFF: dict[HandoffReason, str] = {
    HandoffReason.LEAD_ACEITOU: "lead aceitou a cotação",
    HandoffReason.LEAD_PEDIU_HUMANO: "lead pediu atendente",
    HandoffReason.COTACAO_INDISPONIVEL: "cotação indisponível",
    HandoffReason.ERRO_INTERNO: "erro interno",
    HandoffReason.FORA_DE_ESCOPO: "fora de escopo",
    HandoffReason.NEGOCIACAO: "negociação/desconto",
    HandoffReason.SEM_PROGRESSO: "sem progresso",
}


def aviso_consultor(state: LeadState, action: Handoff, telefone: str, link: str) -> str:
    """Mensagem que vai para o CONSULTOR (não para o lead) quando a conversa vira handoff.

    Aqui o valor PODE aparecer: quem lê é quem vai fechar a venda, e o número continua vindo do
    `Quote` da API, formatado pelo mesmo `_brl` da mensagem do lead — preço nasce num lugar só.
    """
    return _t(
        "presenter.handoff.aviso_consultor",
        motivo=MOTIVOS_HANDOFF.get(action.reason, action.reason.value.replace("_", " ")),
        nome=state.lead_nome or "—",
        telefone=telefone or "—",
        origem=state.origem or "—",
        dados=_dados_do_lead(state),
        cotacoes=_cotacoes_do_lead(state),
        link=link,
    )


def _dados_do_lead(state: LeadState) -> str:
    """Uma linha com o que foi coletado: `idade: 35 · carros: Onix 2022 · cep: 01310-100`."""
    partes = []
    if state.idade is not None:
        partes.append(f"idade: {state.idade}")
    carros = "; ".join(v.rotulo() for v in state.veiculos) or state.veiculo_texto
    if carros:
        partes.append(f"carros: {carros}")
    if state.cep:
        partes.append(f"cep: {_cep_formatado(state.cep)}")
    elif state.cep_ausente:
        partes.append("cep: não informado")
    if state.plano_id:
        plano = f"plano: {state.plano_id}"
        partes.append(f"{plano} (assumido)" if state.plano_assumido else plano)
    return " · ".join(partes) or "—"


def _cotacoes_do_lead(state: LeadState) -> str:
    """Uma linha por carro, com o preço que a API devolveu (ou o que impediu de cotar)."""
    if not state.veiculos and state.veiculo_texto is None and state.veiculo_ano is None:
        # Handoff no começo da conversa (o lead pediu humano de cara): não há carro para listar.
        return "nenhum carro informado ainda"
    veiculos = state.veiculos or [
        VeiculoColetado(texto=state.veiculo_texto, ano=state.veiculo_ano, quote_result=state.quote_result)
    ]
    linhas = []
    for veiculo in veiculos:
        resultado = veiculo.quote_result
        rotulo = veiculo.rotulo()
        if resultado is None:
            linhas.append(f"{rotulo} — sem cotação")
        elif resultado.outcome is QuoteOutcome.OK and resultado.quote is not None:
            quote = resultado.quote
            linhas.append(f"{rotulo} — {quote.plano_nome} {_brl(quote.premio_mensal)}/mês")
        elif resultado.outcome is QuoteOutcome.RECUSA:
            linhas.append(f"{rotulo} — recusado: {resultado.motivo_recusa or 'perfil fora dos critérios'}")
        else:
            linhas.append(f"{rotulo} — sem cotação ({resultado.outcome.value})")
    return "\n".join(linhas)


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


def _data_br(iso: str) -> str:
    """`2026-09-15` → `15/09/2026`. Fatiamento, não parsing: data inválida volta como veio."""
    partes = (iso or "").split("-")
    if len(partes) != 3:
        return iso
    ano, mes, dia = partes
    return f"{dia}/{mes}/{ano}"


def _minuscula_inicial(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        return texto
    frase = texto[0].lower() + texto[1:]
    return frase if frase.endswith((".", "!", "?")) else f"{frase}."


__all__ = [
    "CANAIS",
    "aviso_consultor",
    "canal_de",
    "nomes_de_coberturas",
    "render",
    "resumo_dos_planos",
    "vitrine",
]
