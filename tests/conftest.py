from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from oddtrue.ledger import db
from oddtrue.ledger.repository import Ledger

if TYPE_CHECKING:
    from tests.world import World

# Throwaway local Postgres from docker-compose.yml (`docker compose up -d --wait`). CI sets the variable.
TEST_DSN = os.environ.get("ODDTRUE_TEST_DATABASE_URL", "postgresql://oddtrue:oddtrue_dev@127.0.0.1:55432/oddtrue")


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection[DictRow]]:
    """A fresh, migrated schema per test. Skips (not passes silently in CI) if Postgres is unreachable."""
    try:
        c = await db.connect(TEST_DSN)
    except Exception as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres not reachable ({type(exc).__name__}); run `docker compose up -d --wait`")
    await c.execute("DROP SCHEMA public CASCADE")
    await c.execute("CREATE SCHEMA public")
    await db.migrate(c)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def ledger(conn: AsyncConnection[DictRow]) -> Ledger:
    return Ledger(conn)


@pytest.fixture
async def world(ledger: Ledger) -> World:
    from tests.world import build_world

    return build_world(ledger)
