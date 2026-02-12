"""memory.jsonl → PostgreSQL 迁移脚本"""

import json
import re

import click

from .db import close_pool, get_pool
from .embedding import close as close_embedding
from .embedding import get_embedding
from .quality import contains_sensitive


def parse_jsonl(path: str) -> tuple[list[dict], list[dict]]:
    """解析 memory.jsonl，返回 (entities, relations)"""
    entities = []
    relations = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record["type"] == "entity":
                entities.append(record)
            elif record["type"] == "relation":
                relations.append(record)
    return entities, relations


def split_super_entity(entity: dict, threshold: int = 10) -> list[dict]:
    """拆分超级节点

    规则: 如果 observations 中有 === xxx === 格式的分隔符，
    按分隔符拆分成子实体 (parent/section_name)
    """
    observations = entity.get("observations", [])
    if len(observations) <= threshold:
        return [entity]

    sections: dict[str, list[str]] = {}
    current_section = "general"

    for obs in observations:
        m = re.match(r"^===\s*(.+?)\s*===$", obs)
        if m:
            current_section = m.group(1).strip()
            continue
        if current_section not in sections:
            sections[current_section] = []
        sections[current_section].append(obs)

    if len(sections) <= 1:
        return [entity]

    result = []
    parent_name = entity["name"]
    entity_type = entity.get("entityType", "Topic")

    for section_name, obs_list in sections.items():
        sub_name = f"{parent_name}/{section_name}"
        result.append({
            "name": sub_name,
            "entityType": entity_type,
            "observations": obs_list,
            "description": f"{parent_name} - {section_name}",
        })

    return result


async def migrate(jsonl_path: str):
    click.echo(f"Reading {jsonl_path} ...")
    entities_raw, relations_raw = parse_jsonl(jsonl_path)
    click.echo(f"  Raw: {len(entities_raw)} entities, {len(relations_raw)} relations")

    # 拆分超级节点
    entities_split = []
    name_mapping: dict[str, list[str]] = {}

    for e in entities_raw:
        parts = split_super_entity(e)
        new_names = [p["name"] for p in parts]
        name_mapping[e["name"]] = new_names
        entities_split.extend(parts)

    total_obs = sum(len(e.get("observations", [])) for e in entities_split)
    click.echo(f"  After split: {len(entities_split)} entities, {total_obs} observations")

    pool = await get_pool()

    # 写入实体 + observations
    for i, e in enumerate(entities_split, 1):
        name = e["name"]
        entity_type = e.get("entityType", "Topic")
        description = e.get("description", "")
        observations = e.get("observations", [])

        click.echo(f"  [{i}/{len(entities_split)}] {name} ({len(observations)} obs) ...", nl=False)

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
            RETURNING id
            """,
            name, entity_type, description, emb,
        )
        entity_id = row["id"]

        for obs in observations:
            if contains_sensitive(obs):
                continue
            obs_emb = await get_embedding(obs)
            await pool.execute(
                """
                INSERT INTO kg_observations (entity_id, content, embedding, source_agent)
                VALUES ($1, $2, $3, 'migration')
                ON CONFLICT (entity_id, content_hash) DO NOTHING
                """,
                entity_id, obs, obs_emb,
            )

        click.echo(" done")

    # 写入关系
    click.echo("\nWriting relations ...")
    rel_count = 0
    for r in relations_raw:
        from_name = r["from"]
        to_name = r["to"]
        rel_type = r["relationType"]

        from_names = name_mapping.get(from_name, [from_name])
        to_names = name_mapping.get(to_name, [to_name])

        for fn in from_names:
            for tn in to_names:
                from_row = await pool.fetchrow(
                    "SELECT id FROM kg_entities WHERE name = $1", fn
                )
                to_row = await pool.fetchrow(
                    "SELECT id FROM kg_entities WHERE name = $1", tn
                )
                if from_row and to_row:
                    await pool.execute(
                        """
                        INSERT INTO kg_relations (from_entity_id, to_entity_id, relation_type)
                        VALUES ($1, $2, $3)
                        ON CONFLICT DO NOTHING
                        """,
                        from_row["id"], to_row["id"], rel_type,
                    )
                    rel_count += 1

    click.echo(f"  Wrote {rel_count} relations")

    # 验证
    entity_count = await pool.fetchval("SELECT COUNT(*) FROM kg_entities")
    obs_count = await pool.fetchval("SELECT COUNT(*) FROM kg_observations")
    rel_count_db = await pool.fetchval("SELECT COUNT(*) FROM kg_relations")
    click.echo("\nMigration complete:")
    click.echo(f"  kg_entities: {entity_count}")
    click.echo(f"  kg_observations: {obs_count}")
    click.echo(f"  kg_relations: {rel_count_db}")

    await close_pool()
    await close_embedding()
