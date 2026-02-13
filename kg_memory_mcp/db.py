"""知识图谱数据库操作层 (asyncpg)"""

import asyncio
import hashlib
import json
import os

import asyncpg
from pgvector.asyncpg import register_vector

from .embedding import get_embedding, get_embeddings
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
            max_size=max(2, int(os.getenv("KG_DB_POOL_SIZE", "10"))),
            command_timeout=60,
            timeout=10,
            init=register_vector,
        )
        password = os.getenv("KG_DB_PASSWORD")
        if password:
            kwargs["password"] = password
        ssl_mode = os.getenv("KG_DB_SSL", "")
        if ssl_mode and ssl_mode.lower() not in ("disable", "false", "0"):
            kwargs["ssl"] = True
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

    # 批量生成 embedding（1 次 Ollama 调用代替 N 次）
    embed_texts = [
        f"{e['name']}: {e.get('description', '')}" if e.get("description") else e["name"]
        for e in entities
    ]
    embeddings = await get_embeddings(embed_texts)

    # 批量写入 entity（单连接 + 事务保证原子性）
    results = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for e, emb in zip(entities, embeddings):
                name = e["name"]
                entity_type = e["entityType"]
                description = e.get("description", "")

                row = await conn.fetchrow(
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
                results.append({"name": row["name"], "id": row["id"]})

    # observations 在事务外处理（各自独立事务）
    for e in entities:
        observations = e.get("observations", [])
        if observations:
            await add_observations(e["name"], observations, source_agent=None)

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

    # 1. 过滤敏感信息
    safe_observations = [o for o in observations if not contains_sensitive(o)]
    if not safe_observations:
        return []

    # 2. Hash 去重（无需 embedding，快速过滤）
    candidates = []
    for content in safe_observations:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if not await is_duplicate_hash(pool, entity_id, content_hash):
            candidates.append(content)
    if not candidates:
        return []

    # 3. 批量生成 embedding（1 次 Ollama 调用代替 N 次）
    embeddings = await get_embeddings(candidates)

    # 4. 向量去重 + 写入（单连接 + 事务保证原子性）
    added = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for content, emb in zip(candidates, embeddings):
                if await is_duplicate_semantic(conn, entity_id, emb):
                    continue

                await conn.execute(
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

async def read_graph(limit: int = 100, offset: int = 0) -> dict:
    """读取图谱（分页），返回当前页 entity + 相关 relation + 总数"""
    pool = await get_pool()

    total: int = await pool.fetchval("SELECT COUNT(*) FROM kg_entities") or 0

    entities_rows = await pool.fetch(
        """
        SELECT e.id, e.name, e.entity_type, e.description,
               COALESCE(json_agg(o.content ORDER BY o.id) FILTER (WHERE o.id IS NOT NULL), '[]') AS observations
        FROM kg_entities e
        LEFT JOIN kg_observations o ON o.entity_id = e.id
        GROUP BY e.id
        ORDER BY e.name
        LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )

    entities = []
    entity_ids = []
    for row in entities_rows:
        obs = row["observations"]
        entities.append({
            "name": row["name"],
            "entityType": row["entity_type"],
            "description": row["description"] or "",
            "observations": obs if isinstance(obs, list) else json.loads(obs),
        })
        entity_ids.append(row["id"])

    entity_names = {e["name"] for e in entities}

    if entity_ids:
        relations_rows = await pool.fetch(
            """
            SELECT fe.name AS "from", te.name AS "to", r.relation_type AS "relationType"
            FROM kg_relations r
            JOIN kg_entities fe ON r.from_entity_id = fe.id
            JOIN kg_entities te ON r.to_entity_id = te.id
            WHERE r.from_entity_id = ANY($1::int[]) OR r.to_entity_id = ANY($1::int[])
            ORDER BY r.id
            """,
            entity_ids,
        )
        relations = []
        for r in relations_rows:
            rel = dict(r)
            # Mark if either endpoint is outside the current page
            if rel["from"] not in entity_names or rel["to"] not in entity_names:
                rel["cross_page"] = True
            relations.append(rel)
    else:
        relations = []

    return {
        "entities": entities,
        "relations": relations,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
