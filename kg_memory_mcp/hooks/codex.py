#!/usr/bin/env python3
"""Codex CLI hook: 自动存档对话到 PostgreSQL

支持两种入口:
- legacy notify: JSON via sys.argv[1], event type agent-turn-complete
- official hooks: JSON via stdin, event hook_event_name Stop
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("kg-memory-hook-codex")

CHAT_SANITIZE = os.environ.get("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "codex-notify.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _get_conn():
    from ._common import get_conn
    return await get_conn()


def _find_session_by_id(session_id: str) -> Path | None:
    """按 session_id 精确匹配 Codex 会话文件，避免并发串档

    Codex 文件名格式: rollout-{date}T{time}-{session_id}.jsonl
    用 endswith 确保 ID 位于文件名末尾，避免子串误匹配。
    """
    if not session_id or not CODEX_SESSIONS_DIR.exists():
        return None
    suffix = f"-{session_id}.jsonl"
    for f in CODEX_SESSIONS_DIR.glob("**/*.jsonl"):
        if f.name.endswith(suffix):
            return f
    return None


def _find_latest_session() -> Path | None:
    """Fallback：按修改时间找最新会话"""
    if not CODEX_SESSIONS_DIR.exists():
        return None
    sessions = list(CODEX_SESSIONS_DIR.glob("**/*.jsonl"))
    if not sessions:
        return None
    return max(sessions, key=lambda p: p.stat().st_mtime)


def _read_payload() -> dict | None:
    """Read Codex payload from legacy notify argv or official hooks stdin."""
    raw = None
    for arg in sys.argv[1:]:
        if arg.strip().startswith("{"):
            raw = arg
            break

    if raw is None and not sys.stdin.isatty():
        raw = sys.stdin.read()

    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        log.info("Ignoring non-JSON hook payload")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"Ignoring invalid hook payload: {e}")
        return None


def _find_session_for_payload(payload: dict) -> Path | None:
    """Resolve transcript path from official hook payload, with legacy fallbacks."""
    transcript_path = payload.get("transcript_path")
    if transcript_path:
        path = Path(transcript_path).expanduser()
        if path.exists():
            return path

    sid = payload.get("session_id", "")
    session_path = _find_session_by_id(sid) if sid else None
    if session_path is None:
        if sid:
            log.warning(f"Session file not found for id={sid[:12]}..., falling back to latest")
        session_path = _find_latest_session()
    return session_path


def _parse_session(filepath: Path) -> dict | None:
    """解析 Codex JSONL"""
    messages = []
    session_meta = {}
    first_ts = None
    last_ts = None
    model = None

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = record.get("timestamp")
            if not ts_str:
                continue

            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if first_ts is None:
                first_ts = ts
            last_ts = ts

            record_type = record.get("type", "")
            payload = record.get("payload", {})

            if record_type == "session_meta":
                session_meta = payload
                continue

            if record_type == "turn_context":
                if payload.get("model"):
                    model = payload["model"]
                continue

            if record_type == "response_item":
                role = payload.get("role", "")
                content_parts = payload.get("content") or []

                texts = []
                for part in content_parts:
                    if isinstance(part, dict):
                        if part.get("type") in ("input_text", "output_text", "text"):
                            texts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        texts.append(part)

                content = "\n".join(texts)
                if not content.strip():
                    continue

                if "<environment_context>" in content and len(content) < 500:
                    continue

                messages.append({
                    "role": role or "user",
                    "content": content[:50000],
                    "meta": {},
                    "created_at": ts,
                })

            elif record_type == "event_msg":
                msg_type = payload.get("type", "")
                if msg_type == "user_message":
                    text = payload.get("message", "")
                    if text.strip():
                        messages.append({
                            "role": "user",
                            "content": text[:50000],
                            "meta": {"source": "event_msg"},
                            "created_at": ts,
                        })

    if not messages:
        return None

    native_id = session_meta.get("id", filepath.stem)
    project_dir = session_meta.get("cwd")

    return {
        "agent": "codex",
        "native_session_id": native_id,
        "project_dir": project_dir,
        "model": model,
        "messages": messages,
        "started_at": first_ts,
        "ended_at": last_ts,
        "meta": {
            k: v for k, v in session_meta.items()
            if k in ("cli_version", "source", "model_provider", "originator")
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
        # KG_CHAT_SANITIZE=true 时跳过含敏感信息的消息
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
        payload = _read_payload()
        if payload is None:
            log.info("No payload provided")
            return

        event_type = payload.get("type") or payload.get("hook_event_name", "")
        cwd = payload.get("cwd", "")

        if event_type not in ("agent-turn-complete", "Stop"):
            return

        session_path = _find_session_for_payload(payload)
        if not session_path:
            log.info("No session file found")
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
                log.info(f"Archived {count} new messages for {session['native_session_id']} (cwd={cwd})")
        finally:
            await conn.close()

        # Knowledge extraction (rate-limited, per-turn hooks fire often)
        from ._common import fork_extraction
        fork_extraction(
            session["messages"],
            agent="codex",
            source_label=cwd or session.get("native_session_id", ""),
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
