import unittest

from memory_intent import MAX_PHRASE, parse


class RequestsToWriteSomethingDown(unittest.TestCase):
    def test_plain_requests_give_the_fact(self):
        cases = {
            "Lembre que eu moro em Recife e prefiro ser chamado de Rafa.": "eu moro em Recife e prefiro ser chamado de Rafa",
            "Anote na sua memória que eu gosto de café": "eu gosto de café",
            "anote na sua memoria que eu gosto de cafe": "eu gosto de cafe",
            "guarde na memória, que sou vegetariano": "sou vegetariano",
            "Não esqueça que meu aniversário é em maio!": "meu aniversário é em maio",
            "Lembre-se de que trabalho com contabilidade": "trabalho com contabilidade",
            "Pode anotar que prefiro respostas curtas": "prefiro respostas curtas",
            "Por favor, memorize que meu nome é Ana": "meu nome é Ana",
            "Você pode guardar isso na memória que eu uso Windows": "eu uso Windows",
        }
        for text, fact in cases.items():
            with self.subTest(text):
                self.assertEqual(parse(text), fact)

    def test_questions_other_requests_and_empty_facts_go_to_the_model(self):
        for text in ("Você lembra que eu moro em Recife?", "Anote o telefone dele", "Guarde o arquivo que baixei", "Lembre de ligar amanhã",
                     "Lembre que", "Lembre que   .", "Qual é a capital da França?", "Liste meus emails", "", None, 42):
            with self.subTest(text):
                self.assertIsNone(parse(text))

    def test_a_long_message_is_left_to_the_model(self):
        self.assertIsNone(parse("Lembre que " + "x " * MAX_PHRASE))


if __name__ == "__main__":
    unittest.main()
