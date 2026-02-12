"""知识图谱数据库操作层 (asyncpg)"""

import asyncio
import hashlib
import json
import os

import asyncpg
from pgvector.asyncpg import register_vector

from .embedding import get_embedding
from .quality import contains_sensitive, is_duplicate_hash, is_duplicate_semantic

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        kwargs: dict = dict(
            database=os.getenv("KG_DB_NAME", "knowledge_base"),
            user=os.getenv("KG_DB_USER", "postgres"),
            host=os.getenv("KG_DB_HOST", "localhost"),
            port=int(os.getenv("KG_DB_PORT", "5432")),
            min_size=2,
            max_size=5,
            command_timeout=60,
            timeout=10,
            init=register_vector,
        )
        password = os.getenv("KG_DB_PASSWORD")
        if password:
            kwargs["password"] = password
        _pool = await asyncpg.create_pool(**kwargs)
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def health_check() -> bool:
    """Check if the database connection pool is healthy."""
    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        return True
    except Exception:
        return False


# ============================================================
# Entities
# ============================================================

async def create_entities(entities: list[dict]) -> list[dict]:
    """创建实体，返回创建结果列表"""
    pool = await get_pool()
    results = []
    for e in entities:
        name = e["name"]
        entity_type = e["entityType"]
        description = e.get("description", "")

        # 生成 embedding (基于 name + description)
        embed_text = f"{name}: {description}" if description else name
        emb = await get_embedding(embed_text)

        row = await pool.fetchrow(
            """
            INSERT INTO kg_entities (name, entity_type, description, embedding)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                description = EXCLUDED.description,
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
            RETURNING id, name
            """,
            name, entity_type, description, emb,
        )

        # 如果有初始 observations，直接添加
        observations = e.get("observations", [])
        if observations:
            await add_observations(name, observations, source_agent=None)

        results.append({"name": row["name"], "id": row["id"]})
    return results


async def delete_entities(names: list[str]) -> list[str]:
    """删除实体（级联删除 observations 和 relations）"""
    pool = await get_pool()
    deleted = []
    for name in names:
        result = await pool.execute(
            "DELETE FROM kg_entities WHERE name = $1", name
        )
        if result == "DELETE 1":
            deleted.append(name)
    return deleted


# ============================================================
# Observations
# ============================================================

async def add_observations(entity_name: str, observations: list[str], source_agent: str | None = None) -> list[str]:
    """向实体添加观察，自动去重 + 敏感词过滤"""
    pool = await get_pool()

    # 查实体
    entity = await pool.fetchrow(
        "SELECT id FROM kg_entities WHERE name = $1", entity_name
    )
    if entity is None:
        raise ValueError(f"Entity '{entity_name}' not found")

    entity_id = entity["id"]
    added = []

    # 过滤敏感信息
    safe_observations = [o for o in observations if not contains_sensitive(o)]

    for content in safe_observations:
        # content_hash 去重
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if await is_duplicate_hash(pool, entity_id, content_hash):
            continue

        # 生成 embedding
        emb = await get_embedding(content)

        # 向量去重
        if await is_duplicate_semantic(pool, entity_id, emb):
            continue

        await pool.execute(
            """
            INSERT INTO kg_observations (entity_id, content, embedding, source_agent)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (entity_id, content_hash) DO NOTHING
            """,
            entity_id, content, emb, source_agent,
        )
        added.append(content)

    return added


async def delete_observations(entity_name: str, observations: list[str]) -> list[str]:
    """删除指定 observations"""
    pool = await get_pool()
    entity = await pool.fetchrow(
        "SELECT id FROM kg_entities WHERE name = $1", entity_name
    )
    if entity is None:
        raise ValueError(f"Entity '{entity_name}' not found")

    deleted = []
    for content in observations:
        result = await pool.execute(
            "DELETE FROM kg_observations WHERE entity_id = $1 AND content = $2",
            entity["id"], content,
        )
        if result == "DELETE 1":
            deleted.append(content)
    return deleted


# ============================================================
# Relations
# ============================================================

async def create_relations(relations: list[dict]) -> list[dict]:
    """创建实体间关系"""
    pool = await get_pool()
    created = []
    for r in relations:
        from_name = r["from"]
        to_name = r["to"]
        rel_type = r["relationType"]

        from_entity = await pool.fetchrow(
            "SELECT id FROM kg_entities WHERE name = $1", from_name
        )
        to_entity = await pool.fetchrow(
            "SELECT id FROM kg_entities WHERE name = $1", to_name
        )
        if from_entity is None or to_entity is None:
            continue

        await pool.execute(
            """
            INSERT INTO kg_relations (from_entity_id, to_entity_id, relation_type)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            from_entity["id"], to_entity["id"], rel_type,
        )
        created.append(r)
    return created


async def delete_relations(relations: list[dict]) -> list[dict]:
    """删除关系"""
    pool = await get_pool()
    deleted = []
    for r in relations:
        from_entity = await pool.fetchrow(
            "SELECT id FROM kg_entities WHERE name = $1", r["from"]
        )
        to_entity = await pool.fetchrow(
            "SELECT id FROM kg_entities WHERE name = $1", r["to"]
        )
        if from_entity is None or to_entity is None:
            continue

        result = await pool.execute(
            """
            DELETE FROM kg_relations
            WHERE from_entity_id = $1 AND to_entity_id = $2 AND relation_type = $3
            """,
            from_entity["id"], to_entity["id"], r["relationType"],
        )
        if result == "DELETE 1":
            deleted.append(r)
    return deleted


# ============================================================
# Read Graph
# ============================================================

async def read_graph() -> dict:
    """读取完整图谱"""
    pool = await get_pool()

    entities_rows = await pool.fetch(
        """
        SELECT e.id, e.name, e.entity_type, e.description,
               COALESCE(json_agg(o.content ORDER BY o.id) FILTER (WHERE o.id IS NOT NULL), '[]') AS observations
        FROM kg_entities e
        LEFT JOIN kg_observations o ON o.entity_id = e.id
        GROUP BY e.id
        ORDER BY e.name
        """
    )

    entities = []
    for row in entities_rows:
        entities.append({
            "name": row["name"],
            "entityType": row["entity_type"],
            "description": row["description"] or "",
            "observations": json.loads(row["observations"]),
        })

    relations_rows = await pool.fetch(
        """
        SELECT fe.name AS "from", te.name AS "to", r.relation_type AS "relationType"
        FROM kg_relations r
        JOIN kg_entities fe ON r.from_entity_id = fe.id
        JOIN kg_entities te ON r.to_entity_id = te.id
        ORDER BY r.id
        """
    )

    relations = [dict(r) for r in relations_rows]

    return {"entities": entities, "relations": relations}
