"""Shared message builder for the IMAP tests."""
from email.message import EmailMessage


def build(subject="Ola", sender="Ana <ana@exemplo.invalid>", body="Texto simples", date="Mon, 01 Jun 2026 10:00:00 +0000", **extra):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"], msg["Message-ID"] = sender, "eu@exemplo.invalid", subject, date, "<m1@exemplo.invalid>"
    msg.set_content(body)
    for name, value in extra.items():
        msg[name] = value
    return msg
