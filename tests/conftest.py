"""Shared test fixtures for kg-memory-mcp (unit + integration)."""

import getpass
import hashlib
import os
import socket
from pathlib import Path

import asyncpg
import numpy as np
import pytest
import pytest_asyncio
from pgvector.asyncpg import register_vector

# Point all tests at the test database
os.environ.setdefault("KG_DB_NAME", "knowledge_base_test")
os.environ.setdefault("KG_DB_USER", getpass.getuser())

TEST_DB = "knowledge_base_test"
SCHEMA_SQL = Path(__file__).parent.parent / "kg_memory_mcp" / "schema.sql"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pg_reachable() -> bool:
    """TCP probe — is PostgreSQL listening?"""
    try:
        s = socket.create_connection(
            (os.getenv("KG_DB_HOST", "localhost"),
             int(os.getenv("KG_DB_PORT", "5432"))),
            timeout=3,
        )
        s.close()
        return True
    except OSError:
        return False


def _dsn(db: str = "postgres") -> dict:
    d = dict(
        database=db,
        user=os.getenv("KG_DB_USER", getpass.getuser()),
        host=os.getenv("KG_DB_HOST", "localhost"),
        port=int(os.getenv("KG_DB_PORT", "5432")),
    )
    pw = os.getenv("KG_DB_PASSWORD")
    if pw:
        d["password"] = pw
    return d


def _deterministic_vector(text: str) -> np.ndarray:
    """Deterministic 1024-dim unit vector seeded by text hash."""
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32)
    rng = np.random.RandomState(seed)
    v = rng.randn(1024).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


_PG_OK = _pg_reachable()


# ------------------------------------------------------------------
# Embedding mock (autouse — harmless for unit tests)
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_embedding(monkeypatch):
    """Replace Ollama calls with deterministic vectors."""

    async def _one(text: str) -> np.ndarray:
        return _deterministic_vector(text)

    async def _many(texts: list[str]) -> list[np.ndarray]:
        return [_deterministic_vector(t) for t in texts]

    monkeypatch.setattr("kg_memory_mcp.db.get_embedding", _one)
    monkeypatch.setattr("kg_memory_mcp.db.get_embeddings", _many)
    monkeypatch.setattr("kg_memory_mcp.search.get_embedding", _one)


# ------------------------------------------------------------------
# Integration fixtures (require PostgreSQL)
# ------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool():
    """Create test DB ➜ apply schema ➜ yield pool ➜ drop test DB."""
    if not _PG_OK:
        pytest.skip("PostgreSQL not available")

    try:
        admin = await asyncpg.connect(**_dsn(), timeout=5)
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError, OSError) as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")

    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{TEST_DB}' AND pid <> pg_backend_pid()"
        )
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        await admin.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await admin.close()

    # Apply schema BEFORE creating pool (register_vector needs the extension)
    setup_conn = await asyncpg.connect(**_dsn(TEST_DB), timeout=10)
    try:
        await setup_conn.execute(SCHEMA_SQL.read_text())
    finally:
        await setup_conn.close()

    pool = await asyncpg.create_pool(
        **_dsn(TEST_DB), min_size=2, max_size=5, init=register_vector,
    )

    # Inject pool into the db module so all production code uses it
    import kg_memory_mcp.db as _db

    saved_pool = _db._pool
    _db._pool = pool

    yield pool

    # Teardown
    _db._pool = saved_pool
    await pool.close()

    admin = await asyncpg.connect(**_dsn(), timeout=5)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{TEST_DB}' AND pid <> pg_backend_pid()"
        )
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    finally:
        await admin.close()


@pytest_asyncio.fixture(loop_scope="session")
async def clean_tables(db_pool):
    """TRUNCATE all data tables + reset sequences before each test."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE kg_entities, kg_observations, kg_relations, "
            "chat_sessions, chat_messages, chat_attachments "
            "RESTART IDENTITY CASCADE"
        )
