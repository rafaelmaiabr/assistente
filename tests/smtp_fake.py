"""A small fake SMTP server for tests. It speaks just enough of the protocol, records every session and can misbehave."""
import base64
import socket
import socketserver
import ssl
import threading
import time


class FakeSmtp(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user="atendimento@empresa.invalid", password="senha-smtp-0123", ssl_context=None, implicit_tls=False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.user, self.password = user, password
        self.ssl_context, self.implicit_tls = ssl_context, implicit_tls
        self.reject_rcpt, self.data_mode, self.delay = set(), "ok", 0.0  # data_mode: ok | drop | reject | slow
        self.sessions, self.log = [], []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()

    def get_request(self):
        sock, address = super().get_request()
        if self.implicit_tls and self.ssl_context is not None:
            try:
                sock = self.ssl_context.wrap_socket(sock, server_side=True)
            except OSError:
                sock.close()
                raise
        return sock, address

    def delivered(self):
        return [s for s in self.sessions if s["accepted"]]


class Handler(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write(text.encode("utf-8") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        server = self.server
        session = {"from": None, "rcpts": [], "data": b"", "user": None, "accepted": False, "authenticated": False, "tls": False}
        server.sessions.append(session)
        self.reply("220 fake.smtp ESMTP pronto")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, rest = text.partition(" ")
            verb = verb.upper()
            server.log.append(verb)
            if verb in ("EHLO", "HELO"):
                extras = ["250-fake.smtp", "250-AUTH PLAIN LOGIN"]
                if server.ssl_context is not None and not server.implicit_tls and not session["tls"]:
                    extras.append("250-STARTTLS")
                self.reply("\r\n".join(extras + ["250 8BITMIME"]))
            elif verb == "STARTTLS":
                if server.ssl_context is None:
                    self.reply("502 sem TLS")
                    continue
                self.reply("220 pronto para TLS")
                try:
                    secure = server.ssl_context.wrap_socket(self.request, server_side=True)
                except OSError:
                    return
                self.request = secure
                self.rfile, self.wfile = secure.makefile("rb"), secure.makefile("wb")
                session["tls"] = True
            elif verb == "AUTH":
                parts = rest.split()
                if parts and parts[0].upper() == "PLAIN" and len(parts) > 1:
                    _, user, password = base64.b64decode(parts[1]).decode().split("\x00")
                    ok = (user, password) == (server.user, server.password)
                    session["user"], session["authenticated"] = user, ok
                    self.reply("235 2.7.0 autenticado" if ok else "535 5.7.8 credenciais invalidas")
                else:
                    self.reply("504 mecanismo nao suportado")
            elif verb == "MAIL":
                if not session["authenticated"]:
                    self.reply("530 5.7.0 autentique-se primeiro")
                    continue
                session["from"] = rest.split(":", 1)[-1].strip().strip("<>").split(">")[0]
                self.reply("250 2.1.0 remetente ok")
            elif verb == "RCPT":
                address = rest.split(":", 1)[-1].strip().strip("<>").split(">")[0]
                if address in server.reject_rcpt:
                    self.reply("550 5.1.1 destinatario inexistente")
                else:
                    session["rcpts"].append(address)
                    self.reply("250 2.1.5 destinatario ok")
            elif verb == "DATA":
                self.reply("354 envie os dados")
                data = b""
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data += chunk[1:] if chunk.startswith(b"..") else chunk
                session["data"] = data
                if server.data_mode == "drop":
                    return  # close without a verdict: the client cannot know whether it was accepted
                if server.data_mode == "slow":
                    time.sleep(server.delay)
                if server.data_mode == "reject":
                    self.reply("554 5.7.1 mensagem rejeitada")
                    continue
                session["accepted"] = True
                self.reply("250 2.0.0 Ok: queued as FAKE123")
            elif verb == "RSET":
                session["from"], session["rcpts"] = None, []
                self.reply("250 2.0.0 ok")
            elif verb == "QUIT":
                self.reply("221 2.0.0 tchau")
                return
            else:
                self.reply("500 comando desconhecido")
