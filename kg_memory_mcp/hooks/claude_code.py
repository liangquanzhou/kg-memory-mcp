#!/usr/bin/env python3
"""Claude Code SessionEnd Hook: 快速存档 + fork 后台知识提取

触发: Claude Code 会话结束时
数据传递: JSON via stdin
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("kg-memory-hook-claude")

CHAT_SANITIZE = os.environ.get("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")
MIN_TURNS = 3


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "auto-memory.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file), level=logging.INFO,
        format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ── Transcript parsing ──────────────────────────────────────

def _read_transcript(path: str) -> list:
    messages = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    messages.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning(f"Error reading transcript: {e}")
    return messages


def _normalize_messages(messages: list) -> list[dict]:
    """Convert Claude Code JSONL messages to normalized {role, content, created_at} format."""
    normalized = []
    for m in messages:
        ts_str = m.get("timestamp")
        if not ts_str:
            continue

        msg_type = m.get("type", "")
        content = m.get("content", "")

        if msg_type == "user":
            role = "user"
            if isinstance(content, list):
                texts = [
                    p.get("text", "") if isinstance(p, dict) and p.get("type") == "text"
                    else (p if isinstance(p, str) else "")
                    for p in content
                ]
                content = "\n".join(t for t in texts if t)
            elif not isinstance(content, str):
                content = str(content)
        elif msg_type == "assistant":
            role = "assistant"
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
        else:
            continue

        if not content or not content.strip():
            continue

        normalized.append({
            "role": role,
            "content": content[:50000],
            "meta": {},
            "created_at": ts_str.replace("Z", "+00:00"),
        })
    return normalized


# ── Archive (fast, <1s) ─────────────────────────────────────

async def _archive_phase(messages: list[dict], session_id: str, cwd: str):
    from ._common import get_conn
    conn = await get_conn()
    try:
        timestamps = [m["created_at"] for m in messages]
        started_at = timestamps[0] if timestamps else None
        ended_at = timestamps[-1] if timestamps else None

        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (agent, native_session_id, project_dir, started_at, ended_at, meta)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (agent, native_session_id) DO UPDATE SET
                ended_at = COALESCE(EXCLUDED.ended_at, chat_sessions.ended_at)
            RETURNING id
            """,
            "claude-code", session_id, cwd, started_at, ended_at, json.dumps({}),
        )
        assert row is not None
        db_session_id = row["id"]

        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1", db_session_id
        )
        if (existing or 0) > 0:
            log.info(f"Session {session_id} already archived, skipping messages")
            return

        count = 0
        for m in messages:
            content = m["content"]
            if CHAT_SANITIZE and content:
                from ..quality import contains_sensitive
                if contains_sensitive(content):
                    continue
            await conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, meta, created_at) VALUES ($1, $2, $3, $4, $5)",
                db_session_id, m["role"], content, json.dumps(m.get("meta", {})), m["created_at"],
            )
            count += 1

        await conn.execute(
            "UPDATE chat_sessions SET message_count = (SELECT COUNT(*) FROM chat_messages WHERE session_id = $1) WHERE id = $1",
            db_session_id,
        )
        log.info(f"Archived {count} messages for session {session_id}")
    finally:
        await conn.close()


# ── Entry point ─────────────────────────────────────────────

def main():
    _setup_logging()

    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return

    transcript_path = hook_input.get("transcript_path")
    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", "")
    reason = hook_input.get("reason", "")

    log.info(f"SessionEnd triggered: session={session_id[:12]}..., reason={reason}")

    if not transcript_path or not Path(transcript_path).exists():
        log.info("No transcript file, skipping")
        return

    # Path safety check
    resolved = Path(transcript_path).resolve()
    allowed_prefixes = [
        Path.home() / ".claude",
        Path("/tmp").resolve(),
    ]
    if not any(resolved.is_relative_to(p) for p in allowed_prefixes):
        log.warning("Transcript path outside allowed directories")
        return

    raw_messages = _read_transcript(str(resolved))
    messages = _normalize_messages(raw_messages)
    user_turns = sum(1 for m in messages if m["role"] == "user")

    if not messages:
        log.info("No messages found")
        return

    # Phase 1: fast archive
    try:
        asyncio.run(_archive_phase(messages, session_id, cwd))
    except Exception as e:
        log.error(f"Archive failed: {e}")
        return

    # Phase 2: fork background extraction
    if user_turns < MIN_TURNS:
        log.info(f"Too few turns ({user_turns}), skipping extraction")
        return

    from ._common import fork_extraction
    fork_extraction(messages, agent="claude-code", source_label=cwd, project_dir=cwd)


if __name__ == "__main__":
    main()
