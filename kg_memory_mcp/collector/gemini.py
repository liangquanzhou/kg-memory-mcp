"""Gemini CLI 对话解析器"""

import glob as glob_mod
import json
import os
from datetime import datetime

from . import import_sessions


def parse_gemini_session(filepath: str) -> dict | None:
    """解析 Gemini CLI 的 JSON 会话文件

    格式: 单个 JSON 对象 (非 JSONL)
    {sessionId, projectHash, startTime, lastUpdated, messages: [{id, timestamp, type, content}]}
    """
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    raw_messages = data.get("messages", [])
    if not raw_messages:
        return None

    session_id = data.get("sessionId", os.path.basename(filepath).replace(".json", ""))
    messages = []
    first_ts = None
    last_ts = None

    for msg in raw_messages:
        ts_str = msg.get("timestamp")
        if not ts_str:
            continue

        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if first_ts is None:
            first_ts = ts
        last_ts = ts

        msg_type = msg.get("type", "")
        raw = msg.get("content", "")

        # content 可能是 string 或 list
        if isinstance(raw, list):
            content = "\n".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in raw
            )
        elif isinstance(raw, str):
            content = raw
        else:
            content = str(raw) if raw else ""

        if not content.strip():
            continue

        # gemini → assistant
        role = "assistant" if msg_type == "gemini" else "user"

        # 移除引用文件的内容块
        if "--- Content from referenced files ---" in content:
            content = content[:content.index("--- Content from referenced files ---")].strip()

        if not content:
            continue

        messages.append({
            "role": role,
            "content": content[:50000],
            "meta": {},
            "created_at": ts,
        })

    if not messages:
        return None

    return {
        "agent": "gemini-cli",
        "native_session_id": session_id,
        "project_dir": None,
        "model": None,
        "messages": messages,
        "started_at": first_ts,
        "ended_at": last_ts,
        "meta": {"projectHash": data.get("projectHash", "")},
    }


async def collect() -> tuple[int, int]:
    """采集所有 Gemini CLI 会话"""
    session_dir = os.path.expanduser("~/.gemini/tmp")
    files = sorted(glob_mod.glob(os.path.join(session_dir, "*/chats/session-*.json"), recursive=False))
    print(f"\nGemini CLI: 发现 {len(files)} 个会话文件")

    # 按 sessionId 去重，保留最新的文件
    by_session: dict[str, tuple[str, str]] = {}
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
            sid = data.get("sessionId", "")
            updated = data.get("lastUpdated", "")
            if sid and (sid not in by_session or updated > by_session[sid][1]):
                by_session[sid] = (f, updated)
        except (json.JSONDecodeError, OSError):
            continue

    unique_files = [fp for fp, _ in by_session.values()]
    print(f"  去重后: {len(unique_files)} 个唯一会话")

    sessions = []
    for f in unique_files:
        session = parse_gemini_session(f)
        if session:
            sessions.append(session)

    print(f"  有效会话: {len(sessions)}")
    s_count, m_count = await import_sessions(sessions)
    print(f"  导入: {s_count} 会话, {m_count} 消息")
    return s_count, m_count
