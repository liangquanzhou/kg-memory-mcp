"""Codex 对话解析器"""

import glob as glob_mod
import json
import os
from datetime import datetime

from . import import_sessions


def parse_codex_session(filepath: str) -> dict | None:
    """解析 Codex 的 JSONL 转录文件

    格式: 每行一个 JSON
    types: session_meta, response_item, event_msg, turn_context
    """
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
                attachments = []
                for part in content_parts:
                    if isinstance(part, dict):
                        ptype = part.get("type", "")
                        if ptype in ("input_text", "output_text", "text"):
                            texts.append(part.get("text", ""))
                        elif ptype == "input_image":
                            # Format: {"type": "input_image", "image_url": "data:image/png;base64,..."}
                            data_url = part.get("image_url", "")
                            if data_url.startswith("data:") and ";base64," in data_url:
                                header, b64 = data_url.split(";base64,", 1)
                                media_type = header.removeprefix("data:")
                                attachments.append({"media_type": media_type, "data": b64})
                    elif isinstance(part, str):
                        texts.append(part)

                content = "\n".join(texts)
                if not content.strip() and not attachments:
                    continue

                # 跳过 environment_context 系统消息
                if "<environment_context>" in content and len(content) < 500:
                    continue

                meta: dict = {}
                if attachments:
                    meta["has_images"] = True
                    meta["image_count"] = len(attachments)

                msg_data: dict = {
                    "role": role or "user",
                    "content": content[:50000],
                    "meta": meta,
                    "created_at": ts,
                }
                if attachments:
                    msg_data["attachments"] = attachments
                messages.append(msg_data)

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

    native_id = session_meta.get("id", os.path.basename(filepath).replace(".jsonl", ""))
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


async def collect() -> tuple[int, int]:
    """采集所有 Codex 转录"""
    session_dir = os.path.expanduser("~/.codex/sessions")
    files = sorted(glob_mod.glob(os.path.join(session_dir, "**/*.jsonl"), recursive=True))
    print(f"\nCodex: 发现 {len(files)} 个转录文件")

    sessions = []
    for f in files:
        session = parse_codex_session(f)
        if session:
            sessions.append(session)

    print(f"  有效会话: {len(sessions)}")
    s_count, m_count = await import_sessions(sessions)
    print(f"  导入: {s_count} 会话, {m_count} 消息")
    return s_count, m_count
