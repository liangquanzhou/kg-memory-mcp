"""Integration tests: search (search.py) — requires PostgreSQL."""

import pytest

from kg_memory_mcp.db import add_observations, create_entities
from kg_memory_mcp.search import search

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("clean_tables"),
]


async def _seed_entity(name: str, observations: list[str]):
    """Helper: create entity + add observations."""
    await create_entities([{"name": name, "entityType": "test"}])
    await add_observations(name, observations)


async def test_search_fts():
    """FTS should match on observation content words."""
    await _seed_entity("Python", ["Python is a versatile programming language"])
    await _seed_entity("JavaScript", ["JavaScript runs in the browser"])

    result = await search("programming language")

    names = [e["name"] for e in result["entities"]]
    assert "Python" in names  # "programming language" appears in Python's observation


async def test_search_vector():
    """Exact same text as query ➜ identical mock vector ➜ cosine similarity = 1."""
    phrase = "unique vector search test phrase xyz"
    await _seed_entity("VecTest", [phrase])

    result = await search(phrase)

    names = [e["name"] for e in result["entities"]]
    assert "VecTest" in names


async def test_search_pagination():
    """limit should cap the number of returned entities (no relations ➜ no 1-hop expansion)."""
    for i in range(3):
        await _seed_entity(f"Item{i}", [f"pagination search content item {i}"])

    result = await search("pagination search content", limit=1)

    assert len(result["entities"]) == 1
