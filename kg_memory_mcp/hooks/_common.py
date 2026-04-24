"""Shared extraction logic for all agent hooks.

Provides: DB connection, embedding, LLM extraction, KG save, fork helper.
Each hook handles its own input parsing, session discovery, and archiving,
then calls fork_extraction() for background knowledge extraction.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import asyncpg
import httpx
import numpy as np
from pgvector.asyncpg import register_vector

log = logging.getLogger("kg-memory-hook")

# ── Config ──────────────────────────────────────────────────

DB_NAME = os.environ.get("KG_DB_NAME", "knowledge_base")
DB_USER = os.environ.get("KG_DB_USER", "didi")
DB_HOST = os.environ.get("KG_DB_HOST", "localhost")
DB_PORT = os.environ.get("KG_DB_PORT", "5432")
DB_PASSWORD = os.environ.get("KG_DB_PASSWORD", "")
DB_SSL = os.environ.get("KG_DB_SSL", "")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_EXTRACT_MODEL = os.environ.get("KG_EXTRACT_OPENAI_MODEL", "gpt-5.4-mini")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_EXTRACT_MODEL = os.environ.get("KG_EXTRACT_DEEPSEEK_MODEL", "deepseek-v4-flash")

MIN_MESSAGES = 3


# ── DB ──────────────────────────────────────────────────────

async def get_conn() -> asyncpg.Connection:
    kwargs: dict = dict(database=DB_NAME, user=DB_USER, host=DB_HOST, port=int(DB_PORT), timeout=10)
    if DB_PASSWORD:
        kwargs["password"] = DB_PASSWORD
    if DB_SSL and DB_SSL.lower() not in ("disable", "false", "0"):
        kwargs["ssl"] = True
    conn = await asyncpg.connect(**kwargs)
    await register_vector(conn)
    return conn


# ── Embedding ───────────────────────────────────────────────

def get_embedding(text: str) -> list[float] | None:
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": text},
            timeout=30.0, trust_env=False,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]
    except Exception as e:
        log.warning(f"Embedding error: {e}")
        return None


def get_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    """Batch embedding — 1 HTTP call instead of N."""
    if not texts:
        return []
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": OLLAMA_EMBED_MODEL, "input": texts},
            timeout=60.0, trust_env=False,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
    except Exception as e:
        log.warning(f"Batch embedding error: {e}")
        return [None] * len(texts)


# ── Conversation builder ────────────────────────────────────

def build_conversation(messages: list[dict], max_content_len: int = 2000) -> str:
    """Build sanitized conversation text from normalized messages."""
    from ..quality import contains_sensitive

    blocks = []
    for m in messages:
        content = m.get("content", "")
        if content and len(content) > 50:
            if len(content) > max_content_len:
                content = content[:max_content_len] + "..."
            blocks.append(f"[{m['role']}]: {content}")

    return "\n\n".join(
        block for block in blocks
        if not contains_sensitive(block)
    )


# ── LLM extraction ─────────────────────────────────────────

def _build_extraction_prompt(conversation: str, source: str) -> str:
    return f"""分析以下 AI 编程助手的对话记录，提取值得长期记住的信息。

来源: {source}

对话记录:
{conversation[-30000:]}

请提取以下类型的信息（如果有的话），用 JSON 格式返回：
{{
  "user_preferences": ["用户偏好，如代码风格、工具选择"],
  "project_decisions": ["项目相关的重要决策"],
  "solutions": ["问题解决方案，格式：问题 -> 解决方法"],
  "learned_facts": ["了解到的用户/项目相关事实"],
  "skip_reason": "如果这个对话没有值得记住的内容，说明原因"
}}

只返回 JSON，不要其他内容。"""


def _extract_memories_from_text(text: str) -> list[str]:
    text = text.strip()
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


def _extract_with_gemini(prompt: str) -> list[str]:
    from google import genai  # type: ignore[attr-defined]

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return _extract_memories_from_text(response.text or "")


def _extract_with_openai(prompt: str) -> list[str]:
    url = f"{OPENAI_BASE_URL}/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_EXTRACT_MODEL,
        "input": prompt,
        "max_output_tokens": 2000,
    }

    resp = httpx.post(url, headers=headers, json=payload, timeout=60.0)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("output_text")
    if not text:
        parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    parts.append(content.get("text", ""))
        text = "\n".join(parts)
    return _extract_memories_from_text(text or "")


def _extract_with_deepseek(prompt: str) -> list[str]:
    """DeepSeek uses OpenAI-compatible /chat/completions endpoint."""
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_EXTRACT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
        # v4 defaults to thinking mode (output goes to reasoning_content, leaving content empty).
        # Disable for extraction so the JSON object is in `content` where we read it.
        "thinking": {"type": "disabled"},
    }

    resp = httpx.post(url, headers=headers, json=payload, timeout=60.0, trust_env=False)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_memories_from_text(text or "")


def extract_with_llm(conversation: str, source: str) -> list[str]:
    """Extract key knowledge: DeepSeek → Gemini → OpenAI (first configured wins, with fallback on error)."""
    if len(conversation) < 200:
        return []

    prompt = _build_extraction_prompt(conversation, source)

    if DEEPSEEK_API_KEY:
        try:
            return _extract_with_deepseek(prompt)
        except Exception as e:
            log.warning(f"DeepSeek extraction error: {e}")

    if GEMINI_API_KEY:
        try:
            return _extract_with_gemini(prompt)
        except Exception as e:
            log.warning(f"Gemini extraction error: {e}")

    if OPENAI_API_KEY:
        try:
            return _extract_with_openai(prompt)
        except Exception as e:
            log.warning(f"OpenAI extraction error: {e}")

    return []


# ── KG save ─────────────────────────────────────────────────

async def save_to_kg(
    conn: asyncpg.Connection,
    memories: list[str],
    agent: str,
    source_label: str,
    project_dir: str | None = None,
):
    """Save extracted memories to knowledge graph with batch embedding."""
    if not memories:
        return

    from ..quality import contains_sensitive

    # Determine entity
    if project_dir:
        entity_name = f"project/{os.path.basename(project_dir)}"
        entity_type = "Project"
        description = f"Auto-extracted from {agent} sessions in {project_dir}"
    else:
        entity_name = f"{agent}/sessions"
        entity_type = "Agent"
        description = f"Auto-extracted from {agent} sessions"

    emb = get_embedding(entity_name)
    emb_vec = np.array(emb, dtype=np.float32) if emb else None

    row = await conn.fetchrow(
        """
        INSERT INTO kg_entities (name, entity_type, description, embedding)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        entity_name, entity_type, description, emb_vec,
    )
    if row is None:
        log.error(f"Failed to upsert entity '{entity_name}'")
        return
    entity_id = row["id"]

    # Filter sensitive + dedup
    safe_memories = []
    for memory in memories:
        content = f"[{agent}: {source_label}] {memory}"
        if contains_sensitive(content):
            continue
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        exists = await conn.fetchval(
            "SELECT 1 FROM kg_observations WHERE entity_id = $1 AND content_hash = $2",
            entity_id, content_hash,
        )
        if exists:
            continue
        safe_memories.append(content)

    if not safe_memories:
        return

    # Batch embedding (1 call instead of N)
    embeddings = get_embeddings_batch(safe_memories)

    saved = 0
    for content, obs_emb in zip(safe_memories, embeddings):
        obs_emb_vec = np.array(obs_emb, dtype=np.float32) if obs_emb else None
        await conn.execute(
            """
            INSERT INTO kg_observations (entity_id, content, embedding, source_agent)
            VALUES ($1, $2, $3, $4)
            """,
            entity_id, content, obs_emb_vec, agent,
        )
        saved += 1

    log.info(f"Saved {saved} observations to kg entity '{entity_name}'")


# ── Pipeline ────────────────────────────────────────────────

async def extract_and_save(
    messages: list[dict],
    agent: str,
    source_label: str,
    project_dir: str | None = None,
):
    """Full pipeline: build conversation → LLM extract → save to KG."""
    conversation = build_conversation(messages)
    memories = extract_with_llm(conversation, source_label)
    if not memories:
        log.info(f"No memories extracted for {agent}")
        return

    conn = await get_conn()
    try:
        await save_to_kg(conn, memories, agent, source_label, project_dir)
        log.info(f"Extracted {len(memories)} memories for {agent}")
    finally:
        await conn.close()


# ── Rate limiting (for per-turn hooks) ──────────────────────

def _should_extract(session_id: str, interval_sec: int) -> bool:
    """Rate limit: only extract once per interval per session (atomic)."""
    marker = Path(tempfile.gettempdir()) / f"kg-extract-{hashlib.md5(session_id.encode()).hexdigest()[:12]}"
    try:
        age = time.time() - marker.stat().st_mtime
        if age < interval_sec:
            return False
    except FileNotFoundError:
        pass  # first time — proceed to create
    # Atomic create/update via exclusive open
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.utime(str(marker))  # refresh mtime
    except OSError:
        pass
    return True


# ── Background extraction via subprocess ─────────────────────

def fork_extraction(
    messages: list[dict],
    agent: str,
    source_label: str,
    project_dir: str | None = None,
    rate_limit_sec: int = 0,
    session_id: str = "",
):
    """Spawn a background subprocess for knowledge extraction.

    Parent returns immediately. Subprocess does heavy work independently.
    Uses subprocess.Popen instead of os.fork() to avoid asyncio/asyncpg deadlocks.

    Args:
        rate_limit_sec: >0 enables rate limiting (for per-turn hooks like codex/opencode).
                        0 = no limit (for session-end hooks like gemini/claude-code).
        session_id: used as rate-limit key when rate_limit_sec > 0.
    """
    if len(messages) < MIN_MESSAGES:
        log.info(f"Too few messages ({len(messages)}), skipping extraction")
        return

    if rate_limit_sec > 0 and session_id:
        if not _should_extract(session_id, rate_limit_sec):
            log.info(f"Rate limited: skipping extraction for {agent} session {session_id[:12]}")
            return

    # Serialize params to temp file (mkstemp: mode 0600, unpredictable name)
    payload = {
        "messages": messages,
        "agent": agent,
        "source_label": source_label,
        "project_dir": project_dir,
    }
    fd, tmp_path = tempfile.mkstemp(prefix="kg-extract-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
    except Exception:
        os.unlink(tmp_path)
        raise

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kg_memory_mcp.hooks._common", tmp_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info(f"Spawned background pid={proc.pid} for {agent} extraction")
    except Exception as e:
        log.error(f"Failed to spawn extraction subprocess: {e}")
        os.unlink(tmp_path)
        # Popen failed — undo rate-limit marker so next attempt isn't blocked
        if rate_limit_sec > 0 and session_id:
            marker = Path(tempfile.gettempdir()) / f"kg-extract-{hashlib.md5(session_id.encode()).hexdigest()[:12]}"
            marker.unlink(missing_ok=True)


# ── Subprocess entry point ───────────────────────────────────

def _subprocess_main():
    """Entry point when run as `python -m kg_memory_mcp.hooks._common <payload.json>`."""
    log_file = Path.home() / ".claude" / "hooks" / "kg-extract.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file), level=logging.INFO,
        format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if len(sys.argv) < 2:
        sys.exit(1)

    payload_path = Path(sys.argv[1])
    try:
        payload = json.loads(payload_path.read_text())
        asyncio.run(extract_and_save(
            payload["messages"],
            payload["agent"],
            payload["source_label"],
            payload.get("project_dir"),
        ))
    except Exception as e:
        log.error(f"Extraction subprocess error: {e}", exc_info=True)
    finally:
        payload_path.unlink(missing_ok=True)


if __name__ == "__main__":
    _subprocess_main()
