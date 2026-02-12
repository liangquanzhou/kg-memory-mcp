"""对话采集器：解析各 agent 转录文件 → PostgreSQL"""

import base64
import hashlib
import os
from pathlib import Path

from .. import chat_db

ATTACHMENT_DIR = os.getenv(
    "ATTACHMENT_DIR",
    os.path.expanduser("~/.local/share/kg-memory/attachments"),
)

# media_type → file extension
_EXT_MAP = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


def _save_attachment(session_id: str, media_type: str, data: str) -> tuple[str, int]:
    """Save base64 attachment to disk. Returns (file_path, file_size).

    Uses content hash for dedup — same image stored only once.
    """
    raw = base64.b64decode(data)
    content_hash = hashlib.sha256(raw).hexdigest()[:16]
    ext = _EXT_MAP.get(media_type, "bin")

    dir_path = Path(ATTACHMENT_DIR) / session_id
    dir_path.mkdir(parents=True, exist_ok=True)

    file_path = dir_path / f"{content_hash}.{ext}"
    if not file_path.exists():
        file_path.write_bytes(raw)

    return str(file_path), len(raw)


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
            message_ids = await chat_db.insert_messages(sid, session["messages"])
            total_sessions += 1
            total_messages += len([mid for mid in message_ids if mid > 0])

            # Process attachments for newly inserted messages
            for msg, msg_id in zip(session["messages"], message_ids):
                if msg_id <= 0:
                    continue  # skipped or sanitized message
                for att in msg.get("attachments", []):
                    try:
                        file_path, file_size = _save_attachment(
                            session["native_session_id"],
                            att["media_type"],
                            att["data"],
                        )
                        await chat_db.insert_attachment(
                            message_id=msg_id,
                            file_path=file_path,
                            file_type=att["media_type"],
                            file_size=file_size,
                        )
                    except Exception as e:
                        print(f"  ⚠ Attachment save failed: {e}")

        except Exception as e:
            print(f"  ⚠ 导入失败 {session['native_session_id']}: {e}")

    return total_sessions, total_messages
