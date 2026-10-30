"""A fake IMAP server that can also write, for the move tests (#140). It records every command (``trace``) and has switches to break things.

It is a test double only: ``capabilities`` decides whether MOVE and UIDPLUS exist, ``special_use`` whether Trash/Junk carry their RFC 6154
attributes, ``fail_on`` makes the Nth write command fail, ``hang_on`` makes it never answer, and ``folders`` can be changed between calls
to simulate a message that is gone or a UIDVALIDITY that changed. A general ``EXPUNGE`` is answered but also counted in ``general_expunge``.
"""
import re

from tests.imap_fake import FakeImap, Handler, tokens


class FakeImapRW(FakeImap):
    def __init__(self, folders, user="ana@exemplo.invalid", password="senha-de-app", capabilities=("MOVE", "UIDPLUS"), special_use=True,
                 **kwargs):
        super().__init__(folders, user=user, password=password, **kwargs)
        self.RequestHandlerClass = HandlerRW
        self.capabilities, self.special_use = tuple(capabilities), special_use
        self.trace, self.general_expunge, self.writes = [], 0, 0
        self.keep_on_move = False  # MOVE answers OK and copies, but leaves the original in place (a lying server)
        self.fail_on, self.hang_on = None, None  # the Nth write command (1-based) fails / never answers
        self.next_uid = {}

    def uid_of(self, folder, message_id):
        for uid, raw in self.folders.get(folder, {}).get("messages", {}).items():
            if f"Message-ID: {message_id}".lower() in raw.decode("utf-8", "replace").lower():
                return uid
        return None

    def put(self, folder, raw):
        data = self.folders[folder]
        uid = max([self.next_uid.get(folder, 0), *data["messages"]] or [0]) + 1
        self.next_uid[folder] = uid
        data["messages"][uid] = raw
        return uid


ATTRIBUTES = {"Trash": "\\Trash", "Junk": "\\Junk", "Lixeira": "\\Trash", "Spam": "\\Junk"}


class HandlerRW(Handler):
    def serve(self):
        server = self.server
        self.send("* OK fake IMAP (read/write) ready\r\n")
        folder, authenticated = None, False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            server.trace.append(f"{command} {args.split(' ', 1)[0].upper()}".strip() if command == "UID" else command)
            server.log.append(command + (" " + args.split(" ", 1)[0].upper() if command == "UID" else ""))
            if command == "CAPABILITY":
                self.send(f"* CAPABILITY IMAP4rev1 {' '.join(server.capabilities)}\r\n{tag} OK done\r\n")
            elif command == "LOGIN":
                user, password = (tokens(args) + ["", ""])[:2]
                authenticated = (user, password) == (server.user, server.password)
                self.send(f"{tag} OK [CAPABILITY IMAP4rev1 {' '.join(server.capabilities)}] logged in\r\n" if authenticated
                          else f"{tag} NO [AUTHENTICATIONFAILED] bad credentials\r\n")
            elif command == "LOGOUT":
                self.send(f"* BYE bye\r\n{tag} OK done\r\n")
                return
            elif not authenticated:
                self.send(f"{tag} NO login first\r\n")
            elif command == "LIST":
                for name in server.folders:
                    flag = f" {ATTRIBUTES[name]}" if server.special_use and name in ATTRIBUTES else ""
                    self.send(f'* LIST (\\HasNoChildren{flag}) "/" "{name}"\r\n')
                self.send(f"{tag} OK done\r\n")
            elif command in ("EXAMINE", "SELECT"):
                name = tokens(args)[0]
                if name not in server.folders:
                    self.send(f"{tag} NO no such mailbox\r\n")
                    continue
                folder = name
                data = server.folders[name]
                mode = "READ-ONLY" if command == "EXAMINE" else "READ-WRITE"
                self.send(f"* {len(data['messages'])} EXISTS\r\n* OK [UIDVALIDITY {data['validity']}] valid\r\n{tag} OK [{mode}] opened\r\n")
            elif command == "UID" and args.upper().startswith("SEARCH"):
                self.search_rw(tag, args[6:].strip(), folder)
            elif command == "UID" and args.upper().startswith("FETCH"):
                if folder is None:
                    self.send(f"{tag} NO select a folder first\r\n")
                else:
                    self.fetch(tag, args[5:].strip(), folder)
            elif command == "UID" and args.split(" ", 1)[0].upper() in ("MOVE", "COPY", "STORE", "EXPUNGE"):
                if not self.write(tag, args, folder):
                    return
            elif command == "EXPUNGE":
                server.general_expunge += 1  # the thing that must never be sent
                self.send(f"{tag} OK expunged everything marked deleted\r\n")
            else:
                self.send(f"{tag} BAD command not available in the fake\r\n")

    def search_rw(self, tag, criteria, folder):
        header = re.match(r'(?i)HEADER\s+Message-ID\s+("(?:[^"\\]|\\.)*"|\S+)\s*$', criteria)
        if header:
            wanted = tokens(header.group(1))[0].lower()
            found = [uid for uid, raw in self.server.folders[folder]["messages"].items() if f"message-id: {wanted}" in raw.decode("utf-8", "replace").lower()]
            self.send("* SEARCH " + " ".join(str(u) for u in found) + f"\r\n{tag} OK done\r\n")
            return
        single = re.match(r"(?i)UID\s+(\d+)\s*$", criteria)  # a single UID means exactly that UID (the base fake reads "n" as "n:*")
        if single:
            found = [uid for uid in self.server.folders[folder]["messages"] if uid == int(single.group(1))]
            self.send("* SEARCH " + " ".join(str(u) for u in found) + f"\r\n{tag} OK done\r\n")
            return
        self.search(tag, criteria, folder)

    def write(self, tag, args, folder):
        """MOVE / COPY / STORE / UID EXPUNGE. Returns False when the connection must drop (a simulated hang)."""
        server = self.server
        server.writes += 1
        if server.hang_on == server.writes:
            import time
            time.sleep(3)  # never answers: the client times out
            return False
        if server.fail_on == server.writes:
            self.send(f"{tag} NO [SERVERBUG] simulated failure\r\n")
            return True
        parts = args.split(" ", 2)
        verb, uid_text = parts[0].upper(), parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        messages = server.folders[folder]["messages"]
        uid = int(uid_text) if uid_text.isdigit() else None
        if verb in ("MOVE", "COPY"):
            target = tokens(rest)[0]
            if uid not in messages or target not in server.folders:
                self.send(f"{tag} NO no such message or mailbox\r\n")
                return True
            new_uid = server.put(target, messages[uid])
            code = f"* OK [COPYUID {server.folders[target]['validity']} {uid} {new_uid}] done\r\n"
            if verb == "MOVE":
                if not server.keep_on_move:
                    del messages[uid]
                self.send(f"* {uid} EXPUNGE\r\n" + code + f"{tag} OK moved\r\n")
            else:
                self.send(code + f"{tag} OK copied\r\n")
        elif verb == "STORE":
            if uid not in messages:
                self.send(f"{tag} NO no such message\r\n")
                return True
            server.marked = getattr(server, "marked", set()) | {(folder, uid)}
            self.send(f"{tag} OK stored\r\n")
        elif verb == "EXPUNGE":
            if uid is None or "UIDPLUS" not in server.capabilities:
                self.send(f"{tag} BAD UID EXPUNGE needs UIDPLUS\r\n")
                return True
            if (folder, uid) in getattr(server, "marked", set()):
                messages.pop(uid, None)
                self.send(f"* 1 EXPUNGE\r\n{tag} OK expunged one\r\n")
            else:
                self.send(f"{tag} OK nothing to expunge\r\n")
        return True
