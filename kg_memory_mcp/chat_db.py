"""对话存档数据库操作层"""

import json

from .db import get_pool


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


async def insert_messages(session_id: int, messages: list[dict]) -> int:
    """批量插入消息，返回插入数量。通过已有消息数实现增量导入。"""
    if not messages:
        return 0
    pool = await get_pool()

    # 增量导入：跳过已存在的消息
    existing = await pool.fetchval(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = $1", session_id
    )
    new_messages = messages[existing:]
    if not new_messages:
        return 0

    count = 0
    for msg in new_messages:
        await pool.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, meta, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            session_id, msg["role"], msg.get("content"),
            json.dumps(msg.get("meta", {}), ensure_ascii=False), msg["created_at"],
        )
        count += 1

    # 更新 message_count
    await pool.execute(
        """
        UPDATE chat_sessions SET message_count = (
            SELECT COUNT(*) FROM chat_messages WHERE session_id = $1
        ) WHERE id = $1
        """,
        session_id,
    )
    return count


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

    idx = 1
    params: list = []

    # FTS 条件 (plainto_tsquery 自动分词)
    fts_cond = f"m.search_vector @@ plainto_tsquery('simple', ${idx})"
    params.append(query)
    idx += 1

    # ILIKE fallback (FTS 对短词/特殊字符可能不匹配)
    ilike_cond = f"m.content ILIKE '%' || ${idx} || '%'"
    params.append(query)
    idx += 1

    # 组合: FTS OR ILIKE
    search_cond = f"({fts_cond} OR {ilike_cond})"

    agent_cond = ""
    if agent:
        agent_cond = f"AND s.agent = ${idx}"
        params.append(agent)
        idx += 1

    params.append(limit)

    rows = await pool.fetch(
        f"""
        SELECT m.id, m.role, m.content, m.created_at,
               s.agent, s.native_session_id, s.project_dir,
               ts_rank(m.search_vector, plainto_tsquery('simple', $1)) AS rank
        FROM chat_messages m
        JOIN chat_sessions s ON m.session_id = s.id
        WHERE {search_cond} {agent_cond}
        ORDER BY rank DESC, m.created_at DESC
        LIMIT ${idx}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def get_session(session_id: int | None = None, native_session_id: str | None = None) -> dict | None:
    """获取完整会话（含消息）"""
    pool = await get_pool()

    if session_id:
        session = await pool.fetchrow(
            "SELECT * FROM chat_sessions WHERE id = $1", session_id
        )
    elif native_session_id:
        session = await pool.fetchrow(
            "SELECT * FROM chat_sessions WHERE native_session_id = $1", native_session_id
        )
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
