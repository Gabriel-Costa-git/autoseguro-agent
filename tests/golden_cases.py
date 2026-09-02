"""Casos que geram os goldens de texto (`tests/golden/*.txt`).

Gerados a partir do código ENTREGUE (02/09/2026) antes de os textos virarem editáveis no
Studio. O teste `test_golden_textos.py` prova que, com a configuração padrão, cada saída
continua byte-idêntica. Regenerar só com decisão explícita (mudança de comportamento).
"""
from __future__ import annotations

from datetime import date

from agent import brain, policy, presenter
from agent.models import (
    AskPlan,
    CepInfo,
    ConfirmCep,
    Handoff,
    HandoffReason,
    LeadState,
    PlanoResumo,
    Present,
    ProRata,
    Quote,
    QuoteAttempt,
    QuoteOutcome,
    QuoteRequest,
    QuoteResult,
    Refuse,
    SendText,
    Stage,
)

HOJE = date(2026, 9, 1)


def casos() -> dict[str, str]:
    q = Quote(
        plano_id="completo", plano_nome="Completo", premio_mensal=1025.14, franquia=3000,
        coberturas=["colisao", "roubo", "furto", "terceiros", "vidros", "carro_reserva", "assistencia_24h"],
        multiplicadores={"faixa_etaria": 1.6, "idade_veiculo": 1.45, "regiao": 1.3},
        carencia_coberturas=["roubo", "furto"], carencia_dias=30, carencia_observacao="obs", moeda="BRL",
        pro_rata=ProRata(dias_no_mes=30, dias_cobrados=16, valor_primeiro_pagamento=546.74),
    )
    req = QuoteRequest(plano_id="completo", idade=19, veiculo_ano=2008, cep="08000000", data_inicio="2026-09-15")
    res = QuoteResult(
        quote_id="qabc12345", outcome=QuoteOutcome.OK, request=req, quote=q,
        attempts=[QuoteAttempt(attempt=1, status="ok", http_status=200, latency_ms=20)], total_ms=25,
    )
    q2 = q.model_copy(update={"pro_rata": None, "coberturas": ["colisao", "roubo", "furto"], "carencia_coberturas": []})
    res2 = res.model_copy(update={"quote": q2})
    st = LeadState(
        conversation_id="g", lead_nome="Ursula Souza", idade=19, veiculo_texto="Onix 2008", veiculo_ano=2008,
        cep="08000000", cep_info=CepInfo(cep="08000000", existe=True, cidade="Guarulhos", uf="SP"),
        cep_confirmado=True, plano_id="completo", stage=Stage.APRESENTADO, quote_result=res, ultima_pergunta="plano",
    )
    st_sem_nome = st.model_copy(update={"lead_nome": None})
    planos = [
        PlanoResumo(id="essencial", nome="Essencial", franquia=4500, coberturas=["colisao", "roubo", "furto"]),
        PlanoResumo(id="completo", nome="Completo", franquia=3000, coberturas=["colisao", "roubo", "furto", "terceiros", "vidros"]),
        PlanoResumo(id="premium", nome="Premium", franquia=1500,
                    coberturas=["colisao", "roubo", "furto", "terceiros", "vidros", "carro_reserva", "assistencia_24h"]),
    ]
    acoes = {
        "presenter_confirm_cep": (ConfirmCep(cep="01310100", cidade="São Paulo", uf="SP"), st),
        "presenter_ask_plan": (AskPlan(planos=planos), st),
        "presenter_present_completo": (Present(result=res, cep_ausente=False), st),
        "presenter_present_sem_prorata_cep_ausente": (Present(result=res2, cep_ausente=True), st),
        "presenter_refuse": (Refuse(motivo="Só conseguimos cotar para condutores de 18 a 75 anos."), st),
        "presenter_send_text": (SendText(text="Só um instante."), st),
    }
    for r in HandoffReason:
        acoes[f"presenter_handoff_{r.value}_com_nome"] = (Handoff(reason=r, payload={}), st)
        acoes[f"presenter_handoff_{r.value}_sem_nome"] = (Handoff(reason=r, payload={}), st_sem_nome)
    out = {nome: presenter.render(acao, s) for nome, (acao, s) in acoes.items()}

    st_ext = LeadState(
        conversation_id="g", idade=35, veiculo_texto="Onix 2022", veiculo_ano=2022, cep="01310100",
        cep_info=CepInfo(cep="01310100", existe=True, cidade="São Paulo", uf="SP"),
        stage=Stage.ESCOLHA_PLANO, ultima_pergunta="plano",
    )
    # `ferramentas=[]` = a configuração ENTREGUE (nenhuma tool no painel). Sem isso o golden
    # dependeria do `config/custom_tools.json` da máquina: uma tool ligada acrescenta o intent
    # `consulta` ao prompt DE PROPÓSITO, e o gate ficaria vermelho por causa de configuração.
    out["brain_extractor_instructions"] = brain.build_extraction_instructions(st_ext, HOJE, ferramentas=[])
    out["brain_extractor_instructions_vazio"] = brain.build_extraction_instructions(
        LeadState(conversation_id="g"), HOJE, ferramentas=[]
    )
    out["brain_responder_instructions"] = brain.build_responder_instructions(st, "peça o CEP")
    out["brain_responder_instructions_apresentado"] = brain.build_responder_instructions(st, policy.DIRETIVA_OBJECAO)
    out["brain_directive_for_field"] = "\n".join(
        brain.directive_for_field(c, m) for c in ["idade", "veiculo", "cep", "plano", "data_inicio"] for m in [None, "motivo x"]
    )
    out["brain_resumo_state"] = brain.resumo_state(st) + "\n" + brain.resumo_state(LeadState(conversation_id="g"))
    return out
