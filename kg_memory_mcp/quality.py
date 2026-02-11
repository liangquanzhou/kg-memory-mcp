"""写入质量控制：去重 + 敏感词过滤"""

import re

# 敏感词模式 — 匹配常见 API key / password / token 格式
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bpassword\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(sk|pk|ak|m0)-[A-Za-z0-9]{20,}"),  # OpenAI sk-xxx, Mem0 m0-xxx 等
    re.compile(r"(?i)\bAIza[A-Za-z0-9_-]{30,}"),  # Google API key
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{30,}"),  # GitHub personal access token
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}"),
]


def contains_sensitive(text: str) -> bool:
    """检查文本是否包含敏感信息"""
    return any(p.search(text) for p in _SENSITIVE_PATTERNS)


def filter_sensitive(observations: list[str]) -> list[str]:
    """过滤掉包含敏感信息的 observation"""
    return [o for o in observations if not contains_sensitive(o)]


async def is_duplicate_hash(pool, entity_id: int, content_hash: str) -> bool:
    """通过 content_hash 精确去重"""
    row = await pool.fetchval(
        "SELECT 1 FROM kg_observations WHERE entity_id = $1 AND content_hash = $2",
        entity_id,
        content_hash,
    )
    return row is not None


async def is_duplicate_semantic(pool, entity_id: int, embedding, threshold: float = 0.95) -> bool:
    """通过向量相似度去重 (余弦相似度 > threshold)"""
    row = await pool.fetchval(
        """
        SELECT 1 FROM kg_observations
        WHERE entity_id = $1
          AND embedding IS NOT NULL
          AND 1 - (embedding <=> $2::vector) > $3
        LIMIT 1
        """,
        entity_id,
        embedding,
        threshold,
    )
    return row is not None
