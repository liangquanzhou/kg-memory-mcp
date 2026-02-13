"""Integration tests: Chat archive (chat_db.py) — requires PostgreSQL."""

from datetime import datetime, timedelta, timezone

import pytest

from kg_memory_mcp.chat_db import (
    get_session,
    insert_messages,
    list_sessions,
    search_chats,
    upsert_session,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("clean_tables"),
]

_UTC = timezone.utc


async def test_upsert_session():
    # Create
    sid = await upsert_session(
        "claude", "sess-001", project_dir="/tmp/proj", model="opus",
    )
    assert sid > 0

    # Upsert same (agent, native_session_id) → same row, update fields
    sid2 = await upsert_session("claude", "sess-001", model="sonnet")
    assert sid2 == sid

    session = await get_session(session_id=sid)
    assert session["model"] == "sonnet"        # updated
    assert session["project_dir"] == "/tmp/proj"  # preserved by COALESCE


async def test_insert_messages_basic():
    sid = await upsert_session("test", "basic-msgs")
    t1 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=_UTC)
    t2 = datetime(2024, 1, 1, 10, 0, 1, tzinfo=_UTC)

    msgs = [
        {"role": "user", "content": "Hello world", "created_at": t1},
        {"role": "assistant", "content": "Hi!", "created_at": t2},
    ]
    ids, new = await insert_messages(sid, msgs)

    assert len(ids) == 2
    assert all(i > 0 for i in ids)
    assert len(new) == 2
    assert new[0]["role"] == "user"


async def test_insert_messages_watermark():
    """Batch 1: 3 msgs (t1-t3). Batch 2: 5 msgs (t1-t5, first 3 overlap).
    Only 2 new messages should be inserted."""
    sid = await upsert_session("test", "watermark")
    base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=_UTC)

    batch1 = [
        {"role": "user", "content": f"msg-{i}", "created_at": base + timedelta(seconds=i)}
        for i in range(3)
    ]
    ids1, new1 = await insert_messages(sid, batch1)
    assert len(new1) == 3

    batch2 = [
        {"role": "user", "content": f"msg-{i}", "created_at": base + timedelta(seconds=i)}
        for i in range(5)
    ]
    ids2, new2 = await insert_messages(sid, batch2)

    # t0, t1 < last_ts(=t2) → filtered; t2 == last_ts + same content → filtered
    # t3, t4 > last_ts → inserted
    assert len(new2) == 2
    assert new2[0]["content"] == "msg-3"
    assert new2[1]["content"] == "msg-4"


async def test_insert_messages_same_timestamp():
    """Multiple messages at the exact same second — no false dedup."""
    sid = await upsert_session("test", "same-ts")
    t = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)

    batch1 = [
        {"role": "user", "content": "Hello", "created_at": t},
        {"role": "assistant", "content": "Hi there", "created_at": t},
        {"role": "user", "content": "How are you?", "created_at": t},
    ]
    ids1, new1 = await insert_messages(sid, batch1)
    assert len(new1) == 3

    # Re-send same 3 + 1 new, all at same timestamp
    batch2 = batch1 + [
        {"role": "assistant", "content": "I'm doing well", "created_at": t},
    ]
    ids2, new2 = await insert_messages(sid, batch2)

    assert len(new2) == 1
    assert new2[0]["content"] == "I'm doing well"


async def test_search_chats():
    sid = await upsert_session("test", "search-test")
    t = datetime(2024, 3, 1, 9, 0, 0, tzinfo=_UTC)

    await insert_messages(sid, [
        {"role": "user", "content": "Tell me about PostgreSQL indexing strategies", "created_at": t},
        {"role": "assistant", "content": "GIN and GiST are common index types", "created_at": t + timedelta(seconds=1)},
        {"role": "user", "content": "What about weather today?", "created_at": t + timedelta(seconds=2)},
    ])

    results = await search_chats("PostgreSQL indexing")
    assert len(results) >= 1
    contents = [r["content"] for r in results]
    assert any("PostgreSQL" in c for c in contents)


async def test_list_sessions():
    for i in range(5):
        await upsert_session("test", f"list-{i}", model=f"model-{i}")

    page1 = await list_sessions(agent="test", limit=2, offset=0)
    assert len(page1) == 2

    page2 = await list_sessions(agent="test", limit=2, offset=2)
    assert len(page2) == 2

    page3 = await list_sessions(agent="test", limit=2, offset=4)
    assert len(page3) == 1

    # All sessions returned
    all_ids = {s["id"] for s in page1 + page2 + page3}
    assert len(all_ids) == 5
