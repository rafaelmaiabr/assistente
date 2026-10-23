"""A tiny fake IMAP server for tests: it records every command it receives and can only read."""
import base64
import re
import socket
import socketserver
import ssl
import threading
from email.utils import parsedate_to_datetime


def tokens(text):
    """Split an IMAP argument string keeping quoted strings together."""
    return [m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m.group(1) is not None else m.group(2)
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\S+)', text)]


class FakeImap(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, folders, user="ana@exemplo.invalid", password="senha-de-app", ssl_context=None):
        super().__init__(("127.0.0.1", 0), Handler)
        self.ssl_context = ssl_context
        self.folders = folders  # {"INBOX": {"validity": 11, "messages": {uid: raw_bytes}}}
        self.user, self.password = user, password
        self.log, self.seen, self.delay = [], set(), 0
        self.oauth_tokens = set()  # access tokens accepted by AUTHENTICATE XOAUTH2
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def get_request(self):
        sock, address = super().get_request()
        if self.ssl_context is not None:
            try:
                sock = self.ssl_context.wrap_socket(sock, server_side=True)
            except OSError:  # a failed handshake (for example an untrusting client) just drops the connection
                sock.close()
                raise
        return sock, address

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()

    def commands(self):
        return [entry.split(" ", 1)[0] if not entry.startswith("UID ") else " ".join(entry.split(" ")[:2]) for entry in self.log]


class Handler(socketserver.StreamRequestHandler):
    def send(self, text):
        self.wfile.write(text.encode("utf-8") if isinstance(text, str) else text)
        self.wfile.flush()

    def handle(self):
        if isinstance(self.request, ssl.SSLSocket):
            return self.serve()
        self.request.settimeout(0.3)  # a TLS client speaks first; a plain IMAP client waits for the greeting
        try:
            first = self.request.recv(1, socket.MSG_PEEK)
        except socket.timeout:
            first = b""
        except OSError:
            return
        self.request.settimeout(None)
        if first == b"\x16":  # a TLS ClientHello arrived at a plain server
            self.send("* BYE this server does not speak TLS\r\n")
            return
        self.serve()

    def serve(self):
        server = self.server
        self.send("* OK fake IMAP ready\r\n")
        folder, authenticated = None, False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            server.log.append(command + (" " + args.split(" ", 1)[0].upper() if command == "UID" else ""))
            if server.delay and command not in ("LOGOUT",):
                import time
                time.sleep(server.delay)
            if command == "CAPABILITY":
                self.send(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n")
            elif command == "LOGIN":
                user, password = (tokens(args) + ["", ""])[:2]
                authenticated = (user, password) == (server.user, server.password)
                self.send(f"{tag} OK logged in\r\n" if authenticated else f"{tag} NO [AUTHENTICATIONFAILED] bad credentials\r\n")
            elif command == "AUTHENTICATE":
                if args.split()[:1] != ["XOAUTH2"]:
                    self.send(f"{tag} NO mecanismo nao suportado\r\n")
                    continue
                self.send("+ \r\n")
                answer = base64.b64decode(self.rfile.readline().strip() or b"").decode("utf-8", "replace")
                fields = dict(part.split("=", 1) for part in answer.split("\x01") if "=" in part)
                token = fields.get("auth", "").removeprefix("Bearer ")
                authenticated = fields.get("user") == server.user and token in server.oauth_tokens
                self.send(f"{tag} OK autenticado\r\n" if authenticated else f"{tag} NO [AUTHENTICATIONFAILED] token invalido\r\n")
            elif command == "LOGOUT":
                self.send(f"* BYE bye\r\n{tag} OK done\r\n")
                return
            elif not authenticated:
                self.send(f"{tag} NO login first\r\n")
            elif command == "LIST":
                for name in server.folders:
                    self.send(f'* LIST (\\HasNoChildren) "/" "{name}"\r\n')
                self.send(f"{tag} OK done\r\n")
            elif command == "EXAMINE":
                name = tokens(args)[0]
                if name not in server.folders:
                    self.send(f"{tag} NO no such mailbox\r\n")
                    continue
                folder = name
                data = server.folders[name]
                self.send(f"* {len(data['messages'])} EXISTS\r\n* OK [UIDVALIDITY {data['validity']}] valid\r\n{tag} OK [READ-ONLY] examined\r\n")
            elif command == "UID" and args.upper().startswith("SEARCH"):
                self.search(tag, args[6:].strip(), folder)
            elif command == "UID" and args.upper().startswith("FETCH"):
                self.fetch(tag, args[5:].strip(), folder)
            else:  # SELECT, STORE, COPY, MOVE, DELETE, EXPUNGE, APPEND ...: this server never allows them
                self.send(f"{tag} BAD command not available in the read-only fake\r\n")

    def search(self, tag, criteria, folder):
        messages = self.server.folders[folder]["messages"]
        words, matches = tokens(criteria), []
        for uid, raw in messages.items():
            text = raw.decode("utf-8", "replace")
            ok, i = True, 0
            while i < len(words):
                key = words[i].upper()
                if key == "ALL":
                    i += 1
                    continue
                value = words[i + 1] if i + 1 < len(words) else ""
                header = lambda name: next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.lower().startswith(name + ":")), "")  # noqa: E731
                if key == "TEXT":
                    ok = ok and value.lower() in text.lower()
                elif key == "FROM":
                    ok = ok and value.lower() in header("from").lower()
                elif key == "SUBJECT":
                    ok = ok and value.lower() in header("subject").lower()
                elif key in ("SINCE", "BEFORE"):
                    sent = parsedate_to_datetime(header("date")).date()
                    from datetime import datetime
                    limit = datetime.strptime(value, "%d-%b-%Y").date()
                    ok = ok and (sent >= limit if key == "SINCE" else sent < limit)
                elif key == "UID":
                    low, _, high = value.partition(":")
                    ok = ok and int(low) <= uid <= (int(high) if high not in ("", "*") else 10 ** 9)
                i += 2
            if ok:
                matches.append(uid)
        self.send("* SEARCH " + " ".join(str(u) for u in matches) + f"\r\n{tag} OK done\r\n")

    def fetch(self, tag, args, folder):
        uid_text, _, items = args.partition(" ")
        raw = self.server.folders[folder]["messages"].get(int(uid_text))
        if raw is None:
            self.send(f"{tag} OK done\r\n")
            return
        if "BODY[" in items and "BODY.PEEK[" not in items.replace("BODY.PEEK[", "BODY.PEEK[") or re.search(r"(?<![.\w])BODY\[|RFC822(?!\.SIZE)", items):
            self.server.seen.add(int(uid_text))  # a non-PEEK fetch would mark the message as read
        parts = [f"UID {uid_text}"]
        if "RFC822.SIZE" in items:
            parts.append(f"RFC822.SIZE {len(raw)}")
        literal, label = None, ""
        header = re.search(r"BODY\.PEEK\[HEADER\.FIELDS \(([A-Z -]+)\)\]", items)
        partial = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", items)
        if header:
            wanted = {w.lower() for w in header.group(1).split()}
            lines = [l for l in raw.decode("utf-8", "replace").split("\r\n\r\n")[0].splitlines() if l.split(":", 1)[0].lower() in wanted]
            literal, label = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"), f"BODY[HEADER.FIELDS ({header.group(1)})]"
        elif partial:
            literal, label = raw[: int(partial.group(1))], "BODY[]<0>"
        body = f"* 1 FETCH ({' '.join(parts)}"
        if literal is not None:
            self.send(f"{body} {label} {{{len(literal)}}}\r\n".encode() + literal + b")\r\n")
        else:
            self.send(body + ")\r\n")
        self.send(f"{tag} OK done\r\n")
