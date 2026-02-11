"""搜索模块：FTS + 向量 + RRF 融合 + 1-hop 图扩展"""

import json

from .db import get_pool
from .embedding import get_embedding


async def search(query: str, limit: int = 10) -> dict:
    """混合搜索：FTS + 向量 + RRF 排序 + 1-hop 图扩展

    返回格式兼容 mcp-server-memory 的 search 输出:
    {entities: [{name, entityType, description, observations}], relations: [...]}
    """
    pool = await get_pool()
    query_embedding = await get_embedding(query)

    # 1. FTS 召回
    fts_rows = await pool.fetch(
        """
        SELECT o.entity_id, o.id AS obs_id,
               ts_rank(o.search_vector, plainto_tsquery('simple', $1)) AS score
        FROM kg_observations o
        WHERE o.search_vector @@ plainto_tsquery('simple', $1)
        ORDER BY score DESC
        LIMIT $2
        """,
        query, limit * 3,
    )

    # 2. 向量召回
    vec_rows = await pool.fetch(
        """
        SELECT o.entity_id, o.id AS obs_id,
               1 - (o.embedding <=> $1::vector) AS score
        FROM kg_observations o
        WHERE o.embedding IS NOT NULL
        ORDER BY o.embedding <=> $1::vector
        LIMIT $2
        """,
        query_embedding, limit * 3,
    )

    # 3. RRF 融合 (k=60)
    k = 60
    rrf_scores: dict[int, float] = {}  # entity_id -> score

    for rank, row in enumerate(fts_rows):
        eid = row["entity_id"]
        rrf_scores[eid] = rrf_scores.get(eid, 0) + 1.0 / (k + rank + 1)

    for rank, row in enumerate(vec_rows):
        eid = row["entity_id"]
        rrf_scores[eid] = rrf_scores.get(eid, 0) + 1.0 / (k + rank + 1)

    # 排序取 top entity_ids
    sorted_entities = sorted(rrf_scores.items(), key=lambda x: -x[1])[:limit]
    top_entity_ids = [eid for eid, _ in sorted_entities]

    if not top_entity_ids:
        return {"entities": [], "relations": []}

    # 4. 1-hop 图扩展：拉命中实体的邻居
    neighbor_ids = await pool.fetch(
        """
        SELECT DISTINCT CASE
            WHEN from_entity_id = ANY($1) THEN to_entity_id
            ELSE from_entity_id
        END AS neighbor_id
        FROM kg_relations
        WHERE from_entity_id = ANY($1) OR to_entity_id = ANY($1)
        """,
        top_entity_ids,
    )
    all_entity_ids = list(set(top_entity_ids + [r["neighbor_id"] for r in neighbor_ids]))

    # 5. 拉取完整实体 + observations
    entities_rows = await pool.fetch(
        """
        SELECT e.id, e.name, e.entity_type, e.description,
               COALESCE(json_agg(o.content ORDER BY o.id) FILTER (WHERE o.id IS NOT NULL), '[]') AS observations
        FROM kg_entities e
        LEFT JOIN kg_observations o ON o.entity_id = e.id
        WHERE e.id = ANY($1)
        GROUP BY e.id
        """,
        all_entity_ids,
    )

    # 排序：直接命中的排前面，邻居排后面
    top_set = set(top_entity_ids)
    entities = []
    for row in sorted(entities_rows, key=lambda r: (r["id"] not in top_set, r["name"])):
        entities.append({
            "name": row["name"],
            "entityType": row["entity_type"],
            "description": row["description"] or "",
            "observations": json.loads(row["observations"]),
        })

    # 6. 拉取相关关系
    relations_rows = await pool.fetch(
        """
        SELECT fe.name AS "from", te.name AS "to", r.relation_type AS "relationType"
        FROM kg_relations r
        JOIN kg_entities fe ON r.from_entity_id = fe.id
        JOIN kg_entities te ON r.to_entity_id = te.id
        WHERE r.from_entity_id = ANY($1) OR r.to_entity_id = ANY($1)
        """,
        all_entity_ids,
    )

    relations = [dict(r) for r in relations_rows]

    return {"entities": entities, "relations": relations}
