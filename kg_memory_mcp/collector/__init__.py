"""对话采集器：解析各 agent 转录文件 → PostgreSQL"""

import os

from .. import chat_db

ATTACHMENT_DIR = os.getenv(
    "ATTACHMENT_DIR",
    os.path.expanduser("~/.local/share/kg-memory/attachments"),
)


async def import_sessions(sessions: list[dict]) -> tuple[int, int]:
    """批量导入会话，返回 (session_count, message_count)"""
    total_sessions = 0
    total_messages = 0

    for session in sessions:
        try:
            sid = await chat_db.upsert_session(
                agent=session["agent"],
                native_session_id=session["native_session_id"],
                project_dir=session.get("project_dir"),
                model=session.get("model"),
                started_at=session.get("started_at"),
                ended_at=session.get("ended_at"),
                meta=session.get("meta"),
            )
            count = await chat_db.insert_messages(sid, session["messages"])
            total_sessions += 1
            total_messages += count
        except Exception as e:
            print(f"  ⚠ 导入失败 {session['native_session_id']}: {e}")

    return total_sessions, total_messages
