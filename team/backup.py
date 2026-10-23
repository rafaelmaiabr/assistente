"""Consistent local backup and restore of the team database.

A backup is a SQLite online copy (consistent even while the app writes) plus a manifest with the schema
version, a checksum and row counts. **Secrets are not included**: the credential vault (Windows
credential store) lives outside the database, and the database only holds references to it, so after a
restore on another computer the passwords must be entered again. The backup itself is **not encrypted**:
it contains customer data, so keep it in a folder only you can read (the files are created readable by the
current user only where the system supports it) and never publish it.

Restoring replaces the live database (stop the app first), keeps the previous file as ``.pre-restore``, checks
integrity and the manifest first, and then reconciles what the outside world did while the backup was old:
running jobs return to the queue (or to reconciliation when an external effect was in doubt) and messages that
were being sent become *unknown* for an operator decision. Nothing is re-sent automatically.
"""
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from .access import OperatorContext, PermissionDenied
from .db import MigrationError, connect, discover, migrate
from .jobs import JobQueue
from .repository import SqliteRepository

FORMAT = 1
DB_NAME = "team.sqlite3"
MANIFEST = "manifest.json"
NOTICE = ("O backup contem dados de clientes sem criptografia e NAO inclui senhas (ficam no cofre do sistema). "
          "Guarde em pasta protegida; apos restaurar em outro computador, cadastre as senhas novamente.")


class BackupInvalid(ValueError):
    """The backup cannot be used; the message says why and is safe to show."""


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                       "AND name NOT LIKE 'knowledge_index%' ORDER BY name")]


class BackupService:
    def __init__(self, repository: SqliteRepository, operator_id, base_dir, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.operator_id, self.base, self.clock = repository, operator_id, base_dir, clock
        self.root = base_dir / "backups"

    def _operator(self, operator):
        if not isinstance(operator, OperatorContext) or operator.principal_id != self.operator_id:
            raise PermissionDenied()

    # -- create --------------------------------------------------------------------------------------------------
    def create(self, operator, destination=None):
        self._operator(operator)
        now = self.clock()
        folder = (destination or self.root) / now.strftime("%Y%m%dT%H%M%SZ")
        folder.mkdir(parents=True, exist_ok=False)
        target = folder / DB_NAME
        source = connect(self.repository.path)
        copy = sqlite3.connect(str(target))
        try:
            source.backup(copy)
            check = copy.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise BackupInvalid("A copia nao passou na verificacao de integridade; o banco de origem precisa de atencao.")
            rows = {name: copy.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in _tables(copy)}
            version = copy.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            flight = {
                "jobs_em_execucao": copy.execute("SELECT COUNT(*) FROM jobs WHERE state = 'em_execucao'").fetchone()[0],
                "mensagens_enviando": copy.execute("SELECT COUNT(*) FROM outbound_messages WHERE state = 'enviando'").fetchone()[0],
                "caixa_de_saida_pendente": copy.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]}
        except BaseException:
            copy.close()
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            source.close()
        copy.close()
        manifest = {"formato": FORMAT, "criado_em": _stamp(now), "versao_do_esquema": version, "arquivo": DB_NAME,
                    "sha256": _sha256(target), "tamanho": target.stat().st_size, "linhas": rows, "em_andamento": flight,
                    "segredos": "nao incluidos", "criptografado": False, "aviso": NOTICE}
        (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for file in (target, folder / MANIFEST):
            try:
                os.chmod(file, 0o600)
            except OSError:
                pass
        return {"pasta": str(folder), **manifest}

    def list_backups(self, operator):
        self._operator(operator)
        found = []
        for folder in sorted(self.root.glob("*")) if self.root.exists() else []:
            try:
                found.append({"pasta": str(folder), **json.loads((folder / MANIFEST).read_text(encoding="utf-8"))})
            except (OSError, ValueError):
                continue
        return found

    # -- verify --------------------------------------------------------------------------------------------------
    def verify(self, folder):
        """Check the manifest, checksum, integrity, foreign keys and schema version of a backup folder."""
        try:
            manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BackupInvalid("Pasta sem manifesto valido; nao parece um backup desta ferramenta.") from None
        if manifest.get("formato") != FORMAT or manifest.get("arquivo") != DB_NAME:
            raise BackupInvalid("Formato de backup desconhecido.")
        database = folder / DB_NAME
        if not database.is_file() or _sha256(database) != manifest.get("sha256"):
            raise BackupInvalid("O arquivo do backup foi alterado ou esta incompleto (soma de verificacao diferente).")
        known = len(discover())
        if not isinstance(manifest.get("versao_do_esquema"), int) or manifest["versao_do_esquema"] > known:
            raise BackupInvalid("O backup foi feito por uma versao mais nova do aplicativo; atualize antes de restaurar.")
        conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupInvalid("O banco do backup esta corrompido.")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupInvalid("O banco do backup tem referencias quebradas.")
        except sqlite3.DatabaseError:
            raise BackupInvalid("O arquivo do backup nao e um banco valido.") from None
        finally:
            conn.close()
        return manifest

    # -- restore -------------------------------------------------------------------------------------------------
    def restore(self, operator, folder, *, overwrite=False):
        self._operator(operator)
        manifest = self.verify(folder)
        live = self.repository.path
        if live.exists() and not overwrite:
            raise BackupInvalid("Ja existe um banco em uso. Pare o aplicativo e confirme a substituicao (overwrite=True).")
        live.parent.mkdir(parents=True, exist_ok=True)
        staged = live.with_name(live.name + ".restoring")
        shutil.copy2(folder / DB_NAME, staged)
        previous = None
        try:
            if live.exists():
                previous = live.with_name(f"{live.name}.pre-restore-{self.clock().strftime('%Y%m%dT%H%M%SZ')}")
                os.replace(live, previous)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(str(live) + suffix)
                except FileNotFoundError:
                    pass
            os.replace(staged, live)
        except OSError as error:
            if previous is not None and not live.exists():
                os.replace(previous, live)  # put the old database back
            staged.unlink(missing_ok=True)
            raise BackupInvalid(f"Nao foi possivel substituir o banco ({error.strerror or 'erro de arquivo'}); o aplicativo esta em uso?") from None
        try:
            applied = migrate(live)
        except MigrationError as error:
            raise BackupInvalid(f"O backup foi restaurado, mas a migracao falhou: {error}") from None
        report = self.reconcile()
        return {"restaurado_de": str(folder), "criado_em": manifest["criado_em"], "migracoes_aplicadas": applied,
                "banco_anterior": None if previous is None else str(previous), **report}

    def reconcile(self):
        """Settle what a restored snapshot cannot know: running jobs and messages in the middle of sending."""
        queue = JobQueue(self.repository)
        jobs = queue.recover_all()
        unknown, now = 0, _stamp(self.clock())
        pending_outbox = 0
        for tenant in self.repository.list_global("tenant", limit=1000):
            with self.repository.transaction(tenant["id"]) as session:
                for message in session.list("outbound_message", state="enviando", limit=1000):
                    session.save({**message, "state": "desconhecido", "last_error": "restaurado_do_backup",
                                  "revision": message["revision"] + 1, "updated_at": now})
                    unknown += 1
                pending_outbox += session.conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE tenant_id = ? AND status = 'pending'", (tenant["id"],)).fetchone()[0]
        return {"jobs_apos_restauracao": jobs, "mensagens_para_revisao": unknown, "caixa_de_saida_pendente": pending_outbox,
                "proximo_passo": "Revise jobs em 'reconciliar' e mensagens 'desconhecido' antes de reativar os envios; nada foi reenviado."}

    # -- retention -----------------------------------------------------------------------------------------------
    def prune(self, operator, keep_days):
        """Remove backups older than ``keep_days`` (the newest backup is always kept)."""
        self._operator(operator)
        if not (isinstance(keep_days, int) and not isinstance(keep_days, bool) and keep_days >= 1):
            raise BackupInvalid("Informe a idade maxima em dias (1 ou mais).")
        limit, removed = self.clock() - timedelta(days=keep_days), []
        backups = self.list_backups(operator)
        for item in backups[:-1]:
            created = datetime.fromisoformat(item["criado_em"].replace("Z", "+00:00"))
            if created <= limit:
                shutil.rmtree(item["pasta"], ignore_errors=True)
                removed.append(item["pasta"])
        return removed
