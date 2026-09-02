"""Textos padrão do agente (fonte única). Gerado a partir do código em 02/09/2026 e depois
mantido à mão: cada slot é editável no Studio; a versão `default` de um slot é SEMPRE este texto.
Slots com placeholders (templates) são registrados aqui pelos módulos que os usam.
"""
from __future__ import annotations

SLOTS: dict[str, dict] = {
    'fallback.idade': {
        'label': 'Fallback anti-preço: idade', 'grupo': 'responder', 'placeholders': [],
        'default': 'Pra te cotar direitinho: quantos anos você tem?',
    },
    'fallback.veiculo': {
        'label': 'Fallback anti-preço: veiculo', 'grupo': 'responder', 'placeholders': [],
        'default': 'Qual o modelo e o ano de fabricação do carro?',
    },
    'fallback.cep': {
        'label': 'Fallback anti-preço: cep', 'grupo': 'responder', 'placeholders': [],
        'default': 'Qual o CEP de onde o carro dorme à noite?',
    },
    'fallback.plano': {
        'label': 'Fallback anti-preço: plano', 'grupo': 'responder', 'placeholders': [],
        'default': 'Qual dos planos você quer que eu cote?',
    },
    'fallback.data_inicio': {
        'label': 'Fallback anti-preço: data_inicio', 'grupo': 'responder', 'placeholders': [],
        'default': 'A partir de quando você quer o seguro valendo?',
    },
    'fallback.padrao': {
        'label': 'Fallback anti-preço: padrão', 'grupo': 'responder', 'placeholders': [],
        'default': 'Valor eu só passo depois que o sistema cotar, pra não te falar bobagem. Podemos seguir com os dados?',
    },
    'intent.saudacao': {
        'label': 'Exemplos do intent saudacao', 'grupo': 'extractor', 'placeholders': [],
        'default': '"oi", "bom dia", "vi o anúncio de vocês"',
    },
    'intent.fornecer_dados': {
        'label': 'Exemplos do intent fornecer_dados', 'grupo': 'extractor', 'placeholders': [],
        'default': '"tenho 35", "Onix 2019", "meu cep é 01310-100"',
    },
    'intent.escolher_plano': {
        'label': 'Exemplos do intent escolher_plano', 'grupo': 'extractor', 'placeholders': [],
        'default': '"quero o completo", "pode ser o do meio"',
    },
    'intent.confirmar': {
        'label': 'Exemplos do intent confirmar', 'grupo': 'extractor', 'placeholders': [],
        'default': '"sim", "isso", "correto", "pode ser"',
    },
    'intent.negar': {
        'label': 'Exemplos do intent negar', 'grupo': 'extractor', 'placeholders': [],
        'default': '"não", "tá errado", "não é esse"',
    },
    'intent.nao_sei': {
        'label': 'Exemplos do intent nao_sei', 'grupo': 'extractor', 'placeholders': [],
        'default': '"não sei o cep", "não lembro o ano"',
    },
    'intent.aceitar': {
        'label': 'Exemplos do intent aceitar', 'grupo': 'extractor', 'placeholders': [],
        'default': '"fechado", "pode emitir", "quero contratar", "pode passar pro consultor fechar" (depois da cotação, concordar em seguir é ACEITAR)',
    },
    'intent.recusar': {
        'label': 'Exemplos do intent recusar', 'grupo': 'extractor', 'placeholders': [],
        'default': '"não quero mais", "deixa pra lá"',
    },
    'intent.pedir_humano': {
        'label': 'Exemplos do intent pedir_humano', 'grupo': 'extractor', 'placeholders': [],
        'default': '"quero falar com um atendente", "me passa pra uma pessoa" (só quando ele quer um humano EM VEZ do bot, não para fechar a cotação)',
    },
    'intent.objecao_preco': {
        'label': 'Exemplos do intent objecao_preco', 'grupo': 'extractor', 'placeholders': [],
        'default': '"tá caro", "vi mais barato", "achei salgado"',
    },
    'intent.pedir_desconto': {
        'label': 'Exemplos do intent pedir_desconto', 'grupo': 'extractor', 'placeholders': [],
        'default': '"tem desconto?", "consegue baixar?", "faz por menos?"',
    },
    'intent.fora_de_escopo': {
        'label': 'Exemplos do intent fora_de_escopo', 'grupo': 'extractor', 'placeholders': [],
        'default': '"bati o carro", "quero ver minha apólice", "seguro de vida"',
    },
    'intent.outro': {
        'label': 'Exemplos do intent outro', 'grupo': 'extractor', 'placeholders': [],
        'default': 'qualquer coisa que não se encaixe nas anteriores',
    },
    'diretiva.idade': {
        'label': 'Diretiva ao Responder: idade', 'grupo': 'responder', 'placeholders': [],
        'default': 'pergunte a idade do condutor principal',
    },
    'diretiva.veiculo': {
        'label': 'Diretiva ao Responder: veiculo', 'grupo': 'responder', 'placeholders': [],
        'default': 'pergunte o modelo e o ano de fabricação do carro',
    },
    'diretiva.cep': {
        'label': 'Diretiva ao Responder: cep', 'grupo': 'responder', 'placeholders': [],
        'default': 'pergunte o CEP de onde o carro dorme à noite',
    },
    'diretiva.plano': {
        'label': 'Diretiva ao Responder: plano', 'grupo': 'responder', 'placeholders': [],
        'default': 'pergunte qual plano ele quer cotar',
    },
    'diretiva.data_inicio': {
        'label': 'Diretiva ao Responder: data_inicio', 'grupo': 'responder', 'placeholders': [],
        'default': 'pergunte a partir de quando ele quer o seguro valendo',
    },
    'policy.txt_midia': {
        'label': 'Policy: TXT_MIDIA', 'grupo': 'policy', 'placeholders': [],
        'default': 'Não consigo ouvir áudio/abrir arquivos por aqui. Pode me escrever?',
    },
    'policy.txt_despedida': {
        'label': 'Policy: TXT_DESPEDIDA', 'grupo': 'policy', 'placeholders': [],
        'default': 'Tudo bem, sem problema! Se mudar de ideia é só me chamar por aqui. Obrigado pelo contato.',
    },
    'policy.txt_terminal_handoff': {
        'label': 'Policy: TXT_TERMINAL_HANDOFF', 'grupo': 'policy', 'placeholders': [],
        'default': 'Um consultor já está com o seu caso e responde por aqui mesmo. Pode deixar a mensagem que ele vê.',
    },
    'policy.txt_terminal_encerrado': {
        'label': 'Policy: TXT_TERMINAL_ENCERRADO', 'grupo': 'policy', 'placeholders': [],
        'default': 'Esse atendimento já foi encerrado por aqui. Se quiser retomar, é só chamar que um consultor te ajuda.',
    },
    'policy.txt_aguarde': {
        'label': 'Policy: TXT_AGUARDE', 'grupo': 'policy', 'placeholders': [],
        'default': 'Só um instante, estou puxando a cotação certinho pra você.',
    },
    'policy.txt_instabilidade': {
        'label': 'Policy: TXT_INSTABILIDADE', 'grupo': 'policy', 'placeholders': [],
        'default': 'Tive uma instabilidade aqui do meu lado e não consegui ler sua mensagem. Pode repetir?',
    },
    'policy.txt_data_passada': {
        'label': 'Policy: TXT_DATA_PASSADA', 'grupo': 'policy', 'placeholders': [],
        'default': 'Essa data de início já passou, então não consigo usar. A vigência começa a partir de hoje — se quiser começar depois, me diga outra data.',
    },
    'policy.diretiva_objecao': {
        'label': 'Policy: DIRETIVA_OBJECAO', 'grupo': 'policy', 'placeholders': [],
        'default': 'lead achou caro; ofereça ver outro plano (mais franquia = parcela menor); NÃO dê desconto nem cite valores novos',
    },
    'policy.diretiva_pos_cotacao': {
        'label': 'Policy: DIRETIVA_POS_COTACAO', 'grupo': 'policy', 'placeholders': [],
        'default': 'lead respondeu depois da cotação sem aceitar nem recusar; retome a dúvida dele e pergunte se quer fechar ou ver outro plano; NÃO cite valores novos',
    },
    'policy.diretiva_mesmo_plano': {
        'label': 'Policy: DIRETIVA_MESMO_PLANO', 'grupo': 'policy', 'placeholders': [],
        'default': 'lead repetiu o plano que já está cotado; confirme se quer fechar esse mesmo ou ver outro; NÃO cite valores novos',
    },
    'policy.motivo_ano_modelo': {
        'label': 'Policy: MOTIVO_ANO_MODELO', 'grupo': 'policy', 'placeholders': [],
        'default': 'ano futuro parece ano-modelo; confirmar ano de fabricação',
    },
    'presenter.handoff.lead_aceitou': {
        'label': 'Handoff: lead_aceitou', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Perfeito! Um consultor vai finalizar com você — já passei seus dados e a cotação pra ele. É só aguardar aqui mesmo que ele te chama.',
    },
    'presenter.handoff.lead_pediu_humano': {
        'label': 'Handoff: lead_pediu_humano', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Claro! Já chamei um consultor e passei o que a gente conversou. Ele assume por aqui em instantes.',
    },
    'presenter.handoff.cotacao_indisponivel': {
        'label': 'Handoff: cotacao_indisponivel', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Nosso sistema de cotação está instável agora e eu não vou te passar um valor chutado. Guardei seus dados e um consultor te retorna com o valor certinho.',
    },
    'presenter.handoff.erro_interno': {
        'label': 'Handoff: erro_interno', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Deu um problema técnico aqui na hora de cotar. Pra não te deixar esperando, passei seu caso pra um consultor — ele te retorna.',
    },
    'presenter.handoff.fora_de_escopo': {
        'label': 'Handoff: fora_de_escopo', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Isso aqui eu não consigo resolver: cuido só de cotação de seguro auto novo. Já estou passando pra um consultor que te ajuda com isso.',
    },
    'presenter.handoff.negociacao': {
        'label': 'Handoff: negociacao', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Condição especial quem pode avaliar é um consultor. Já passei sua cotação pra ele, ele fala com você por aqui.',
    },
    'presenter.handoff.sem_progresso': {
        'label': 'Handoff: sem_progresso', 'grupo': 'presenter', 'placeholders': [],
        'default': 'Acho que por aqui não estou conseguindo te ajudar direito. Vou chamar um consultor pra falar com você.',
    },
    'presenter.cobertura.colisao': {
        'label': 'Cobertura legível: colisao', 'grupo': 'presenter', 'placeholders': [],
        'default': 'colisão',
    },
    'presenter.cobertura.roubo': {
        'label': 'Cobertura legível: roubo', 'grupo': 'presenter', 'placeholders': [],
        'default': 'roubo',
    },
    'presenter.cobertura.furto': {
        'label': 'Cobertura legível: furto', 'grupo': 'presenter', 'placeholders': [],
        'default': 'furto',
    },
    'presenter.cobertura.terceiros': {
        'label': 'Cobertura legível: terceiros', 'grupo': 'presenter', 'placeholders': [],
        'default': 'danos a terceiros',
    },
    'presenter.cobertura.vidros': {
        'label': 'Cobertura legível: vidros', 'grupo': 'presenter', 'placeholders': [],
        'default': 'vidros',
    },
    'presenter.cobertura.carro_reserva': {
        'label': 'Cobertura legível: carro_reserva', 'grupo': 'presenter', 'placeholders': [],
        'default': 'carro reserva',
    },
    'presenter.cobertura.assistencia_24h': {
        'label': 'Cobertura legível: assistencia_24h', 'grupo': 'presenter', 'placeholders': [],
        'default': 'assistência 24h',
    },
    'conversation.texto_erro': {
        'label': 'Conversation: erro inesperado', 'grupo': 'conversation', 'placeholders': [],
        'default': 'Tive um problema aqui do meu lado. Um consultor vai te chamar pra continuar com você.',
    },
    'conversation.texto_lento': {
        'label': "Conversation: 'só um instante'", 'grupo': 'conversation', 'placeholders': [],
        'default': 'Só um instante, estou consultando o sistema...',
    },
}
