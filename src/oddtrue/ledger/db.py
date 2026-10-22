"""Postgres connection and forward-only migrations with checksum verification."""

from __future__ import annotations

from hashlib import sha256
from importlib import resources

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

_MIGRATION_LOCK = 7_240_001  # advisory lock key: two instances starting together must not both migrate


class MigrationError(Exception):
    pass


async def connect(dsn: str) -> AsyncConnection[DictRow]:
    return await AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)


def _migration_files() -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("oddtrue.ledger").joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


async def migrate(conn: AsyncConnection[DictRow]) -> list[int]:
    """Apply pending migrations in order, each in one transaction. Returns the versions applied.

    A migration that was already applied but whose file has since changed is an error: never edit applied
    migrations, add a new one.
    """
    done: list[int] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
        applied = {row["version"]: row["checksum"] for row in await cur.fetchall()}
        for version, name, sql in _migration_files():
            checksum = sha256(sql.encode()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError(f"migration {name} was modified after being applied")
                continue
            await conn.execute(sql.encode())
            await conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (version, checksum))
            done.append(version)
    return done
