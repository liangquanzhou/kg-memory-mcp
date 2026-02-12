#!/usr/bin/env python3
"""Claude Code SessionEnd Hook: 自动采集对话 + 提取知识到 PostgreSQL 知识图谱

触发: Claude Code 会话结束时
数据传递: JSON via stdin
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import asyncpg
import httpx

log = logging.getLogger("kg-memory-hook-claude")

# PostgreSQL 配置
DB_NAME = os.environ.get("KG_DB_NAME", "knowledge_base")
DB_USER = os.environ.get("KG_DB_USER", "postgres")
DB_HOST = os.environ.get("KG_DB_HOST", "localhost")
DB_PORT = os.environ.get("KG_DB_PORT", "5432")

# Ollama 配置
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")

# Gemini 配置
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CHAT_SANITIZE = os.environ.get("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")

MIN_TURNS = 3


def _setup_logging():
    log_file = Path.home() / ".claude" / "hooks" / "auto-memory.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


DB_PASSWORD = os.environ.get("KG_DB_PASSWORD", "")


DB_SSL = os.environ.get("KG_DB_SSL", "")


async def _get_conn() -> asyncpg.Connection:
    """获取短生命周期 DB 连接（hook 用，非连接池）"""
    kwargs: dict = dict(database=DB_NAME, user=DB_USER, host=DB_HOST, port=int(DB_PORT), timeout=10)
    if DB_PASSWORD:
        kwargs["password"] = DB_PASSWORD
    if DB_SSL and DB_SSL.lower() not in ("disable", "false", "0"):
        kwargs["ssl"] = True
    return await asyncpg.connect(**kwargs)


def _get_embedding(text: str) -> list[float] | None:
    """同步获取 Ollama embedding"""
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


def _read_transcript(path: str) -> list:
    messages = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    msg = json.loads(line.strip())
                    messages.append(msg)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning(f"Error reading transcript: {e}")
    return messages


async def _archive_conversation(conn: asyncpg.Connection, messages: list, session_id: str, cwd: str):
    """将原始对话存入 chat_sessions + chat_messages"""
    timestamps = []
    for m in messages:
        ts_str = m.get("timestamp")
        if ts_str:
            timestamps.append(ts_str.replace("Z", "+00:00"))

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

    msg_count = 0
    for m in messages:
        ts_str = m.get("timestamp")
        if not ts_str:
            continue

        msg_type = m.get("type", "")
        content = m.get("content", "")
        meta = {}

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
            continue  # skip tool_use, tool_result, etc.

        if not content or not content.strip():
            continue

        # KG_CHAT_SANITIZE=true 时跳过含敏感信息的消息
        if CHAT_SANITIZE:
            from ..quality import contains_sensitive as _chk
            if _chk(content):
                continue

        await conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, meta, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            db_session_id, role, content[:50000],
            json.dumps(meta, ensure_ascii=False),
            ts_str.replace("Z", "+00:00"),
        )
        msg_count += 1

    await conn.execute(
        """
        UPDATE chat_sessions SET message_count = (
            SELECT COUNT(*) FROM chat_messages WHERE session_id = $1
        ) WHERE id = $1
        """,
        db_session_id,
    )
    log.info(f"Archived {msg_count} messages for session {session_id}")


def _format_conversation(messages: list) -> str:
    formatted = []
    for msg in messages:
        msg_type = msg.get("type", msg.get("role", "unknown"))
        if msg_type not in ("user", "assistant"):
            continue

        content = msg.get("message", {}).get("content", "")
        if not content:
            content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = [
                item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
                else (item if isinstance(item, str) else "")
                for item in content
            ]
            content = "\n".join(t for t in text_parts if t)

        if content and len(content) > 50:
            if len(content) > 2000:
                content = content[:2000] + "..."
            formatted.append(f"[{msg_type}]: {content}")

    return "\n\n".join(formatted)


def _extract_with_gemini(conversation: str, cwd: str) -> dict | None:
    if not GEMINI_API_KEY:
        log.info("No GEMINI_API_KEY, skipping extraction")
        return None

    try:
        from google import genai  # type: ignore[attr-defined]

        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = f"""分析以下 AI 编程助手的对话记录，提取值得长期记住的信息。

工作目录: {cwd}

对话记录:
{conversation}

请提取以下类型的信息（如果有的话），用 JSON 格式返回：
{{
  "user_preferences": ["用户偏好，如代码风格、工具选择"],
  "project_decisions": ["项目相关的重要决策"],
  "solutions": ["问题解决方案，格式：问题 -> 解决方法"],
  "learned_facts": ["了解到的用户/项目相关事实"],
  "skip_reason": "如果这个对话没有值得记住的内容，说明原因"
}}

只返回 JSON，不要其他内容。如果没有值得记住的内容，返回空数组但说明 skip_reason。"""

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
        )
        text = response.text.strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        return json.loads(text)
    except Exception as e:
        log.warning(f"Gemini extraction error: {e}")
        return None


async def _save_to_kg(conn: asyncpg.Connection, memories: list, cwd: str):
    """保存提取的知识到 kg_entities + kg_observations"""
    if not memories:
        return

    project_name = os.path.basename(cwd) if cwd else "general"
    entity_name = f"project/{project_name}"

    emb = _get_embedding(entity_name)
    emb_str = str(emb) if emb else None

    row = await conn.fetchrow(
        """
        INSERT INTO kg_entities (name, entity_type, description, embedding)
        VALUES ($1, 'Project', $2, $3)
        ON CONFLICT (name) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        entity_name, f"Auto-extracted from {cwd}", emb_str,
    )
    assert row is not None
    entity_id = row["id"]

    from ..quality import contains_sensitive

    saved = 0
    for memory in memories:
        content = f"[{cwd}] {memory}"
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
            VALUES ($1, $2, $3, 'auto-extract')
            """,
            entity_id, content, obs_emb_str,
        )
        saved += 1

    log.info(f"Saved {saved} observations to kg entity '{entity_name}'")


async def run():
    """Hook 主入口"""
    _setup_logging()

    try:
        hook_input = json.load(sys.stdin)
        transcript_path = hook_input.get("transcript_path")
        session_id = hook_input.get("session_id", "unknown")
        cwd = hook_input.get("cwd", "")
        reason = hook_input.get("reason", "")

        log.info(f"SessionEnd triggered: session={session_id[:12]}..., reason={reason}")

        if not transcript_path or not Path(transcript_path).exists():
            log.info("No transcript file, skipping")
            return

        # 路径安全校验：resolve() 后用 is_relative_to 防 symlink 绕过，并用 resolved 路径操作 (防 TOCTOU)
        resolved = Path(transcript_path).resolve()
        allowed_prefixes = [
            Path.home() / ".claude",
            Path("/tmp").resolve(),  # macOS: /tmp → /private/tmp
        ]
        if not any(resolved.is_relative_to(p) for p in allowed_prefixes):
            log.warning("Transcript path outside allowed directories")
            return

        messages = _read_transcript(str(resolved))
        user_turns = sum(1 for m in messages if m.get("type") == "user")

        # 连接数据库
        try:
            conn = await _get_conn()
        except Exception as e:
            log.warning(f"DB connection failed, skipping: {e}")
            return

        try:
            # 1. 存档原始对话
            await _archive_conversation(conn, messages, session_id, cwd)

            # 2. 提取知识（需要足够长的对话）
            if user_turns < MIN_TURNS:
                log.info(f"Too few turns ({user_turns}), skipping extraction")
                return

            conversation = _format_conversation(messages)

            # 脱敏：过滤含敏感信息的段落，防止发送到外部 API
            from ..quality import contains_sensitive as _sens_check
            conversation = "\n\n".join(
                block for block in conversation.split("\n\n")
                if not _sens_check(block)
            )

            if len(conversation) < 200:
                log.info("Conversation too short, skipping extraction")
                return

            extracted = _extract_with_gemini(conversation, cwd)
            if extracted:
                memories = []
                for key in ["user_preferences", "project_decisions", "solutions", "learned_facts"]:
                    items = extracted.get(key, [])
                    if items:
                        memories.extend(items)

                if memories:
                    await _save_to_kg(conn, memories, cwd)
                    log.info(f"Successfully processed {len(memories)} memories")
                else:
                    skip_reason = extracted.get("skip_reason", "No valuable info")
                    log.info(f"Nothing to save: {skip_reason}")
        finally:
            await conn.close()

    except Exception as e:
        log.error(f"Hook error: {e}", exc_info=True)


def main():
    """CLI entry point for the hook."""
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()
