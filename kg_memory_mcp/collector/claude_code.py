"""Claude Code 对话解析器"""

import glob as glob_mod
import json
import os
from datetime import datetime

from . import import_sessions


def parse_claude_code_session(filepath: str) -> dict | None:
    """解析 Claude Code 会话文件 (projects/ 格式)

    格式: 每行一个 JSON
    type = user | assistant | system | progress | file-history-snapshot | queue-operation
    """
    filename = os.path.basename(filepath)
    session_id = filename.replace(".jsonl", "")

    messages = []
    first_ts = None
    last_ts = None
    project_dir = None
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

            record_type = record.get("type", "")
            if record_type not in ("user", "assistant"):
                continue

            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if first_ts is None:
                first_ts = ts
            last_ts = ts

            if project_dir is None and record.get("cwd"):
                project_dir = record["cwd"]

            msg = record.get("message", {})
            role = msg.get("role", record_type)
            raw_content = msg.get("content", "")
            meta = {}

            if model is None and msg.get("model"):
                model = msg["model"]

            # 解析 content (可能是 string 或 list of parts)
            attachments = []
            if isinstance(raw_content, list):
                texts = []
                tool_count = 0
                for part in raw_content:
                    if isinstance(part, dict):
                        ptype = part.get("type", "")
                        if ptype == "text":
                            texts.append(part.get("text", ""))
                        elif ptype == "tool_use":
                            tool_count += 1
                        elif ptype == "image":
                            source = part.get("source", {})
                            if source.get("type") == "base64" and source.get("data"):
                                attachments.append({
                                    "media_type": source.get("media_type", "image/png"),
                                    "data": source["data"],
                                })
                    elif isinstance(part, str):
                        texts.append(part)
                content = "\n".join(texts)
                if tool_count:
                    meta["tool_count"] = tool_count
            elif isinstance(raw_content, str):
                content = raw_content
            else:
                content = str(raw_content) if raw_content else ""

            if not content.strip() and not attachments:
                continue

            if attachments:
                meta["has_images"] = True
                meta["image_count"] = len(attachments)

            msg_data: dict = {
                "role": role,
                "content": content[:50000],
                "meta": meta,
                "created_at": ts,
            }
            if attachments:
                msg_data["attachments"] = attachments

            messages.append(msg_data)

    if not messages:
        return None

    return {
        "agent": "claude-code",
        "native_session_id": session_id,
        "project_dir": project_dir,
        "model": model,
        "messages": messages,
        "started_at": first_ts,
        "ended_at": last_ts,
        "meta": {},
    }


async def collect() -> tuple[int, int]:
    """采集所有 Claude Code 会话 (从 ~/.claude/projects/)"""
    projects_dir = os.path.expanduser("~/.claude/projects")
    files = sorted(
        f for f in glob_mod.glob(os.path.join(projects_dir, "**/*.jsonl"), recursive=True)
        if "/subagents/" not in f and os.path.getsize(f) > 1024
    )
    print(f"\nClaude Code: 发现 {len(files)} 个会话文件 (projects/)")

    sessions = []
    for f in files:
        session = parse_claude_code_session(f)
        if session:
            sessions.append(session)

    print(f"  有效会话: {len(sessions)}")
    s_count, m_count = await import_sessions(sessions)
    print(f"  导入: {s_count} 会话, {m_count} 消息")
    return s_count, m_count
