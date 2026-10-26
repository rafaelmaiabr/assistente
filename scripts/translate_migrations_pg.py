"""Translate the SQLite migrations (team/migrations) into PostgreSQL ones (team/migrations_pg).

    python scripts/translate_migrations_pg.py           # rewrite team/migrations_pg
    python scripts/translate_migrations_pg.py --check   # exit 1 if the committed files differ (used by a test)

SQLite stays the source of truth: a new migration is written once, in SQLite, and translated. The translation is mechanical
and deliberately strict: anything it does not recognise stops it, so a new SQLite-only construct is a decision, not a surprise.
The full-text index (FTS5 virtual table) becomes an ordinary table with a ``tsvector`` column and a GIN index (team/knowledge.py).
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "team" / "migrations"
TARGET = ROOT / "team" / "migrations_pg"
HEADER = "-- Gerado por scripts/translate_migrations_pg.py a partir de team/migrations/{name}. Nao edite: altere a migracao SQLite e gere de novo.\n"


class Untranslatable(ValueError):
    pass


TRIGGER_TABLES = {}  # trigger name -> table, filled as migrations are read in order (DROP TRIGGER needs the table in PostgreSQL)


def split_statements(sql):
    """Statements split on ';' outside quotes and outside BEGIN ... END trigger bodies."""
    statements, current, quote = [], [], None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end < 0 else end
            current.append(sql[i:end])
            i = end
            continue
        elif ch == ";":
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
            tail = "".join(current[-12:]).upper()
            if re.search(r"\bBEGIN\s$", tail + " "[:0]) and ch.isspace():
                pass
        i += 1
    rest = "".join(current).strip()
    if rest:
        statements.append(rest)
    return statements


def logical_statements(sql):
    """Split a migration into statements, keeping CREATE TRIGGER ... BEGIN ...; END as one unit."""
    pieces, buffer, in_trigger = [], "", False
    for raw in split_statements(sql):
        chunk = raw.strip()
        if not chunk:
            continue
        body = re.sub(r"--[^\n]*", "", chunk).strip()
        if not body:
            pieces.append(chunk)  # a comment on its own
            continue
        if in_trigger:
            buffer += ";\n" + chunk
            if re.search(r"\bEND\s*$", body, re.I):
                pieces.append(buffer)
                buffer, in_trigger = "", False
            continue
        if re.match(r"CREATE\s+TRIGGER", body, re.I) and not re.search(r"\bEND\s*$", body, re.I):
            buffer, in_trigger = chunk, True
            continue
        pieces.append(chunk)
    if in_trigger:
        raise Untranslatable("gatilho sem END")
    return pieces


def expr(text):
    """SQLite expression -> PostgreSQL expression (the subset the migrations use)."""
    text = re.sub(r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.(\w+)'\s*\)", lambda m: f"({m.group(1)}::jsonb -> '{m.group(2)}')"
                  if m.group(1).upper().startswith(("NEW.", "OLD.")) else f"({m.group(1)}->>'{m.group(2)}')", text)
    text = re.sub(r"\bIS NOT (NEW\.|OLD\.|\()", r"IS DISTINCT FROM \1", text)
    return text


def trigger(statement):
    match = re.match(r"CREATE\s+TRIGGER\s+(\w+)\s+BEFORE\s+(INSERT|UPDATE(?:\s+OF\s+[\w,\s]+?)?|DELETE)\s+ON\s+(\w+)\s*(?:WHEN\s+(.*?))?\s*BEGIN\s+SELECT\s+RAISE\(ABORT,\s*'([^']*)'\);\s*END$",
                     re.sub(r"--[^\n]*", "", statement).strip(), re.I | re.S)
    if not match:
        raise Untranslatable("gatilho fora do padrao: " + statement[:80])
    name, event, table, when, message = match.groups()
    TRIGGER_TABLES[name] = table
    event = re.sub(r"\s+", " ", event.strip())
    returns = "OLD" if event.upper().startswith("DELETE") else "NEW"
    condition = expr(when.strip()) if when else None
    raise_sql = f"RAISE EXCEPTION '{message}' USING ERRCODE = '23000'"
    body = f"IF {condition} THEN {raise_sql}; END IF;" if condition else raise_sql + ";"
    return (f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN {returns}; END $$;\n"
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn();")


# The PostgreSQL counterpart of the FTS5 table: the words are folded and stored by the application (accents removed, as the SQLite
# tokenizer does), so no extension is needed on managed services.
KNOWLEDGE_INDEX = "\n".join((
    "CREATE TABLE knowledge_index (title TEXT NOT NULL, body TEXT NOT NULL, tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, tsv TSVECTOR NOT NULL);",
    "CREATE INDEX knowledge_index_tsv ON knowledge_index USING GIN (tsv);",
    "CREATE INDEX knowledge_index_doc ON knowledge_index (doc_id)"))


def translate_statement(statement):
    body = re.sub(r"--[^\n]*", "", statement).strip()
    if not body:
        return statement
    if re.match(r"CREATE\s+VIRTUAL\s+TABLE", body, re.I):
        if "knowledge_index" not in body:
            raise Untranslatable("tabela virtual desconhecida: " + body[:80])
        return KNOWLEDGE_INDEX
    if re.match(r"CREATE\s+TRIGGER", body, re.I):
        return trigger(statement)
    dropped = re.match(r"DROP\s+TRIGGER\s+(\w+)$", body, re.I)
    if dropped:
        name = dropped.group(1)
        if name not in TRIGGER_TABLES:
            raise Untranslatable("DROP TRIGGER de um gatilho desconhecido: " + name)
        return f"DROP TRIGGER {name} ON {TRIGGER_TABLES.pop(name)};\nDROP FUNCTION {name}_fn()"
    if re.search(r"fts5|PRAGMA|sqlite_|\browid\b|strftime|INSERT\s+OR\s", body, re.I):
        raise Untranslatable("construcao so do SQLite: " + body[:80])
    out = statement
    out = re.sub(r"TEXT NOT NULL DEFAULT '\{\}' CHECK \(json_valid\((\w+)\)\)", "JSONB NOT NULL DEFAULT '{}'::jsonb", out)
    out = re.sub(r"TEXT NOT NULL CHECK \(json_valid\((\w+)\)\)", "JSONB NOT NULL", out)
    out = re.sub(r"TEXT CHECK \((\w+) IS NULL OR json_valid\(\1\)\)", "JSONB", out)
    out = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", out)
    out = re.sub(r"\bINTEGER\b", "BIGINT", out)
    out = re.sub(r"json_extract\(\s*(\w+)\s*,\s*'\$\.(\w+)'\s*\)", r"(\1->>'\2')", out)
    # foreign keys can be deferred inside one transaction (SET CONSTRAINTS ALL DEFERRED), as SQLite's defer_foreign_keys: erasing a case
    out = re.sub(r"(REFERENCES\s+\w+\s*\([^)]*\))", r"\1 DEFERRABLE INITIALLY IMMEDIATE", out)
    if "json_valid" in out or "json_extract" in out:
        raise Untranslatable("json do SQLite sem traducao: " + body[:80])
    # the queue orders ties by insertion; SQLite has rowid, PostgreSQL needs a column
    out = re.sub(r"(CREATE TABLE jobs \(\n  id TEXT PRIMARY KEY,)", r"\1\n  seq BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE,", out)
    return out


def translate(name, sql):
    return HEADER.format(name=name) + "\n".join(translate_statement(s).rstrip().rstrip(";") + ("" if s.lstrip().startswith("--") and not re.sub(r"--[^\n]*", "", s).strip() else ";")
                                                 for s in logical_statements(sql)) + "\n"


def build():
    TRIGGER_TABLES.clear()
    return {path.name: translate(path.name, path.read_text(encoding="utf-8")) for path in sorted(SOURCE.glob("*.sql"))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    generated = build()
    if args.check:
        stale = [name for name, text in generated.items() if not (TARGET / name).exists() or (TARGET / name).read_text(encoding="utf-8") != text]
        extra = [p.name for p in TARGET.glob("*.sql") if p.name not in generated] if TARGET.exists() else []
        if stale or extra:
            print("Desatualizado:", ", ".join(stale + extra), file=sys.stderr)
            return 1
        return 0
    TARGET.mkdir(exist_ok=True)
    for name, text in generated.items():
        (TARGET / name).write_text(text, encoding="utf-8", newline="\n")
    for old in TARGET.glob("*.sql"):
        if old.name not in generated:
            old.unlink()
    print(f"{len(generated)} migracoes em {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
