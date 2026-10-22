"""SQLite connection and forward-only migrations with checksum verification."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources


class MigrationError(Exception):
    pass


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migration_files() -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("oddtrue.ledger").joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order, each in one transaction. Returns the versions applied.

    A migration that was already applied but whose file has since changed is an error: never edit applied
    migrations, add a new one.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"]: row["checksum"] for row in conn.execute("SELECT version, checksum FROM schema_migrations")}
    done: list[int] = []
    for version, name, sql in _migration_files():
        checksum = sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise MigrationError(f"migration {name} was modified after being applied")
            continue
        conn.execute("BEGIN")
        try:
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                (version, checksum, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done.append(version)
    return done


def _split(sql: str) -> list[str]:
    """Split a script into statements; triggers contain ';' inside BEGIN..END so they are kept whole."""
    statements: list[str] = []
    current: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        ends = stripped.upper().endswith("END;") if in_trigger else stripped.endswith(";")
        if ends:
            statements.append("\n".join(current))
            current, in_trigger = [], False
    if current:
        raise MigrationError("unterminated statement in migration")
    return statements
