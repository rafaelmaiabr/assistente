import asyncio
import email
import email.policy
import json
import shutil
import ssl
import subprocess
import unittest
from pathlib import Path

from team.access import NotFound, PermissionDenied
from team.attachments import MAX_FILE_BYTES, AttachmentStore
from team.outbound import OutboundError, OutboundGateway
from team.retention import RetentionService
from team.smtp_transport import (
    FakeEmailTransport, RoutingEmailTransport, SmtpAccountRegistry, SmtpAccounts, SmtpConfig, SmtpTransport)
from tests.smtp_fake import FakeSmtp
from tests.test_team_outbound import CLIENT, OutboundCase

PDF = b"%PDF-1.4\n%assistente\n1 0 obj<<>>endobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class SmtpCase(OutboundCase):
    def setUp(self):
        super().setUp()
        self.smtp = FakeSmtp()
        self.addCleanup(self.smtp.stop)
        self.registry = SmtpAccountRegistry(Path(self.temp.name) / "smtp-accounts.json", self.access.operator_id)
        self.accounts = SmtpAccounts(self.connections, self.registry)
        self.store = AttachmentStore(Path(self.temp.name))
        self.fake = FakeEmailTransport()
        self.router = RoutingEmailTransport(self.registry, SmtpTransport(self.registry, self.store), self.fake)
        self.outbound = OutboundGateway(self.repo, self.access, self.cases, self.approvals, self.connections, {"email": self.router},
                                        handoffs=self.handoffs, clock=self.clock, send_timeout=3, attachments=self.store,
                                        message_id_domain=self.registry.message_id_domain)
        self.real = self.connections.create_connection(self.ca, label="SMTP real", provider="smtp", connection_kind="smtp")
        self.connections.set_credential(self.ca, self.real["id"], self.smtp.password)
        self.configure()

    def configure(self, **overrides):
        options = dict(host="127.0.0.1", port=self.smtp.port, username=self.smtp.user, from_address=self.smtp.user, security="plain",
                       from_name="Atendimento", timeout=3)
        options.update(overrides)
        self.accounts.configure(self.op, self.ca, self.real["id"], **options)

    def real_message(self, **kwargs):
        return self.approved(account_id=self.real["id"], **kwargs)

    def delivered(self):
        return self.smtp.delivered()

    @staticmethod
    def parsed(session):
        return email.message_from_bytes(session["data"], policy=email.policy.default)


class SendTests(SmtpCase):
    def test_an_approved_message_goes_out_with_our_message_id_cc_and_hidden_copies(self):
        self.team.create(self.ca, "access_grant", principal_id=self.suporte["id"], resource_kind="case", resource_id=None,
                         actions=["read", "write"], granted_by=self.op.principal_id, expires_at=None)
        for address in ("copia@exemplo.com", "oculto@exemplo.com"):
            contact = self.team.create(self.ca, "contact", display_name=address, status="ativo")
            observed = self.team.observe_address(self.ca, "email", address)
            self.team.link_address(self.ca, observed["id"], contact["id"], observed["revision"])
        message = self.real_message(cc=["copia@exemplo.com"], bcc=["oculto@exemplo.com"], subject="Seu chamado\r\nBcc: invasor@x.com")
        self.assertEqual(message["subject"], "Seu chamado Bcc: invasor@x.com")  # no header injection through the subject
        sent = self.send(message)
        self.assertEqual((sent["state"], sent["simulated"] if "simulated" in sent else None), ("enviada", None))
        self.assertEqual(sent["provider_id"], message["message_id"])
        self.assertTrue(sent["provider_result"].startswith("250"))
        session = self.delivered()[0]
        self.assertEqual(sorted(session["rcpts"]), sorted([CLIENT, "copia@exemplo.com", "oculto@exemplo.com"]))
        mail = self.parsed(session)
        self.assertEqual((mail["Message-ID"], mail["To"], mail["Cc"], mail["X-Assistente-Origem"]), (message["message_id"], CLIENT, "copia@exemplo.com", "resposta"))
        self.assertIsNone(mail["Bcc"])
        self.assertNotIn("oculto@exemplo.com", session["data"].decode())  # a hidden copy stays hidden in the headers and body
        self.assertEqual((session["from"], session["user"]), (self.smtp.user, self.smtp.user))
        self.assertTrue(message["message_id"].endswith("@empresa.invalid>"))
        entry = [e for e in self.cases.timeline(self.ca, self.case["id"]) if e["tipo"] == "entrada" and e["item"]["direction"] == "saida"][0]["item"]
        self.assertEqual(entry["body"], message["body"])  # real send: no simulation marker

    def test_replies_carry_threading_headers_from_the_incoming_email(self):
        now = "2026-06-01T12:00:00Z"
        with self.repo.transaction(self.a["id"]) as session:
            session.conn.execute("PRAGMA foreign_keys = OFF")
            for uid, mid, refs in ((1, "<c1@cli>", []), (2, "<c2@cli>", ["<c1@cli>"])):
                doc = {"kind": "email_message", "schema_version": 1, "id": f"eml_{uid:032x}", "tenant_id": self.a["id"], "revision": 1,
                       "created_at": now, "updated_at": now, "connection_id": self.account["id"], "folder": "INBOX", "uidvalidity": 11, "uid": uid,
                       "message_id": mid, "in_reply_to": refs[-1] if refs else None, "references": refs, "sender": CLIENT, "sender_name": None,
                       "recipients": ["atendimento@empresa.invalid"], "classification": "nova_demanda" if uid == 1 else "resposta",
                       "case_id": self.case["id"], "identity": "nao_verificada", "subject_hash": "0" * 64, "body_hash": "0" * 64, "reason": None}
                session.conn.execute(
                    "INSERT INTO email_messages (id, tenant_id, revision, created_at, updated_at, connection_id, folder, uidvalidity, uid, message_id, "
                    "case_id, classification, doc) VALUES (?, ?, 1, ?, ?, ?, 'INBOX', 11, ?, ?, ?, ?, ?)",
                    (doc["id"], self.a["id"], now, now, self.account["id"], uid, mid, self.case["id"], doc["classification"], json.dumps(doc)))
        message = self.real_message()
        self.assertEqual((message["in_reply_to"], message["references"]), ("<c2@cli>", ["<c1@cli>", "<c2@cli>"]))
        self.send(message)
        mail = self.parsed(self.delivered()[0])
        self.assertEqual((mail["In-Reply-To"], mail["References"]), ("<c2@cli>", "<c1@cli> <c2@cli>"))
        fresh = self.working_case()
        self.assertIsNone(self.draft(case=fresh, account_id=self.real["id"], dedup_key="outro")["in_reply_to"])  # no email in that case: no reply headers

    def test_attachments_are_validated_stored_hashed_and_sent_intact(self):
        message = self.real_message(attachments=[{"filename": "../../Fatura Junho.pdf", "data": PDF}, {"filename": "logo.png", "data": PNG}])
        self.assertEqual([(a["filename"], a["content_type"], a["size"]) for a in message["attachments"]],
                         [("Fatura_Junho.pdf", "application/pdf", len(PDF)), ("logo.png", "image/png", len(PNG))])
        self.send(message)
        parts = {p.get_filename(): p.get_payload(decode=True) for p in self.parsed(self.delivered()[0]).iter_attachments()}
        self.assertEqual(parts, {"Fatura_Junho.pdf": PDF, "logo.png": PNG})
        for bad in ([{"filename": "virus.exe", "data": b"MZ"}], [{"filename": "a.pdf", "data": b"nao e pdf"}], [{"filename": "b.pdf", "data": b""}],
                    [{"filename": "c.pdf", "data": b"%PDF-" + b"0" * MAX_FILE_BYTES}], [{"filename": f"{i}.txt", "data": b"x"} for i in range(4)],
                    [{"filename": "d.txt", "data": b"\xff\xfe\x00"}], [{"filename": "e.zip", "data": b"PK"}], [{"filename": "...", "data": b"x"}]):
            with self.assertRaises(OutboundError, msg=str(bad)[:60]):
                self.draft(account_id=self.real["id"], attachments=bad, dedup_key=f"anexo-{id(bad)}")
        plain = OutboundGateway(self.repo, self.access, self.cases, self.approvals, self.connections, {"email": self.router}, clock=self.clock)
        with self.assertRaisesRegex(OutboundError, "Anexos nao estao disponiveis"):
            plain.create_draft(self.agent_ctx(), self.case["id"], account_id=self.real["id"], to=[CLIENT], body="x", attachments=[{"filename": "a.txt", "data": b"x"}])

    def test_the_approved_payload_covers_cc_bcc_and_attachments(self):
        message = self.real_message(attachments=[{"filename": "a.txt", "data": b"original"}])
        approval = self.approvals.get(self.ca, message["approval_id"])
        shown = approval["normalized_action"]["params"]
        self.assertEqual((shown["attachments"], shown["bcc"], approval["normalized_action"]["recipients"]), (["a.txt"], [], [CLIENT]))
        # someone adds a hidden recipient after approval: the hash no longer matches and nothing is sent
        with self.repo.transaction(self.a["id"]) as session:
            session.save({**message, "bcc": ["intruso@exemplo.com"], "revision": message["revision"] + 1})
        blocked = self.send(message)
        self.assertEqual((blocked["state"], blocked["last_error"]), ("bloqueada", "rascunho_alterado"))
        self.assertEqual(self.smtp.sessions, [])  # not even a connection

    def test_an_attachment_changed_after_approval_is_never_sent(self):
        message = self.real_message(attachments=[{"filename": "a.txt", "data": b"original"}])
        stored = self.store.root / self.a["id"] / self.case["id"] / message["attachments"][0]["sha256"]
        stored.write_bytes(b"trocado")
        result = self.send(message)
        self.assertEqual((result["state"], result["last_error"]), ("falhou", "recusada_pelo_provedor:anexo_invalido"))
        self.assertEqual(self.delivered(), [])

    def test_cc_and_bcc_follow_the_recipient_policy_and_limits(self):
        with self.assertRaisesRegex(OutboundError, "mais de uma vez"):
            self.draft(account_id=self.real["id"], cc=[CLIENT], dedup_key="a")
        with self.assertRaisesRegex(OutboundError, "No maximo"):
            self.draft(account_id=self.real["id"], cc=[f"c{i}@exemplo.com" for i in range(5)], dedup_key="b")
        outsider = self.draft(account_id=self.real["id"], bcc=["estranho@fora.com"], dedup_key="c")
        with self.assertRaisesRegex(OutboundError, "fora da politica"):
            self.outbound.submit(self.agent_ctx(), outsider["id"])  # hidden recipients are checked like any other
        with self.assertRaises(OutboundError):
            self.draft(account_id=self.real["id"], to=["sem-arroba"], dedup_key="d")


class OutcomeTests(SmtpCase):
    def test_a_lost_answer_after_data_is_unknown_and_never_retried(self):
        self.smtp.data_mode = "drop"
        message = self.real_message()
        result = self.send(message)
        self.assertEqual((result["state"], result["last_error"]), ("desconhecido", "sem_resposta_do_provedor"))
        again = self.send(message)
        self.assertEqual(again["state"], "desconhecido")
        self.assertEqual(len(self.smtp.sessions), 1)  # no automatic second attempt
        self.assertEqual(self.outbound.needing_reconciliation(self.ca)[0]["id"], message["id"])
        resolved = self.outbound.resolve_unknown(self.ca, message["id"], "enviada", provider_id=message["message_id"], detail="achei na pasta Enviados")
        self.assertEqual((resolved["state"], resolved["provider_id"]), ("enviada", message["message_id"]))

    def test_a_timeout_while_the_server_thinks_is_unknown_too(self):
        self.smtp.data_mode, self.smtp.delay = "slow", 2.0
        self.configure(timeout=0.5)
        message = self.real_message()
        result = self.send(message)
        self.assertEqual((result["state"], result["last_error"]), ("desconhecido", "sem_resposta_do_provedor"))
        self.assertIn("aguarda reconciliacao", " ".join(str(e["item"].get("body", "")) for e in self.cases.timeline(self.ca, self.case["id"])))

    def test_refusals_before_or_at_data_are_definitive_and_say_why(self):
        self.smtp.reject_rcpt.add(CLIENT)
        first = self.send(self.real_message())
        self.assertEqual((first["state"], first["last_error"]), ("falhou", "recusada_pelo_provedor:destinatario_recusado"))
        self.assertEqual(self.smtp.delivered(), [])
        self.smtp.reject_rcpt.clear()
        self.smtp.data_mode = "reject"
        other = self.send(self.real_message(dedup_key="outra-chave", body="Segunda tentativa"))
        self.assertEqual((other["state"], other["last_error"]), ("falhou", "recusada_pelo_provedor:dados_recusados"))
        self.assertEqual(self.approvals.get(self.ca, other["approval_id"])["effect_outcome"], "falhou")

    def test_a_wrong_password_is_refused_and_a_revoked_credential_never_connects(self):
        self.smtp.password = "outra-senha"
        failed = self.send(self.real_message())
        self.assertEqual((failed["state"], failed["last_error"]), ("falhou", "recusada_pelo_provedor:autenticacao_recusada"))
        self.assertNotIn(self.smtp.password, json.dumps(failed))
        self.smtp.password = "senha-smtp-0123"
        message = self.real_message(dedup_key="revogada", body="Depois da revogacao")
        before = len(self.smtp.sessions)
        self.connections.revoke_credential(self.ca, self.real["id"])
        revoked = self.send(message)
        self.assertEqual((revoked["state"], revoked["last_error"]), ("falhou", "credencial_ou_conta_indisponivel"))
        self.assertEqual(len(self.smtp.sessions), before)

    def test_no_double_send_and_a_changed_owner_blocks_the_send(self):
        message = self.real_message()
        self.send(message)
        self.assertEqual(self.send(message)["state"], "enviada")
        self.assertEqual(len(self.delivered()), 1)  # the second dispatch found it already sent
        fresh = self.real_message(dedup_key="troca-de-dono", body="Antes da troca")
        self.transfer_to_financeiro()
        blocked = self.send(fresh)
        self.assertEqual(blocked["state"], "bloqueada")
        self.assertEqual(len(self.delivered()), 1)

    def test_acceptance_is_not_delivery_and_it_is_recorded_as_such(self):
        sent = self.send(self.real_message())
        self.assertEqual(sent["state"], "enviada")
        self.assertIn("queued", sent["provider_result"])
        approval = self.approvals.get(self.ca, sent["approval_id"])
        self.assertIn("aceita pelo provedor", approval["effect_detail"])
        self.assertNotIn("entregue", json.dumps(sent).lower())
        self.assertNotIn("lida", json.dumps(sent).lower())


class SimulatedTests(SmtpCase):
    def test_without_an_smtp_configuration_the_simulated_transport_is_used_and_labelled(self):
        simulated_account = self.account  # configured nowhere: no SMTP server was given for it
        message = self.approved(account_id=simulated_account["id"])
        sent = self.send(message)
        self.assertEqual((sent["state"], sent["simulated"], sent["provider_id"]), ("enviada", True, message["message_id"]))
        self.assertIn("nada saiu do computador", sent["provider_result"])
        self.assertEqual(self.smtp.sessions, [])
        entry = [e for e in self.cases.timeline(self.ca, self.case["id"]) if e["tipo"] == "entrada" and e["item"]["direction"] == "saida"][0]["item"]
        self.assertTrue(entry["body"].startswith("[ENVIO SIMULADO"))
        self.assertEqual(len(self.fake.sent), 1)

    def test_imap_connections_cannot_be_outbound_accounts(self):
        imap = self.connections.create_connection(self.ca, label="Caixa", provider="imap", connection_kind="imap")
        with self.assertRaisesRegex(OutboundError, "incompativel"):
            self.draft(account_id=imap["id"], dedup_key="imap")

    def test_the_smtp_setup_belongs_to_the_operator_and_the_right_connection_kind(self):
        agent_ctx = self.agent_ctx()
        with self.assertRaises((PermissionDenied, NotFound)):
            self.accounts.configure(agent_ctx, self.ca, self.real["id"], host="127.0.0.1", username="x", from_address="a@b.com", security="plain")
        with self.assertRaises(NotFound):
            self.accounts.configure(self.op, self.ca, self.account["id"], host="127.0.0.1", username="x", from_address="a@b.com", security="plain")
        with self.assertRaises(NotFound):
            self.accounts.test(self.cb, self.real["id"])
        from team.connections import ConnectionRejected
        for bad in (dict(host="mail.exemplo.com", security="plain"), dict(host="bad host"), dict(port=0), dict(security="nenhuma"),
                    dict(from_address="sem-arroba"), dict(username="a\nb")):
            with self.assertRaises(ConnectionRejected, msg=str(bad)):
                self.configure(**bad)
        self.assertIsNone(self.registry.get(self.b["id"], self.real["id"]))


class ConnectionTestTests(SmtpCase):
    def test_connection_test_reports_success_and_actionable_errors(self):
        self.assertEqual(self.accounts.test(self.ca, self.real["id"]), {"ok": True})
        self.assertEqual(self.smtp.delivered(), [])  # the test sends no message
        self.smtp.password = "trocada"
        bad = self.accounts.test(self.ca, self.real["id"])
        self.assertEqual((bad["ok"], bad["erro"]), (False, "autenticacao_recusada"))
        self.assertNotIn(self.smtp.password, json.dumps(bad))
        self.configure(port=1)
        self.assertEqual(self.accounts.test(self.ca, self.real["id"])["erro"], "sem_conexao")

    @unittest.skipUnless(shutil.which("openssl"), "openssl nao encontrado")
    def test_starttls_with_an_untrusted_certificate_is_refused_and_a_trusted_one_works(self):
        cert, key = Path(self.temp.name, "c.pem"), Path(self.temp.name, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        secure = FakeSmtp(ssl_context=context)
        self.addCleanup(secure.stop)
        self.configure(port=secure.port, security="starttls", host="127.0.0.1")
        refused = self.accounts.test(self.ca, self.real["id"])
        self.assertEqual((refused["ok"], refused["erro"]), (False, "tls_invalido"))
        self.assertEqual(self.send(self.approved(account_id=self.real["id"]))["last_error"], "recusada_pelo_provedor:tls_invalido")
        self.configure(port=secure.port, security="starttls", ca_file=str(cert))
        self.assertEqual(self.accounts.test(self.ca, self.real["id"]), {"ok": True})
        sent = self.send(self.approved(account_id=self.real["id"], dedup_key="seguro", body="Com TLS validado"))
        self.assertEqual(sent["state"], "enviada")
        self.assertTrue(secure.delivered()[0]["tls"])


class ErasureTests(SmtpCase):
    def test_erasing_a_case_removes_its_attachments(self):
        message = self.real_message(attachments=[{"filename": "a.txt", "data": b"dados pessoais"}])
        folder = self.store.root / self.a["id"] / self.case["id"]
        self.assertTrue(folder.exists())
        retention = RetentionService(self.repo, self.access, Path(self.temp.name))
        result = retention.erase_case(self.ca, self.case["id"], self.case["id"])
        self.assertFalse(folder.exists())
        self.assertEqual(result["anexos_removidos"], True)
        self.assertTrue(message)


if __name__ == "__main__":
    unittest.main()
