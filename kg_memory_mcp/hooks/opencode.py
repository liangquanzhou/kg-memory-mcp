#!/usr/bin/env python3
"""OpenCode session.idle hook: 增量存档对话到 PostgreSQL

触发事件: session.idle (AI 响应结束后)
调用方式: kg-memory-mcp hooks run opencode (由 TypeScript plugin 调用)
存储结构: ~/.local/share/opencode/storage/{session,message,part}/
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("kg-memory-hook-opencode")

CHAT_SANITIZE = os.environ.get("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")

STORAGE_DIR = Path(os.environ.get(
    "OPENCODE_STORAGE_DIR",
    os.path.expanduser("~/.local/share/opencode/storage"),
))


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "opencode-hook.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _get_conn() -> asyncpg.Connection:
    from ._common import get_conn
    return await get_conn()


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _find_latest_session() -> Path | None:
    """找到最近修改的 OpenCode 会话文件"""
    session_base = STORAGE_DIR / "session"
    if not session_base.is_dir():
        return None

    candidates: list[Path] = []
    for project_dir in session_base.iterdir():
        if project_dir.is_dir():
            for sf in project_dir.iterdir():
                if sf.suffix == ".json":
                    candidates.append(sf)

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_session(session_path: Path) -> dict | None:
    """解析 OpenCode 会话（session + messages + parts）"""
    session = _load_json(session_path)
    if not session:
        return None

    session_id = session.get("id", "")
    if not session_id:
        return None

    time_info = session.get("time", {})
    started_at = _ms_to_dt(time_info["created"]) if time_info.get("created") else None
    ended_at = _ms_to_dt(time_info["updated"]) if time_info.get("updated") else None

    msg_dir = STORAGE_DIR / "message" / session_id
    if not msg_dir.is_dir():
        return None

    part_base = STORAGE_DIR / "part"
    messages = []

    for msg_file in sorted(msg_dir.iterdir()):
        msg = _load_json(msg_file)
        if not msg:
            continue

        msg_id = msg.get("id", "")
        role = msg.get("role", "unknown")
        msg_time = msg.get("time", {})
        created = _ms_to_dt(msg_time["created"]) if msg_time.get("created") else started_at

        parts_dir = part_base / msg_id
        if not parts_dir.is_dir():
            continue

        texts = []
        for part_file in sorted(parts_dir.iterdir()):
            part = _load_json(part_file)
            if not part:
                continue
            ptype = part.get("type", "")
            if ptype == "text" and part.get("text"):
                texts.append(part["text"])
            elif ptype == "tool-use":
                name = part.get("name", "unknown")
                tool_input = str(part.get("input", ""))[:300]
                texts.append(f"[Tool: {name}({tool_input})]")
            elif ptype == "tool-result":
                result = part.get("result", "")
                if isinstance(result, str) and result.strip():
                    texts.append(f"[Result: {result[:500]}]")

        content = "\n".join(texts).strip()
        if not content:
            continue

        messages.append({
            "role": role,
            "content": content[:50000],
            "meta": {},
            "created_at": created,
        })

    if not messages:
        return None

    return {
        "agent": "opencode",
        "native_session_id": session_id,
        "project_dir": session.get("directory"),
        "model": None,
        "messages": messages,
        "started_at": started_at,
        "ended_at": ended_at,
        "meta": {
            "title": session.get("title", ""),
        },
    }


async def _archive_session(conn: asyncpg.Connection, session: dict) -> int:
    """增量存档到 PostgreSQL"""
    row = await conn.fetchrow(
        """
        INSERT INTO chat_sessions (agent, native_session_id, project_dir, model, started_at, ended_at, meta)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (agent, native_session_id) DO UPDATE SET
            project_dir = COALESCE(EXCLUDED.project_dir, chat_sessions.project_dir),
            model = COALESCE(EXCLUDED.model, chat_sessions.model),
            ended_at = COALESCE(EXCLUDED.ended_at, chat_sessions.ended_at),
            meta = chat_sessions.meta || COALESCE(EXCLUDED.meta, '{}')
        RETURNING id
        """,
        session["agent"], session["native_session_id"],
        session.get("project_dir"), session.get("model"),
        session.get("started_at"), session.get("ended_at"),
        json.dumps(session.get("meta", {}), ensure_ascii=False),
    )
    if row is None:
        log.error(f"Failed to upsert session {session['native_session_id']}")
        return 0
    sid = row["id"]

    # 时间戳水位线 + 同时间戳去重（替代 COUNT offset）
    last_ts = await conn.fetchval(
        "SELECT MAX(created_at) FROM chat_messages WHERE session_id = $1", sid
    )
    if last_ts is not None:
        at_ts = {
            (r["role"], r["prefix"])
            for r in await conn.fetch(
                "SELECT role, LEFT(content, 200) AS prefix FROM chat_messages WHERE session_id = $1 AND created_at = $2",
                sid, last_ts,
            )
        }
        new_messages = [
            m for m in session["messages"]
            if m["created_at"] > last_ts
            or (m["created_at"] == last_ts and (m["role"], (m.get("content") or "")[:200]) not in at_ts)
        ]
    else:
        new_messages = session["messages"]

    if not new_messages:
        return 0
    count = 0
    for msg in new_messages:
        content = msg.get("content") or ""
        if CHAT_SANITIZE and content:
            from ..quality import contains_sensitive as _chk
            if _chk(content):
                continue
        result = await conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, meta, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            sid, msg["role"], content,
            json.dumps(msg.get("meta", {}), ensure_ascii=False), msg["created_at"],
        )
        if result == "INSERT 0 1":
            count += 1

    if count > 0:
        await conn.execute(
            """
            UPDATE chat_sessions SET message_count = (
                SELECT COUNT(*) FROM chat_messages WHERE session_id = $1
            ) WHERE id = $1
            """,
            sid,
        )

    return count


async def run():
    """Hook 主入口"""
    _setup_logging()

    try:
        session_path = _find_latest_session()
        if not session_path:
            log.info("No OpenCode session found")
            return

        session = _parse_session(session_path)
        if not session:
            log.info("No messages in session")
            return

        try:
            conn = await _get_conn()
        except Exception as e:
            log.warning(f"DB connection failed, skipping: {e}")
            return

        try:
            count = await _archive_session(conn, session)
            if count > 0:
                log.info(f"Archived {count} new messages for {session['native_session_id']}")
        finally:
            await conn.close()

        # Knowledge extraction (rate-limited, per-turn hooks fire often)
        from ._common import fork_extraction
        fork_extraction(
            session["messages"],
            agent="opencode",
            source_label=session.get("project_dir", session["native_session_id"]),
            project_dir=session.get("project_dir"),
            rate_limit_sec=300,
            session_id=session["native_session_id"],
        )

    except Exception as e:
        log.error(f"Hook error: {e}", exc_info=True)


def main():
    """CLI entry point for the hook."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
