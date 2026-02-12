"""对话存档数据库操作层"""

import json
import os

from .db import get_pool
from .quality import contains_sensitive

_CHAT_SANITIZE = os.getenv("KG_CHAT_SANITIZE", "").lower() in ("1", "true", "yes")


async def upsert_session(
    agent: str,
    native_session_id: str,
    project_dir: str | None = None,
    model: str | None = None,
    started_at=None,
    ended_at=None,
    meta: dict | None = None,
) -> int:
    """创建或更新会话，返回 session_id"""
    pool = await get_pool()
    row = await pool.fetchrow(
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
        agent, native_session_id, project_dir, model, started_at, ended_at,
        json.dumps(meta or {}, ensure_ascii=False),
    )
    return row["id"]


async def insert_messages(session_id: int, messages: list[dict]) -> tuple[list[int], int]:
    """批量插入消息，返回 (message_id 列表, 跳过的已有消息数)。通过已有消息数实现增量导入。

    返回的 message_id 列表仅包含本次新插入的消息，与 messages[existing:] 一一对应。
    被 sanitize 过滤的消息 id 为 -1。
    """
    if not messages:
        return [], 0
    pool = await get_pool()

    # 增量导入：跳过已存在的消息
    existing: int = await pool.fetchval(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1", session_id
    )
    new_messages = messages[existing:]
    if not new_messages:
        return [], existing

    inserted_ids: list[int] = []
    for msg in new_messages:
        content = msg.get("content") or ""
        # KG_CHAT_SANITIZE=true 时过滤含敏感信息的消息
        if _CHAT_SANITIZE and content and contains_sensitive(content):
            inserted_ids.append(-1)  # placeholder for skipped messages
            continue
        row = await pool.fetchrow(
            """
            INSERT INTO chat_messages (session_id, role, content, meta, created_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            session_id, msg["role"], content,
            json.dumps(msg.get("meta", {}), ensure_ascii=False), msg["created_at"],
        )
        inserted_ids.append(row["id"])

    # 更新 message_count
    await pool.execute(
        """
        UPDATE chat_sessions SET message_count = (
            SELECT COUNT(*) FROM chat_messages WHERE session_id = $1
        ) WHERE id = $1
        """,
        session_id,
    )
    return inserted_ids, existing


async def insert_attachment(
    message_id: int,
    file_path: str,
    file_type: str | None = None,
    file_size: int | None = None,
) -> int:
    """插入附件记录"""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO chat_attachments (message_id, file_path, file_type, file_size)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        message_id, file_path, file_type, file_size,
    )
    return row["id"]


async def search_chats(query: str, agent: str | None = None, limit: int = 20) -> list[dict]:
    """搜索对话消息 (FTS + ILIKE fallback + ts_rank 排序)"""
    pool = await get_pool()

    if agent:
        rows = await pool.fetch(
            """
            SELECT m.id, m.role, m.content, m.created_at,
                   s.agent, s.native_session_id, s.project_dir,
                   ts_rank(m.search_vector, plainto_tsquery('simple', $1)) AS rank
            FROM chat_messages m
            JOIN chat_sessions s ON m.session_id = s.id
            WHERE (m.search_vector @@ plainto_tsquery('simple', $1)
                   OR m.content ILIKE '%' || $2 || '%')
              AND s.agent = $3
            ORDER BY rank DESC, m.created_at DESC
            LIMIT $4
            """,
            query, query, agent, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT m.id, m.role, m.content, m.created_at,
                   s.agent, s.native_session_id, s.project_dir,
                   ts_rank(m.search_vector, plainto_tsquery('simple', $1)) AS rank
            FROM chat_messages m
            JOIN chat_sessions s ON m.session_id = s.id
            WHERE m.search_vector @@ plainto_tsquery('simple', $1)
                  OR m.content ILIKE '%' || $2 || '%'
            ORDER BY rank DESC, m.created_at DESC
            LIMIT $3
            """,
            query, query, limit,
        )
    return [dict(r) for r in rows]


async def get_session(
    session_id: int | None = None,
    native_session_id: str | None = None,
    agent: str | None = None,
) -> dict | None:
    """获取完整会话（含消息）"""
    pool = await get_pool()

    if session_id:
        session = await pool.fetchrow(
            "SELECT * FROM chat_sessions WHERE id = $1", session_id
        )
    elif native_session_id and agent:
        session = await pool.fetchrow(
            "SELECT * FROM chat_sessions WHERE agent = $1 AND native_session_id = $2",
            agent, native_session_id,
        )
    elif native_session_id:
        # native_session_id 跨 agent 可能重复，不提供 agent 则拒绝查询
        return None
    else:
        return None

    if session is None:
        return None

    messages = await pool.fetch(
        """
        SELECT m.*, COALESCE(
            json_agg(json_build_object('file_path', a.file_path, 'file_type', a.file_type))
            FILTER (WHERE a.id IS NOT NULL), '[]'
        ) AS attachments
        FROM chat_messages m
        LEFT JOIN chat_attachments a ON a.message_id = m.id
        WHERE m.session_id = $1
        GROUP BY m.id
        ORDER BY m.created_at
        """,
        session["id"],
    )

    return {
        **dict(session),
        "messages": [
            {**dict(m), "attachments": json.loads(m["attachments"])}
            for m in messages
        ],
    }


async def list_sessions(agent: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
    """列出会话列表"""
    pool = await get_pool()

    if agent:
        rows = await pool.fetch(
            """
            SELECT id, agent, native_session_id, project_dir, model,
                   message_count, started_at, ended_at
            FROM chat_sessions
            WHERE agent = $1
            ORDER BY started_at DESC NULLS LAST
            LIMIT $2 OFFSET $3
            """,
            agent, limit, offset,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, agent, native_session_id, project_dir, model,
                   message_count, started_at, ended_at
            FROM chat_sessions
            ORDER BY started_at DESC NULLS LAST
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return [dict(r) for r in rows]
