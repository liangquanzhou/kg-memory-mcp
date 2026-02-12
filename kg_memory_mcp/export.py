"""数据导出：JSONL + SQLite 格式"""

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .db import get_pool


def _json_default(obj):
    """JSON serializer for types not handled by default."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _write_jsonl(filepath: Path, rows: list[dict]) -> int:
    with open(filepath, "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=_json_default, ensure_ascii=False) + "\n")
    return len(rows)


async def export_jsonl(output_dir: str) -> dict:
    """Export all data to JSONL files. Returns counts dict.

    Uses a repeatable-read transaction to get a consistent snapshot across tables.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pool = await get_pool()
    conn = await pool.acquire()
    tr = conn.transaction(isolation="repeatable_read")
    await tr.start()
    counts = {}

    # --- Knowledge Graph ---
    rows = await conn.fetch(
        "SELECT id, name, entity_type, description, meta, created_at, updated_at FROM kg_entities ORDER BY id"
    )
    entities = []
    entity_id_to_name = {}
    for r in rows:
        entity_id_to_name[r["id"]] = r["name"]
        entities.append({
            "name": r["name"], "entity_type": r["entity_type"],
            "description": r["description"],
            "meta": json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {}),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        })
    counts["kg_entities"] = _write_jsonl(out / "kg_entities.jsonl", entities)

    rows = await conn.fetch(
        "SELECT o.id, o.entity_id, o.content, o.source_agent, o.created_at "
        "FROM kg_observations o ORDER BY o.id"
    )
    observations = []
    for r in rows:
        observations.append({
            "entity_name": entity_id_to_name.get(r["entity_id"], ""),
            "content": r["content"], "source_agent": r["source_agent"],
            "created_at": r["created_at"],
        })
    counts["kg_observations"] = _write_jsonl(out / "kg_observations.jsonl", observations)

    rows = await conn.fetch(
        "SELECT r.from_entity_id, r.to_entity_id, r.relation_type, r.created_at "
        "FROM kg_relations r ORDER BY r.id"
    )
    relations = []
    for r in rows:
        relations.append({
            "from_entity": entity_id_to_name.get(r["from_entity_id"], ""),
            "to_entity": entity_id_to_name.get(r["to_entity_id"], ""),
            "relation_type": r["relation_type"], "created_at": r["created_at"],
        })
    counts["kg_relations"] = _write_jsonl(out / "kg_relations.jsonl", relations)

    # --- Chat Archival ---
    rows = await conn.fetch(
        "SELECT id, agent, native_session_id, project_dir, model, message_count, "
        "started_at, ended_at, meta, created_at FROM chat_sessions ORDER BY id"
    )
    session_id_to_native = {}
    sessions = []
    for r in rows:
        session_id_to_native[r["id"]] = r["native_session_id"]
        sessions.append({
            "agent": r["agent"], "native_session_id": r["native_session_id"],
            "project_dir": r["project_dir"], "model": r["model"],
            "message_count": r["message_count"],
            "started_at": r["started_at"], "ended_at": r["ended_at"],
            "meta": json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {}),
            "created_at": r["created_at"],
        })
    counts["chat_sessions"] = _write_jsonl(out / "chat_sessions.jsonl", sessions)

    rows = await conn.fetch(
        "SELECT m.id, m.session_id, m.role, m.content, m.meta, m.created_at "
        "FROM chat_messages m ORDER BY m.id"
    )
    # Preload attachments
    att_rows = await conn.fetch(
        "SELECT message_id, file_path, file_type, file_size FROM chat_attachments ORDER BY id"
    )
    att_by_msg: dict[int, list[dict]] = {}
    for a in att_rows:
        att_by_msg.setdefault(a["message_id"], []).append({
            "file_path": a["file_path"], "file_type": a["file_type"], "file_size": a["file_size"],
        })

    messages = []
    for r in rows:
        msg = {
            "native_session_id": session_id_to_native.get(r["session_id"], ""),
            "role": r["role"], "content": r["content"],
            "meta": json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {}),
            "created_at": r["created_at"],
        }
        atts = att_by_msg.get(r["id"])
        if atts:
            msg["attachments"] = atts
        messages.append(msg)
    counts["chat_messages"] = _write_jsonl(out / "chat_messages.jsonl", messages)

    # Copy attachment files
    att_export_dir = out / "attachments"
    copied_files = 0
    for a in att_rows:
        src = Path(a["file_path"])
        if src.exists():
            # Preserve session_id/filename structure
            rel = src.name
            parent = src.parent.name
            dest = att_export_dir / parent / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
                copied_files += 1
    counts["attachment_files"] = copied_files

    # Metadata
    meta = {
        "version": "0.1.0",
        "exported_at": datetime.now(UTC).isoformat(),
        "counts": counts,
    }
    with open(out / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
        f.write("\n")

    await tr.commit()
    await pool.release(conn)
    return counts


async def export_sqlite(output_path: str) -> dict:
    """Export all data to a single SQLite database. Returns counts dict.

    Uses a repeatable-read transaction to get a consistent snapshot across tables.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        os.remove(out)

    pool = await get_pool()
    pg_conn = await pool.acquire()
    pg_tr = pg_conn.transaction(isolation="repeatable_read")
    await pg_tr.start()
    conn = sqlite3.connect(str(out))
    cur = conn.cursor()
    counts = {}

    # Create tables (PostgreSQL-compatible minus VECTOR/GENERATED columns)
    cur.executescript("""
        CREATE TABLE schema_version (
            version     INTEGER PRIMARY KEY,
            applied     TEXT DEFAULT (datetime('now')),
            description TEXT
        );

        CREATE TABLE kg_entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            entity_type TEXT NOT NULL,
            description TEXT,
            embedding   TEXT,
            meta        TEXT DEFAULT '{}',
            created_at  TEXT,
            updated_at  TEXT
        );

        CREATE TABLE kg_observations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id     INTEGER REFERENCES kg_entities(id),
            content       TEXT NOT NULL,
            content_hash  TEXT,
            embedding     TEXT,
            source_agent  TEXT,
            ref_doc_id    INTEGER,
            created_at    TEXT
        );

        CREATE TABLE kg_relations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity_id  INTEGER REFERENCES kg_entities(id),
            to_entity_id    INTEGER REFERENCES kg_entities(id),
            relation_type   TEXT NOT NULL,
            created_at      TEXT,
            UNIQUE(from_entity_id, to_entity_id, relation_type)
        );

        CREATE TABLE chat_sessions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            agent             TEXT NOT NULL,
            native_session_id TEXT,
            project_dir       TEXT,
            model             TEXT,
            message_count     INTEGER DEFAULT 0,
            started_at        TEXT,
            ended_at          TEXT,
            meta              TEXT DEFAULT '{}',
            created_at        TEXT,
            UNIQUE(agent, native_session_id)
        );

        CREATE TABLE chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER REFERENCES chat_sessions(id),
            role        TEXT NOT NULL,
            content     TEXT,
            meta        TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE chat_attachments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  INTEGER REFERENCES chat_messages(id),
            file_path   TEXT NOT NULL,
            file_type   TEXT,
            file_size   INTEGER,
            created_at  TEXT
        );
    """)

    # schema_version
    sv_rows = await pg_conn.fetch("SELECT version, applied, description FROM schema_version ORDER BY version")
    for r in sv_rows:
        cur.execute("INSERT INTO schema_version VALUES (?, ?, ?)",
                    (r["version"], r["applied"].isoformat() if r["applied"] else None, r["description"]))

    def _ts(val):
        return val.isoformat() if val else None

    def _emb(val):
        if val is None:
            return None
        # pgvector returns string like '[0.1,0.2,...]' or numpy array
        if isinstance(val, str):
            return val
        return json.dumps([float(x) for x in val])

    def _meta(val):
        if isinstance(val, str):
            return val
        return json.dumps(val or {}, ensure_ascii=False)

    # kg_entities
    rows = await pg_conn.fetch("SELECT * FROM kg_entities ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO kg_entities (id, name, entity_type, description, embedding, meta, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["name"], r["entity_type"], r["description"], _emb(r["embedding"]),
             _meta(r["meta"]), _ts(r["created_at"]), _ts(r["updated_at"])),
        )
    counts["kg_entities"] = len(rows)

    # kg_observations
    rows = await pg_conn.fetch("SELECT * FROM kg_observations ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO kg_observations "
            "(id, entity_id, content, content_hash, embedding, source_agent, ref_doc_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["entity_id"], r["content"], r["content_hash"], _emb(r["embedding"]),
             r["source_agent"], r["ref_doc_id"], _ts(r["created_at"])),
        )
    counts["kg_observations"] = len(rows)

    # kg_relations
    rows = await pg_conn.fetch("SELECT * FROM kg_relations ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO kg_relations (id, from_entity_id, to_entity_id, relation_type, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["id"], r["from_entity_id"], r["to_entity_id"], r["relation_type"], _ts(r["created_at"])),
        )
    counts["kg_relations"] = len(rows)

    # chat_sessions
    rows = await pg_conn.fetch("SELECT * FROM chat_sessions ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO chat_sessions "
            "(id, agent, native_session_id, project_dir, model, message_count, "
            "started_at, ended_at, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["agent"], r["native_session_id"], r["project_dir"], r["model"],
             r["message_count"], _ts(r["started_at"]), _ts(r["ended_at"]),
             _meta(r["meta"]), _ts(r["created_at"])),
        )
    counts["chat_sessions"] = len(rows)

    # chat_messages
    rows = await pg_conn.fetch("SELECT id, session_id, role, content, meta, created_at FROM chat_messages ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], r["session_id"], r["role"], r["content"],
             _meta(r["meta"]), _ts(r["created_at"])),
        )
    counts["chat_messages"] = len(rows)

    # chat_attachments
    rows = await pg_conn.fetch("SELECT * FROM chat_attachments ORDER BY id")
    for r in rows:
        cur.execute(
            "INSERT INTO chat_attachments (id, message_id, file_path, file_type, file_size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], r["message_id"], r["file_path"], r["file_type"],
             r["file_size"], _ts(r["created_at"])),
        )
    counts["chat_attachments"] = len(rows)

    conn.commit()
    conn.close()
    await pg_tr.commit()
    await pool.release(pg_conn)
    return counts
