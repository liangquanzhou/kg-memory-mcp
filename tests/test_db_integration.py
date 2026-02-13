"""Integration tests: KG CRUD (db.py) — requires PostgreSQL."""

import pytest

from kg_memory_mcp.db import (
    add_observations,
    create_entities,
    create_relations,
    delete_entities,
    read_graph,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("clean_tables"),
]


async def test_create_entities():
    results = await create_entities([
        {"name": "Python", "entityType": "language", "description": "A programming language"},
        {"name": "Rust", "entityType": "language"},
    ])
    assert len(results) == 2
    assert results[0]["name"] == "Python"
    assert results[1]["name"] == "Rust"
    assert all("id" in r for r in results)


async def test_create_entities_upsert():
    await create_entities([{"name": "Node", "entityType": "runtime", "description": "v18"}])
    results = await create_entities([{"name": "Node", "entityType": "runtime", "description": "v20"}])

    assert len(results) == 1
    assert results[0]["name"] == "Node"

    # Verify description updated via ON CONFLICT UPDATE
    graph = await read_graph()
    assert graph["entities"][0]["description"] == "v20"


async def test_add_observations():
    await create_entities([{"name": "Go", "entityType": "language"}])
    added = await add_observations("Go", ["Compiled language", "Has goroutines"])

    assert len(added) == 2
    assert "Compiled language" in added

    # Verify persisted via read_graph (also exercises json_agg → list parsing)
    graph = await read_graph()
    obs = graph["entities"][0]["observations"]
    assert isinstance(obs, list)
    assert "Compiled language" in obs
    assert "Has goroutines" in obs


async def test_add_observations_dedup_hash():
    await create_entities([{"name": "Zig", "entityType": "language"}])
    added1 = await add_observations("Zig", ["Systems programming language"])
    added2 = await add_observations("Zig", ["Systems programming language"])  # duplicate

    assert len(added1) == 1
    assert len(added2) == 0  # rejected by hash dedup


async def test_add_observations_sensitive_filter():
    await create_entities([{"name": "Config", "entityType": "config"}])
    added = await add_observations("Config", [
        "Normal setting",
        "password=SuperSecret123",  # should be filtered
    ])

    assert added == ["Normal setting"]


async def test_create_relations(db_pool):
    await create_entities([
        {"name": "Django", "entityType": "framework"},
        {"name": "Python", "entityType": "language"},
    ])
    created = await create_relations([
        {"from": "Django", "to": "Python", "relationType": "built_with"},
    ])
    assert len(created) == 1

    # Verify via read_graph
    graph = await read_graph()
    assert len(graph["relations"]) == 1
    assert graph["relations"][0]["from"] == "Django"
    assert graph["relations"][0]["relationType"] == "built_with"

    # Duplicate relation → ON CONFLICT DO NOTHING (no DB error, no new row)
    await create_relations([
        {"from": "Django", "to": "Python", "relationType": "built_with"},
    ])
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM kg_relations")
    assert count == 1


async def test_delete_entities_cascade(db_pool):
    await create_entities([
        {"name": "Flask", "entityType": "framework"},
        {"name": "Python", "entityType": "language"},
    ])
    await add_observations("Flask", ["Micro framework"])
    await create_relations([
        {"from": "Flask", "to": "Python", "relationType": "built_with"},
    ])

    deleted = await delete_entities(["Flask"])
    assert deleted == ["Flask"]

    # Cascade should have removed observations and relations
    async with db_pool.acquire() as conn:
        obs = await conn.fetchval("SELECT COUNT(*) FROM kg_observations")
        rels = await conn.fetchval("SELECT COUNT(*) FROM kg_relations")
    assert obs == 0
    assert rels == 0


async def test_read_graph_pagination():
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    await create_entities([
        {"name": n, "entityType": "test"} for n in names
    ])
    # Relation spanning pages: Alpha (page 1) → Charlie (page 2)
    await create_relations([
        {"from": "Alpha", "to": "Charlie", "relationType": "linked"},
    ])

    page1 = await read_graph(limit=2, offset=0)
    assert page1["total"] == 5
    assert len(page1["entities"]) == 2
    assert page1["entities"][0]["name"] == "Alpha"   # ORDER BY name
    assert page1["entities"][1]["name"] == "Bravo"

    # Alpha→Charlie relation: Charlie not on this page → cross_page
    cross = [r for r in page1["relations"] if r.get("cross_page")]
    assert len(cross) == 1
    assert cross[0]["from"] == "Alpha"
    assert cross[0]["to"] == "Charlie"

    # Last page
    page3 = await read_graph(limit=2, offset=4)
    assert len(page3["entities"]) == 1
    assert page3["entities"][0]["name"] == "Echo"
