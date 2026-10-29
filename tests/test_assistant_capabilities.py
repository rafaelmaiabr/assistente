import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team.connectors.http_routes import RouteStore

LIVEKIT = importlib.util.find_spec("livekit") is not None
CONFIG = {"mcp_servers": [], "model": "x", "ollama_url": "http://x"}


@unittest.skipUnless(LIVEKIT, "livekit is not installed here")
class PromptCapabilitiesTests(unittest.TestCase):
    """The prompt's list of what the assistant can do comes from the tools registered in the session (#121)."""

    def setUp(self):
        import assistant_core
        self.core = assistant_core
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RouteStore(Path(tmp.name) / "http-routes.json")

    def mail(self, configured):
        mail = mock.Mock()
        mail.configured.return_value, mail.smtp_config.return_value, mail.prompt_note = configured, None, ""
        return mail

    def parts(self, configured=False, on_integration=None):
        mail = self.mail(configured)
        return self.core.agent_parts(CONFIG, mail, mock.Mock(), on_integration=on_integration, routes=self.store)

    def names(self, tools):
        return {tool.info.name for tool in tools if getattr(tool, "info", None)}

    def tail(self, instructions):
        return instructions[instructions.index("Capacidades reais nesta sessão"):]

    def test_without_integrations_or_routes_the_prompt_lists_email_and_no_card_or_route(self):
        instructions, tools = self.parts()
        listed = self.tail(instructions)
        self.assertIn("ler a caixa de email", listed)
        self.assertIn("formulário seguro", listed)
        self.assertNotIn("cartão para o usuário conectar", listed)
        self.assertNotIn("rotas de internet", listed)
        self.assertNotIn("solicitar_integracao", self.names(tools))
        self.assertIn("NÃO consegue", listed)

    def test_with_the_mailbox_set_up_the_same_list_holds_and_the_limits_stay(self):
        instructions, tools = self.parts(configured=True)
        listed = self.tail(instructions)
        self.assertIn("rascunho", listed)
        self.assertIn("mover, apagar, arquivar", listed)
        self.assertTrue({"listar_emails", "preparar_resposta"} <= self.names(tools))

    def test_with_the_integration_tool_the_card_is_listed_and_without_it_not(self):
        with_card, tools = self.parts(on_integration=lambda vendor, capability: {})
        self.assertIn("solicitar_integracao", self.names(tools))
        self.assertIn("cartão para o usuário conectar", self.tail(with_card))
        without, _ = self.parts()
        self.assertNotIn("cartão para o usuário conectar", self.tail(without))

    def test_a_route_turned_on_adds_the_route_capability(self):
        self.store.enable("registro-dominio")
        instructions, tools = self.parts()
        self.assertIn("consultar_rota", self.names(tools))
        self.assertIn("rotas de internet", self.tail(instructions))

    def test_a_company_mailbox_is_named_in_the_prompt_as_read_only_and_never_offers_a_reply(self):
        from team.chat_mail import BlockedMail, CompanyMail, ReadOnlyOutbox
        source = {"company": "Empresa A", "label": "Caixa da Ana", "folder": "INBOX", "company_id": "c", "connection_id": "k"}
        mail = CompanyMail(mock.Mock(), source, mock.Mock())
        instructions, tools = self.core.agent_parts(CONFIG, mail, ReadOnlyOutbox(), routes=self.store)
        self.assertIn("a da empresa Empresa A: Caixa da Ana", instructions)
        self.assertIn("somente leitura", instructions)
        self.assertNotIn("preparar_resposta. Isso so cria", instructions)
        self.assertIn(self.core.READ_ONLY, instructions)  # sending is not configured: the model is told to say it only reads
        blocked, _ = self.core.agent_parts(CONFIG, BlockedMail(), ReadOnlyOutbox(), routes=self.store)
        self.assertIn("nenhuma foi escolhida", blocked)

if __name__ == "__main__":
    unittest.main()
