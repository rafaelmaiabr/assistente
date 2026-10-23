"""Opt-in switch for persistent cases; the ephemeral chat keeps working either way.

State lives under ``<data root>/team-data`` (outside Git and the packaged resources). Turning the
switch off never deletes the database, and enabling never imports earlier conversations.
"""
import json
import os
from pathlib import Path

from .db import MigrationError
from .access import AccessService
from .ids import is_id, new_id
from .repository import SqliteRepository
from .service import TeamService

DIRECTORY = "team-data"
DATABASE = "team.sqlite3"
FLAG = "persistence.json"
NOTICE_OFF = ("Atendimento persistente desativado. O chat continua efêmero: o histórico é descartado "
              "ao atualizar a página.")
NOTICE_ON = ("Atendimento persistente ativado. Demandas e configurações da equipe ficam salvas neste "
             "computador. Conversas anteriores não são importadas e o chat efêmero continua disponível.")


def data_dir(root) -> Path:
    return Path(root) / DIRECTORY


def repository(root) -> SqliteRepository:
    return SqliteRepository(data_dir(root) / DATABASE)


def _enabled(root) -> bool:
    try:
        return json.loads((data_dir(root) / FLAG).read_text(encoding="utf-8")).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def status(root, error=None) -> dict:
    enabled = _enabled(root)
    return {"enabled": enabled, "notice": NOTICE_ON if enabled else NOTICE_OFF, "error": error}


def enable(root) -> dict:
    """Migrate the database first; the flag is only set when that succeeds."""
    try:
        repository(root).migrate()
    except MigrationError as error:
        return status(root, error=str(error))
    return _write(root, True)


def disable(root) -> dict:
    return _write(root, False)


def _write(root, enabled) -> dict:
    folder = data_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / (FLAG + ".tmp")
    temp.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    os.replace(temp, folder / FLAG)
    return status(root)


def operator_id(root) -> str:
    """Stable id of the single trusted local operator, created on first use."""
    path = data_dir(root) / "operator.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("operator_id")
        if is_id(value, "usr"):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    value = new_id("user")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"operator_id": value}), encoding="utf-8")
    os.replace(temp, path)
    return value


def team_service(root) -> TeamService:
    """Service over the (already migrated) local database; the backend creates contexts from it."""
    repo = repository(root)
    return TeamService(repo, AccessService(repo, operator_id(root)))
