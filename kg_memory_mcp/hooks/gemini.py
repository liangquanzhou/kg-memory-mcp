#!/usr/bin/env python3
"""Gemini CLI SessionEnd Hook: 自动采集对话 + 提取知识到 PostgreSQL 知识图谱

触发: Gemini CLI 会话结束时
数据传递: JSON via stdin
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
import httpx

log = logging.getLogger("kg-memory-hook-gemini")

DB_NAME = os.environ.get("KG_DB_NAME", "knowledge_base")
DB_USER = os.environ.get("KG_DB_USER", "postgres")
DB_HOST = os.environ.get("KG_DB_HOST", "localhost")
DB_PORT = os.environ.get("KG_DB_PORT", "5432")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

GEMINI_TMP_DIR = Path.home() / ".gemini" / "tmp"
MIN_MESSAGES = 3


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "gemini-session-end.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


DB_PASSWORD = os.environ.get("KG_DB_PASSWORD", "")


async def _get_conn() -> asyncpg.Connection:
    kwargs: dict = dict(database=DB_NAME, user=DB_USER, host=DB_HOST, port=int(DB_PORT), timeout=10)
    if DB_PASSWORD:
        kwargs["password"] = DB_PASSWORD
    return await asyncpg.connect(**kwargs)


def _get_embedding(text: str) -> list[float] | None:
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": text},
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]
    except Exception as e:
        log.warning(f"Embedding error: {e}")
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


async def _archive_conversation(conn: asyncpg.Connection, messages: list[dict], session_id: str):
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
        await conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, meta, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            db_session_id, m["role"], m["content"],
            json.dumps(m.get("meta", {})), m["created_at"],
        )

    await conn.execute(
        """
        UPDATE chat_sessions SET message_count = (
            SELECT COUNT(*) FROM chat_messages WHERE session_id = $1
        ) WHERE id = $1
        """,
        db_session_id,
    )
    log.info(f"Archived {len(messages)} messages for gemini session {session_id}")


def _extract_with_gemini(conversation: str, source: str) -> list:
    if not GEMINI_API_KEY or len(conversation) < 200:
        return []

    try:
        from google import genai  # type: ignore[attr-defined]
        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = f"""分析以下 Gemini CLI AI 编程助手的对话记录，提取值得长期记住的信息。

来源: {source}

对话记录:
{conversation[:15000]}

请提取以下类型的信息（如果有的话），用 JSON 格式返回：
{{
  "user_preferences": ["用户偏好，如代码风格、工具选择"],
  "project_decisions": ["项目相关的重要决策"],
  "solutions": ["问题解决方案，格式：问题 -> 解决方法"],
  "learned_facts": ["了解到的用户/项目相关事实"],
  "skip_reason": "如果这个对话没有值得记住的内容，说明原因"
}}

只返回 JSON，不要其他内容。"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text.strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        extracted = json.loads(text)

        memories = []
        for key in ["user_preferences", "project_decisions", "solutions", "learned_facts"]:
            items = extracted.get(key, [])
            if items:
                memories.extend(items)

        return memories
    except Exception as e:
        log.warning(f"Gemini error: {e}")
        return []


async def _save_to_kg(conn: asyncpg.Connection, memories: list, source: str):
    if not memories:
        return

    entity_name = "gemini-cli/sessions"

    emb = _get_embedding(entity_name)
    emb_str = str(emb) if emb else None

    row = await conn.fetchrow(
        """
        INSERT INTO kg_entities (name, entity_type, description, embedding)
        VALUES ($1, 'Agent', 'Auto-extracted from Gemini CLI sessions', $2)
        ON CONFLICT (name) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        entity_name, emb_str,
    )
    assert row is not None
    entity_id = row["id"]

    from ..quality import contains_sensitive

    saved = 0
    for memory in memories:
        content = f"[Gemini CLI: {source}] {memory}"
        if contains_sensitive(content):
            continue

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        exists = await conn.fetchval(
            "SELECT 1 FROM kg_observations WHERE entity_id = $1 AND content_hash = $2",
            entity_id, content_hash,
        )
        if exists:
            continue

        obs_emb = _get_embedding(content)
        obs_emb_str = str(obs_emb) if obs_emb else None

        await conn.execute(
            """
            INSERT INTO kg_observations (entity_id, content, embedding, source_agent)
            VALUES ($1, $2, $3, 'gemini-cli')
            """,
            entity_id, content, obs_emb_str,
        )
        saved += 1

    log.info(f"Saved {saved} observations from Gemini CLI")


async def run():
    """Hook 主入口"""
    _setup_logging()

    try:
        hook_input = json.load(sys.stdin)
        reason = hook_input.get("reason", "unknown")

        log.info(f"SessionEnd triggered: reason={reason}")

        session_path = _find_latest_session()
        if not session_path:
            log.info("No session file found")
            return

        log.info(f"Processing: {session_path.name}")

        messages, session_id = _parse_session(session_path)
        if not messages:
            log.info("No messages found")
            return

        try:
            conn = await _get_conn()
        except Exception as e:
            log.warning(f"DB connection failed, skipping: {e}")
            return

        try:
            # 1. 存档原始对话
            await _archive_conversation(conn, messages, session_id)

            # 2. 提取知识
            if len(messages) < MIN_MESSAGES:
                log.info(f"Too few messages ({len(messages)}), skipping extraction")
                return

            conversation = "\n\n".join(
                f"[{m['role']}]: {m['content'][:2000]}"
                for m in messages if m["content"] and len(m["content"]) > 50
            )

            project_hash = session_path.parent.parent.name[:8]
            source = f"{project_hash}/{session_path.stem}"

            memories = _extract_with_gemini(conversation, source)
            if memories:
                await _save_to_kg(conn, memories, source)
                log.info(f"Extracted {len(memories)} memories")
            else:
                log.info("No memories to extract")
        finally:
            await conn.close()

    except Exception as e:
        log.error(f"Hook error: {e}", exc_info=True)


def main():
    """CLI entry point for the hook."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
