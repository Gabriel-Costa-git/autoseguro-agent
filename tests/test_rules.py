from datetime import date

from agent.models import QuoteRequest
from agent.rules import Rules

PLANOS = {
    "moeda": "BRL",
    "planos": [
        {"id": "essencial", "nome": "Essencial", "base_mensal": 119.90, "franquia": 4500,
         "coberturas": ["colisao", "roubo", "furto"]},
        {"id": "completo", "nome": "Completo", "base_mensal": 209.90, "franquia": 3000,
         "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"]},
        {"id": "premium", "nome": "Premium", "base_mensal": 339.90, "franquia": 1500,
         "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros", "carro_reserva", "assistencia_24h"]},
    ],
    "regras": {
        "faixa_etaria": [
            {"idade_min": 18, "idade_max": 24, "multiplicador": 1.60},
            {"idade_min": 25, "idade_max": 29, "multiplicador": 1.25},
            {"idade_min": 30, "idade_max": 59, "multiplicador": 1.00},
            {"idade_min": 60, "idade_max": 75, "multiplicador": 1.40},
            {"idade_min": 76, "idade_max": 200, "recusar": True, "motivo": "Idade acima do limite."},
        ],
        "idade_veiculo": [
            {"anos_min": 0, "anos_max": 5, "multiplicador": 1.00},
            {"anos_min": 6, "anos_max": 10, "multiplicador": 1.15},
            {"anos_min": 11, "anos_max": 20, "multiplicador": 1.45},
            {"anos_min": 21, "anos_max": 200, "recusar": True, "motivo": "Veículo muito antigo."},
        ],
        "regiao_cep": {"prefixos_alto_risco": ["07", "08", "21", "26", "59"], "multiplicador": 1.30},
        "carencia": {"coberturas_com_carencia": ["roubo", "furto"], "dias": 30},
    },
}

TODAY = date(2026, 9, 1)


def _rules() -> Rules:
    return Rules.from_planos(PLANOS, TODAY)


def _req(**overrides) -> QuoteRequest:
    base = {
        "plano_id": "essencial",
        "idade": 30,
        "veiculo_ano": 2020,
        "cep": "01310100",
        "data_inicio": "2026-09-02",
    }
    base.update(overrides)
    return QuoteRequest(**base)


def test_from_planos_deriva_limites_sem_hardcode():
    rules = _rules()
    assert rules.idade_min == 18
    assert rules.idade_max == 75
    assert rules.ano_min == 2006
    assert rules.ano_max == 2026


def test_from_planos_deriva_de_json_alterado_prova_que_nao_e_hardcode():
    planos_alterado = {
        "planos": PLANOS["planos"],
        "regras": {
            "faixa_etaria": [
                {"idade_min": 21, "idade_max": 65, "multiplicador": 1.0},
                {"idade_min": 66, "idade_max": 90, "recusar": True, "motivo": "x"},
            ],
            "idade_veiculo": [
                {"anos_min": 0, "anos_max": 12, "multiplicador": 1.0},
                {"anos_min": 13, "anos_max": 100, "recusar": True, "motivo": "x"},
            ],
            "regiao_cep": PLANOS["regras"]["regiao_cep"],
            "carencia": PLANOS["regras"]["carencia"],
        },
    }
    rules = Rules.from_planos(planos_alterado, TODAY)
    assert rules.idade_min == 21
    assert rules.idade_max == 65
    assert rules.ano_min == TODAY.year - 12
    assert rules.ano_max == TODAY.year


def test_validate_idade_limites_exatos():
    rules = _rules()
    assert rules.validate_idade(18) is None
    assert rules.validate_idade(75) is None
    assert rules.validate_idade(17) is not None
    assert rules.validate_idade(76) is not None
    assert rules.validate_idade(17).tipo == "fora_da_faixa"


def test_validate_veiculo_ano_limites_exatos():
    rules = _rules()
    assert rules.validate_veiculo_ano(2006) is None
    assert rules.validate_veiculo_ano(2026) is None
    v_antigo = rules.validate_veiculo_ano(2005)
    assert v_antigo is not None
    assert v_antigo.tipo == "fora_da_faixa"
    v_futuro = rules.validate_veiculo_ano(2027)
    assert v_futuro is not None
    assert v_futuro.tipo == "futuro"


def test_validate_data_inicio_passado_hoje_futuro():
    rules = _rules()
    assert rules.validate_data_inicio(date(2026, 8, 31)) is not None
    assert rules.validate_data_inicio(date(2026, 8, 31)).tipo == "passado"
    assert rules.validate_data_inicio(TODAY) is None
    assert rules.validate_data_inicio(date(2026, 9, 10)) is None


def test_normalize_cep_variantes():
    rules = _rules()
    assert rules.normalize_cep("01310-100") == "01310100"
    assert rules.normalize_cep("01310100") == "01310100"
    assert rules.normalize_cep("cep 01310 100") == "01310100"
    assert rules.normalize_cep("não sei meu cep") is None


def test_validate_request_agrega_violacoes():
    rules = _rules()
    req = _req(idade=10, veiculo_ano=1990, data_inicio="2020-01-01")
    violacoes = rules.validate_request(req)
    campos = {v.campo for v in violacoes}
    assert campos == {"idade", "veiculo_ano", "data_inicio"}


def test_validate_request_sem_violacoes():
    rules = _rules()
    assert rules.validate_request(_req()) == []


def test_planos_resumo_mantem_ordem_do_json():
    rules = _rules()
    resumo = rules.planos_resumo()
    assert [p.id for p in resumo] == ["essencial", "completo", "premium"]
    assert resumo[0].franquia == 4500
