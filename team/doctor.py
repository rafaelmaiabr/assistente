"""Installation check for the team: dependencies, data folder, database migrations, backups, vault, model server and remote access.

Every line says what was found and, when it is not fine, **what to do about it**. It never migrates, writes to the database
or touches a credential; the only thing it creates is the (empty) data folder and a temporary file there to prove it is writable. Run it with ``python -m team.doctor`` or from the Equipe screen (local only).
"""
import json
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import persistence
from .db import MIGRATIONS_DIR, _applied, discover
from .login import SECRET_SLOT, ConfigError, RemoteConfig
from .onboarding import load_templates

OK, WARN, ERROR = "ok", "aviso", "erro"
MIN_PYTHON = (3, 11)


def item(item_id, title, state, detail, action=None):
    return {"id": item_id, "titulo": title, "estado": state, "detalhe": detail, "acao": action}


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] < MIN_PYTHON:
        return item("python", "Python", ERROR, f"Python {version}.", f"Instale o Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} ou mais novo (o pacote Windows ja traz o seu).")
    return item("python", "Python", OK, f"Python {version}.")


def check_dependencies():
    missing, notes = [], []
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        missing.append("aiohttp")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Sao_Paulo")
    except Exception:
        missing.append("tzdata")
    if sys.platform == "win32":
        try:
            import win32cred  # noqa: F401
        except ImportError:
            missing.append("pywin32")
    if missing:
        return item("dependencias", "Dependencias", ERROR, "Faltam: " + ", ".join(missing) + ".",
                    "Reinstale as dependencias do projeto (`uv pip install -r requirements.txt`); no pacote Windows, reinstale o aplicativo.")
    return item("dependencias", "Dependencias", OK, "aiohttp, fusos horarios" + (" e cofre do Windows" if sys.platform == "win32" else "") + " disponiveis.")


def check_data_folder(root):
    folder = persistence.data_dir(root)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".verificacao-", delete=True):
            pass
    except OSError as error:
        return item("pasta_de_dados", "Pasta de dados", ERROR, f"Nao foi possivel gravar em {folder} ({type(error).__name__}).",
                    "Confira as permissoes da pasta ou escolha outra com ASSISTENTE_DATA_DIR.")
    return item("pasta_de_dados", "Pasta de dados", OK, f"Gravavel: {folder}.")


def check_persistence(root):
    if persistence.status(root)["enabled"]:
        return item("persistencia", "Atendimento persistente", OK, "Ligado: demandas e configuracoes ficam salvas neste computador.")
    return item("persistencia", "Atendimento persistente", WARN, "Desligado: a equipe nao salva demandas e o chat continua efemero.",
                "Ative na secao Equipe quando quiser usar a equipe. Os dados existentes nao sao apagados ao desligar.")


def check_database(root):
    path = persistence.data_dir(root) / persistence.DATABASE
    if not path.exists():
        return item("banco", "Banco de dados", OK if not persistence.status(root)["enabled"] else WARN, "Ainda nao existe (e criado ao ativar o atendimento).",
                    None if not persistence.status(root)["enabled"] else "Reinicie o aplicativo; o banco e criado na partida.")
    available = {number: (name, checksum) for number, name, _, checksum in discover(MIGRATIONS_DIR)}
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return item("banco", "Banco de dados", ERROR, f"Nao abriu ({type(error).__name__}).", "Restaure o ultimo backup (TEAM-OPS.md) com o aplicativo fechado.")
    try:
        try:
            applied = _applied(conn)
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as error:
            return item("banco", "Banco de dados", ERROR, f"Ilegivel ({type(error).__name__}).", "Restaure o ultimo backup com o aplicativo fechado.")
    finally:
        conn.close()
    future = sorted(set(applied) - set(available))
    changed = sorted(n for n, (_, checksum) in applied.items() if n in available and available[n][1] != checksum)
    pending = sorted(set(available) - set(applied))
    if integrity != "ok":
        return item("banco", "Banco de dados", ERROR, "A verificacao de integridade do SQLite falhou.", "Feche o aplicativo e restaure o ultimo backup (TEAM-OPS.md).")
    if future:
        return item("banco", "Banco de dados", ERROR, f"O banco tem migracoes desconhecidas desta versao ({', '.join(f'{n:04d}' for n in future)}).",
                    "Este programa e mais antigo que o banco: atualize o aplicativo. Nada foi alterado.")
    if changed:
        return item("banco", "Banco de dados", ERROR, f"Migracao ja aplicada foi alterada ({', '.join(f'{n:04d}' for n in changed)}).",
                    "Reinstale a versao correta do aplicativo; nao edite migracoes aplicadas.")
    if pending:
        return item("banco", "Banco de dados", WARN, f"{len(pending)} migracao(oes) pendente(s): {', '.join(f'{n:04d}' for n in pending)}.",
                    "Elas sao aplicadas ao iniciar o aplicativo, depois de guardar uma copia `.pre-migracao`. Faca um backup antes de atualizar.")
    return item("banco", "Banco de dados", OK, f"Integro e atualizado ({len(applied)} migracoes aplicadas).")


def check_backups(root):
    folder = persistence.data_dir(root) / "backups"
    if not (persistence.data_dir(root) / persistence.DATABASE).exists():
        return item("backup", "Backup", OK, "Ainda nao ha dados para guardar.")
    done = sorted((p for p in folder.glob("*/manifest.json")), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if not done:
        return item("backup", "Backup", WARN, "Nenhum backup foi feito.", "Crie um backup antes de atualizar o aplicativo (TEAM-OPS.md). Segredos nao entram no backup.")
    age = (datetime.now(timezone.utc) - datetime.fromtimestamp(done[-1].stat().st_mtime, timezone.utc)).days
    if age > 30:
        return item("backup", "Backup", WARN, f"O ultimo backup tem {age} dias.", "Faca um backup novo antes de atualizar.")
    return item("backup", "Backup", OK, f"Ultimo backup ha {age} dia(s).")


def check_vault(vault):
    if vault is None:
        return item("cofre", "Cofre de credenciais", WARN, "Nao verificado.", None)
    try:
        vault.get("diagnostico", "inexistente")
    except Exception:
        return item("cofre", "Cofre de credenciais", ERROR, "O cofre do sistema nao respondeu.",
                    "No Windows, confirme que o Gerenciador de Credenciais esta disponivel e que o pywin32 esta instalado; as credenciais da equipe dependem dele.")
    return item("cofre", "Cofre de credenciais", OK, "Responde. Senhas e chaves ficam so nele; backups nao as levam.")


def check_model_server(config, probe):
    url = str(config.get("ollama_url") or "")
    reachable = probe(url)
    if reachable:
        return item("modelo", "Servidor do modelo (Ollama)", OK, f"Responde em {url}.")
    return item("modelo", "Servidor do modelo (Ollama)", WARN, f"Nao respondeu em {url}.",
                "Abra o Ollama (ou `ollama serve`) e baixe o modelo configurado; a equipe e o chat precisam dele, mas nada e apagado enquanto estiver parado.")


def probe_http(url, timeout=2.0):
    """True when ``url`` (the OpenAI-compatible /v1 root of the local model server) answers at all."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/models"), timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError:
        return True  # it answered
    except (urllib.error.URLError, OSError, ValueError):
        return False


def check_remote(config, vault):
    try:
        remote = RemoteConfig.from_config(config)
    except ConfigError as error:
        return item("acesso_remoto", "Acesso remoto", ERROR, f"Configuracao invalida: {error}.", "Corrija a secao `remote` do config.json; enquanto isso o acesso remoto nao sobe.")
    if not remote.enabled:
        return item("acesso_remoto", "Acesso remoto", OK, "Desligado (o padrao): so este computador acessa a equipe.")
    try:
        has_secret = bool(vault.get(*SECRET_SLOT)) if vault is not None else False
    except Exception:
        has_secret = False
    if not has_secret:
        return item("acesso_remoto", "Acesso remoto", WARN, f"Ligado em {remote.public_origin}, mas falta o segredo do cliente Google.",
                    "Rode `python scripts/remote_setup.py set-secret`. Sem ele ninguem consegue entrar.")
    return item("acesso_remoto", "Acesso remoto", OK, f"Ligado em {remote.public_origin} (escuta {remote.bind}:{remote.port}). A validacao com o Google real e manual.")


def check_templates():
    try:
        found = load_templates()
    except Exception as error:
        return item("modelos", "Modelos de equipe", ERROR, f"Um modelo e invalido ({type(error).__name__}).", "Reinstale o aplicativo; os modelos fazem parte do pacote.")
    return item("modelos", "Modelos de equipe", OK, f"{len(found)} modelo(s) disponivel(is): {', '.join(sorted(found))}.")


def run(root, config, *, vault=None, probe=probe_http):
    """All checks. ``ok`` is false only when something is broken (``erro``); warnings are things to do, not failures."""
    items = [check_python(), check_dependencies(), check_data_folder(root), check_persistence(root), check_database(root), check_backups(root),
             check_vault(vault), check_model_server(config, probe), check_remote(config, vault), check_templates()]
    return {"ok": not any(i["estado"] == ERROR for i in items), "itens": items, "somente_leitura": True}


def main(argv=None):
    from settings import DATA_ROOT, load_config
    from .vault import WindowsVault
    report = run(DATA_ROOT, load_config(), vault=WindowsVault(DATA_ROOT) if sys.platform == "win32" else None)
    for entry in report["itens"]:
        mark = {"ok": "[ok]    ", "aviso": "[aviso] ", "erro": "[ERRO]  "}[entry["estado"]]
        print(f"{mark}{entry['titulo']}: {entry['detalhe']}")
        if entry["acao"] and entry["estado"] != OK:
            print(f"         -> {entry['acao']}")
    print("\nResultado:", "sem erros" if report["ok"] else "ha erros a corrigir")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
