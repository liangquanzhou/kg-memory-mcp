"""kg-memory-mcp: 知识图谱 + 对话存档 MCP Server"""

import json

from mcp.server.fastmcp import FastMCP

from . import chat_db, db
from . import search as search_mod

mcp = FastMCP("kg-memory", log_level="WARNING")


# ============================================================
# 知识图谱工具 (兼容 mcp-server-memory)
# ============================================================

@mcp.tool()
async def create_entities(entities: list[dict]) -> str:
    """Create new entities in the knowledge graph.

    Each entity dict should have: name (str), entityType (str),
    and optionally: observations (list[str]), description (str).
    """
    results = await db.create_entities(entities)
    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
async def add_observations(entityName: str, observations: list[str], sourceAgent: str | None = None) -> str:
    """Add observations to an existing entity. Automatically deduplicates and filters sensitive content."""
    added = await db.add_observations(entityName, observations, source_agent=sourceAgent)
    return json.dumps({"added": added, "count": len(added)}, ensure_ascii=False)


@mcp.tool()
async def create_relations(relations: list[dict]) -> str:
    """Create relations between entities.

    Each relation dict should have: from (str), to (str), relationType (str).
    """
    created = await db.create_relations(relations)
    return json.dumps(created, ensure_ascii=False)


@mcp.tool()
async def delete_entities(names: list[str]) -> str:
    """Delete entities by name (cascades to observations and relations)."""
    deleted = await db.delete_entities(names)
    return json.dumps({"deleted": deleted}, ensure_ascii=False)


@mcp.tool()
async def delete_observations(entityName: str, observations: list[str]) -> str:
    """Delete specific observations from an entity."""
    deleted = await db.delete_observations(entityName, observations)
    return json.dumps({"deleted": deleted}, ensure_ascii=False)


@mcp.tool()
async def delete_relations(relations: list[dict]) -> str:
    """Delete specific relations.

    Each relation dict should have: from (str), to (str), relationType (str).
    """
    deleted = await db.delete_relations(relations)
    return json.dumps({"deleted": deleted}, ensure_ascii=False)


@mcp.tool()
async def search_nodes(query: str, limit: int = 10) -> str:
    """Search the knowledge graph using hybrid FTS + vector search with 1-hop graph expansion."""
    query = query[:2000]
    limit = min(max(limit, 1), 100)
    results = await search_mod.search(query, limit=limit)
    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
async def read_graph() -> str:
    """Read the entire knowledge graph (all entities, observations, and relations)."""
    graph = await db.read_graph()
    return json.dumps(graph, ensure_ascii=False)


# ============================================================
# 对话存档工具
# ============================================================

@mcp.tool()
async def search_chats(query: str, agent: str | None = None, limit: int = 20) -> str:
    """Search historical chat messages across all agents.

    Args:
        query: Search keyword
        agent: Filter by agent name (e.g. 'claude-code', 'codex', 'gemini-cli')
        limit: Max results (default 20)
    """
    query = query[:2000]
    limit = min(max(limit, 1), 100)
    results = await chat_db.search_chats(query, agent=agent, limit=limit)
    # 序列化 datetime
    for r in results:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
async def get_session(
    sessionId: int | None = None,
    nativeSessionId: str | None = None,
    agent: str | None = None,
) -> str:
    """Get a complete chat session with all messages.

    Args:
        sessionId: Database session ID
        nativeSessionId: Agent's native session ID (e.g. ses_xxx for Claude Code)
        agent: Agent name (recommended when using nativeSessionId to avoid ambiguity)
    """
    result = await chat_db.get_session(session_id=sessionId, native_session_id=nativeSessionId, agent=agent)
    if result is None:
        return json.dumps({"error": "Session not found"})
    # 序列化 datetime
    for key in ("started_at", "ended_at", "created_at"):
        if result.get(key):
            result[key] = result[key].isoformat()
    for m in result.get("messages", []):
        if m.get("created_at"):
            m["created_at"] = m["created_at"].isoformat()
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_sessions(agent: str | None = None, limit: int = 20, offset: int = 0) -> str:
    """List chat sessions.

    Args:
        agent: Filter by agent name
        limit: Max results (default 20)
        offset: Pagination offset
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    results = await chat_db.list_sessions(agent=agent, limit=limit, offset=offset)
    for r in results:
        for key in ("started_at", "ended_at"):
            if r.get(key):
                r[key] = r[key].isoformat()
    return json.dumps(results, ensure_ascii=False)
