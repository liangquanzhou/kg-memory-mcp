"""OpenCode 对话解析器

OpenCode 存储结构 (~/.local/share/opencode/storage/):
  session/{projectHash}/ses_xxx.json   — 会话元数据 (title, directory, time)
  message/{ses_xxx}/msg_xxx.json       — 消息元数据 (role, model, time)
  part/{msg_xxx}/prt_xxx.json          — 消息内容 (text, tool-invocation, tool-result)
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from . import import_sessions

_STORAGE_DIR = os.path.expanduser("~/.local/share/opencode/storage")


def _ms_to_dt(ms: int | float) -> datetime:
    """Convert Unix milliseconds to timezone-aware datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def parse_opencode_session(session_path: Path) -> dict | None:
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

    # Load messages for this session
    msg_dir = Path(_STORAGE_DIR) / "message" / session_id
    if not msg_dir.is_dir():
        return None

    part_base = Path(_STORAGE_DIR) / "part"

    messages = []
    msg_files = sorted(msg_dir.iterdir())

    for msg_file in msg_files:
        msg = _load_json(msg_file)
        if not msg:
            continue

        msg_id = msg.get("id", "")
        role = msg.get("role", "unknown")
        msg_time = msg.get("time", {})
        created = _ms_to_dt(msg_time["created"]) if msg_time.get("created") else started_at

        model_info = msg.get("model", {})
        model_id = model_info.get("modelID", "")

        # Assemble content from parts
        parts_dir = part_base / msg_id
        if not parts_dir.is_dir():
            continue

        texts = []
        tool_count = 0
        attachments = []
        for part_file in sorted(parts_dir.iterdir()):
            part = _load_json(part_file)
            if not part:
                continue
            ptype = part.get("type", "")
            if ptype == "text" and part.get("text"):
                texts.append(part["text"])
            elif ptype == "tool-invocation":
                tool_count += 1
            elif ptype == "tool-result":
                result = part.get("result", "")
                if isinstance(result, str) and result.strip():
                    texts.append(f"[Result: {result[:500]}]")
            elif ptype == "file":
                # Format: {"type": "file", "mime": "image/png", "url": "data:...;base64,..."}
                mime = part.get("mime", "")
                data_url = part.get("url", "")
                if mime.startswith("image/") and data_url.startswith("data:") and ";base64," in data_url:
                    _, b64 = data_url.split(";base64,", 1)
                    attachments.append({"media_type": mime, "data": b64})

        content = "\n".join(texts).strip()
        if not content and not attachments:
            continue

        meta: dict = {}
        if tool_count:
            meta["tool_count"] = tool_count
        if attachments:
            meta["has_images"] = True
            meta["image_count"] = len(attachments)
        if model_id:
            meta["model"] = model_id

        msg_data: dict = {
            "role": role,
            "content": content[:50000],
            "meta": meta,
            "created_at": created,
        }
        if attachments:
            msg_data["attachments"] = attachments
        messages.append(msg_data)

    if not messages:
        return None

    return {
        "agent": "opencode",
        "native_session_id": session_id,
        "project_dir": session.get("directory"),
        "model": messages[0].get("meta", {}).get("model"),
        "messages": messages,
        "started_at": started_at,
        "ended_at": ended_at,
        "meta": {
            "title": session.get("title", ""),
            "version": session.get("version", ""),
        },
    }


async def collect() -> tuple[int, int]:
    """采集所有 OpenCode 会话"""
    session_base = Path(_STORAGE_DIR) / "session"
    if not session_base.is_dir():
        print("\nOpenCode: 未找到 session 目录")
        return 0, 0

    session_files = []
    for project_dir in session_base.iterdir():
        if project_dir.is_dir():
            for sf in project_dir.iterdir():
                if sf.suffix == ".json":
                    session_files.append(sf)

    session_files.sort()
    print(f"\nOpenCode: 发现 {len(session_files)} 个会话文件")

    sessions = []
    for sf in session_files:
        session = parse_opencode_session(sf)
        if session:
            sessions.append(session)

    print(f"  有效会话: {len(sessions)}")
    s_count, m_count = await import_sessions(sessions)
    print(f"  导入: {s_count} 会话, {m_count} 消息")
    return s_count, m_count
