from agent.pii import mask_obj, mask_text


def test_mask_cpf():
    assert mask_text("meu CPF é 123.456.789-01") == "meu CPF é ***.***.***-**"
    assert mask_text("cpf 12345678901 aqui") == "cpf ***.***.***-** aqui"


def test_mask_email():
    assert mask_text("me chama em joao.silva+lead@gmail.com") == "me chama em ***@gmail.com"


def test_mask_telefone():
    assert mask_text("whatsapp +55 11 91234-5678") == "whatsapp +55 ** *****-****"
    assert mask_text("liga no (11) 91234-5678") == "liga no +55 ** *****-****"


def test_mask_placa():
    assert mask_text("carro placa ABC1D23") == "carro placa ***-****"
    assert mask_text("placa antiga ABC1234") == "placa antiga ***-****"


def test_mask_cep_mantem_prefixo():
    assert mask_text("moro no 01310-100") == "moro no 01310-***"
    assert mask_text("cep 01310100") == "cep 01310-***"


def test_mask_texto_com_varios_padroes():
    texto = "sou joao@teste.com, cpf 123.456.789-01, cep 01310-100"
    resultado = mask_text(texto)
    assert "***@teste.com" in resultado
    assert "***.***.***-**" in resultado
    assert "01310-***" in resultado


def test_mask_obj_recursivo_em_dict_aninhado():
    obj = {
        "lead": {"email": "a@b.com", "idade": 30},
        "notas": ["cpf 123.456.789-01", "sem pii aqui"],
        "ano": 2019,
    }
    resultado = mask_obj(obj)
    assert resultado["lead"]["email"] == "***@b.com"
    assert resultado["lead"]["idade"] == 30
    assert resultado["notas"][0] == "cpf ***.***.***-**"
    assert resultado["notas"][1] == "sem pii aqui"
    assert resultado["ano"] == 2019


def test_mask_obj_falso_positivo_ano_e_valor():
    obj = {"ano": 2019, "premio": "R$ 209,90"}
    resultado = mask_obj(obj)
    assert resultado["ano"] == 2019
    assert resultado["premio"] == "R$ 209,90"


def test_mask_obj_tipos_nao_string_intactos():
    assert mask_obj(42) == 42
    assert mask_obj(True) is True
    assert mask_obj(None) is None
