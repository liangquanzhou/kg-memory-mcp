#!/usr/bin/env python3
"""Gemini CLI SessionEnd Hook: 快速存档 + fork 后台知识提取

触发: Gemini CLI 会话结束时
数据传递: JSON via stdin
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("kg-memory-hook-gemini")

GEMINI_TMP_DIR = Path.home() / ".gemini" / "tmp"
CHAT_SANITIZE = os.environ.get("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "gemini-session-end.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file), level=logging.INFO,
        format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ── Session discovery ───────────────────────────────────────

def _find_session_by_id(session_id: str) -> Path | None:
    if not session_id:
        return None
    for chats_dir in GEMINI_TMP_DIR.glob("*/chats"):
        target = chats_dir / f"session-{session_id}.json"
        if target.exists() and target.stat().st_size > 500:
            return target
    return None


def _find_latest_session() -> Path | None:
    sessions = []
    for chats_dir in GEMINI_TMP_DIR.glob("*/chats"):
        for json_file in chats_dir.glob("session-*.json"):
            if json_file.stat().st_size > 500:
                sessions.append(json_file)
    if not sessions:
        return None
    return max(sessions, key=lambda x: x.stat().st_mtime)


def _parse_session(path: Path) -> tuple[list[dict], str]:
    messages = []
    session_id = ""
    try:
        with open(path) as f:
            session = json.load(f)
        session_id = session.get("sessionId", path.stem)
        for msg in session.get("messages", []):
            msg_type = msg.get("type", "unknown")
            content = msg.get("content", "")
            ts = msg.get("timestamp", datetime.now().isoformat())
            if msg_type in ("user", "gemini") and content:
                messages.append({
                    "role": "user" if msg_type == "user" else "assistant",
                    "content": content[:50000],
                    "meta": {},
                    "created_at": ts,
                })
    except Exception as e:
        log.warning(f"Parse error: {e}")
    return messages, session_id


# ── Archive (fast, <1s) ─────────────────────────────────────

async def _archive_phase(messages: list[dict], session_id: str):
    from ._common import get_conn
    conn = await get_conn()
    try:
        timestamps = [m["created_at"] for m in messages if m.get("created_at")]
        started_at = timestamps[0] if timestamps else None
        ended_at = timestamps[-1] if timestamps else None

        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (agent, native_session_id, started_at, ended_at, meta)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (agent, native_session_id) DO UPDATE SET
                ended_at = COALESCE(EXCLUDED.ended_at, chat_sessions.ended_at)
            RETURNING id
            """,
            "gemini-cli", session_id, started_at, ended_at, json.dumps({}),
        )
        assert row is not None
        db_session_id = row["id"]

        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1", db_session_id
        )
        if (existing or 0) > 0:
            log.info(f"Session {session_id} already archived")
            return

        for m in messages:
            content = m["content"] or ""
            if CHAT_SANITIZE and content:
                from ..quality import contains_sensitive
                if contains_sensitive(content):
                    continue
            await conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, meta, created_at) VALUES ($1, $2, $3, $4, $5)",
                db_session_id, m["role"], content, json.dumps(m.get("meta", {})), m["created_at"],
            )

        await conn.execute(
            "UPDATE chat_sessions SET message_count = (SELECT COUNT(*) FROM chat_messages WHERE session_id = $1) WHERE id = $1",
            db_session_id,
        )
        log.info(f"Archived {len(messages)} messages for gemini session {session_id}")
    finally:
        await conn.close()


# ── Entry point ─────────────────────────────────────────────

def main():
    _setup_logging()

    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return

    reason = hook_input.get("reason", "unknown")
    log.info(f"SessionEnd triggered: reason={reason}")

    sid = hook_input.get("session_id", "")
    session_path = _find_session_by_id(sid) if sid else None
    if session_path is None:
        if sid:
            log.warning(f"Session file not found for id={sid[:12]}..., falling back to latest")
        session_path = _find_latest_session()
    if not session_path:
        log.info("No session file found")
        return

    log.info(f"Processing: {session_path.name}")
    messages, session_id = _parse_session(session_path)
    if not messages:
        log.info("No messages found")
        return

    # Phase 1: fast archive
    try:
        asyncio.run(_archive_phase(messages, session_id))
    except Exception as e:
        log.error(f"Archive failed: {e}")
        return

    # Phase 2: fork background extraction
    from ._common import fork_extraction
    project_hash = session_path.parent.parent.name[:8]
    source_label = f"{project_hash}/{session_path.stem}"
    fork_extraction(messages, agent="gemini-cli", source_label=source_label)


if __name__ == "__main__":
    main()
