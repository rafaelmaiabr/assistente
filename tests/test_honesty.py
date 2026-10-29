import unittest

import honesty
from team.connectors.http_routes import BUILTIN


class CapabilitiesTextTests(unittest.TestCase):
    def test_a_session_with_no_tools_claims_nothing(self):
        text = honesty.capabilities_text(set())
        self.assertIn("apenas conversar", text)
        self.assertNotIn("ler a caixa", text)
        self.assertIn("NÃO consegue", text)

    def test_each_registered_tool_adds_exactly_its_own_capability(self):
        mail = honesty.capabilities_text({"listar_emails", "buscar_emails", "ler_email", "preparar_resposta"})
        self.assertIn("ler a caixa de email", mail)
        self.assertIn("rascunho", mail)
        self.assertNotIn("cartão para o usuário conectar", mail)
        self.assertNotIn("rotas de internet", mail)
        card = honesty.capabilities_text({"solicitar_integracao"})
        self.assertIn("cartão para o usuário conectar", card)
        self.assertNotIn("ler a caixa", card)
        self.assertIn("rotas de internet", honesty.capabilities_text({"consultar_rota"}))

    def test_the_limits_are_always_stated(self):
        for names in (set(), {"listar_emails"}, {"solicitar_integracao", "consultar_rota"}):
            text = honesty.capabilities_text(names)
            for limit in ("internet em geral", "mover, apagar, arquivar", "sem a aprovação", "botões, cartões ou links"):
                self.assertIn(limit, text)


class ParseTests(unittest.TestCase):
    def test_known_out_of_reach_requests_are_recognised(self):
        cases = {
            "Consegue acessar a internet?": "internet", "Você tem acesso à internet?": "internet", "acessa a internet": "internet",
            "Mova os spam": "emails", "mova os spam para a lixeira": "emails", "Apague meus emails": "emails",
            "Marque como lido": "emails", "marcar os emails como spam": "emails", "Pode arquivar as mensagens antigas?": "emails",
            "Exclua os emails de ontem": "emails", "limpe a caixa de entrada": "emails",
            "Envie a resposta sem aprovação": "enviar", "mande o email sem eu revisar": None,
            "Consegue acessar https://brasilapi.com.br/registrobr/v1/h1internet.com.br e ler o resultado?": "endereco",
            "abra https://exemplo.com.br/pagina": "endereco",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                self.assertEqual(honesty.parse(text), key)

    def test_ordinary_requests_and_questions_about_how_go_to_the_model(self):
        for text in ("Liste meus emails", "Qual a capital da França?", "Como faço para apagar um email no Gmail?",
                     "Marque uma reunião para amanhã", "Apague a última frase que você disse", "Resuma o email do Bruno",
                     "A internet caiu aqui em casa", "Configure a caixa de email", "Veja https://exemplo.com.br",
                     "", "x" * 500, None, 7):
            with self.subTest(text=text):
                self.assertIsNone(honesty.parse(text))

    def test_the_answers_say_nothing_was_done_and_what_is_possible(self):
        self.assertIn("nada foi alterado", honesty.answer("emails"))
        self.assertIn("liste meus emails", honesty.answer("emails"))
        self.assertIn("Nada foi enviado", honesty.answer("enviar"))
        self.assertIn("Aprovar e enviar", honesty.answer("enviar"))
        self.assertIn("nada foi acessado", honesty.answer("endereco"))

    def test_the_internet_answer_names_only_the_routes_that_are_on(self):
        self.assertIn("Nenhuma rota", honesty.answer("internet"))
        route = BUILTIN["registro-dominio"]
        text = honesty.answer("internet", [route])
        self.assertIn("registro-dominio", text)
        self.assertIn("Não tenho acesso livre", text)
        self.assertIn("registro-dominio", honesty.answer("endereco", [route]))


class TurnCheckTests(unittest.TestCase):
    def test_a_button_the_server_did_not_show_is_flagged(self):
        for text in ("Clique em [Conectar sistema de consulta de domínio] para continuar.", "Use o botão abaixo para conectar.",
                     "Clique no cartão para decidir.", "Aperte [Abrir formulário]", "Escolha: [Configurar]"):
            with self.subTest(text=text):
                self.assertIn("nenhum foi mostrado", honesty.turn_warning(text, tools=1, shown=False))

    def test_a_card_the_server_did_show_is_not_flagged(self):
        self.assertIsNone(honesty.turn_warning("Use os botões do cartão abaixo.", tools=1, shown=True))

    def test_an_announced_action_with_no_tool_is_flagged_and_with_a_tool_is_not(self):
        for text in ("Estou listando os emails agora.", "Vou consultar a sua caixa.", "Um momento, estou verificando.", "Vou acessar o site."):
            with self.subTest(text=text):
                self.assertIn("nada foi feito", honesty.turn_warning(text, tools=0, shown=False))
                self.assertIsNone(honesty.turn_warning(text, tools=1, shown=False))

    def test_plain_answers_are_not_flagged(self):
        for text in ("Paris é a capital da França.", "Você tem 3 emails: ...", "Não consigo apagar emails.", "[1] item da lista", ""):
            with self.subTest(text=text):
                self.assertIsNone(honesty.turn_warning(text, tools=0, shown=False))

    def test_both_notes_come_together(self):
        notice = honesty.turn_warning("Estou verificando. Clique em [Conectar] abaixo.", tools=0, shown=False)
        self.assertIn("nenhum foi mostrado", notice)
        self.assertIn("nada foi feito", notice)


if __name__ == "__main__":
    unittest.main()
