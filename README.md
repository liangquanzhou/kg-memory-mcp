# kg-memory-mcp

**Knowledge graph memory + conversation archival MCP server**

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) server backed by PostgreSQL + pgvector. It provides persistent knowledge graph storage (entities, observations, relations) and multi-agent conversation archival with hybrid search -- replacing both `mcp-server-memory` (JSONL) and cloud-based memory services.

## Features

- **Knowledge Graph** -- Create entities with typed observations and directional relations. Automatic deduplication (hash + semantic) and sensitive content filtering.
- **Conversation Archival** -- Collect and store chat transcripts from Claude Code, Codex CLI, and Gemini CLI. Full session history with metadata.
- **Hybrid Search** -- Full-text search (PostgreSQL tsvector) combined with vector similarity (pgvector HNSW), fused via Reciprocal Rank Fusion (RRF) with 1-hop graph expansion.
- **Hook System** -- Automatic post-session archival and knowledge extraction. Install hooks for supported agents with a single CLI command.
- **Local Embeddings** -- Uses Ollama with bge-m3 (1024-dim) for all vector operations. No data leaves your machine.
- **Schema Migrations** -- Lightweight numbered-SQL migration system. Safely upgrades existing databases without data loss.
- **Data Export** -- Export to JSONL (human-readable, interoperable) or SQLite (single-file backup). Compatible with migration to other tools.
- **Image Archival** -- Extracts base64 images from Claude Code transcripts to the local filesystem with metadata tracking.

## Architecture

```
+------------------+     stdio      +-------------------+
|   MCP Client     |<-------------->|  kg-memory-mcp    |
|  (Claude Code,   |                |  (FastMCP server) |
|   Codex, Gemini) |                +--------+----------+
+------------------+                         |
                                             | asyncpg
                                    +--------v----------+
                                    |   PostgreSQL 17+   |
                                    |   + pgvector       |
                                    |                    |
                                    | kg_entities        |
                                    | kg_observations    |
                                    | kg_relations       |
                                    | chat_sessions      |
                                    | chat_messages      |
                                    +--------+----------+
                                             |
+------------------+                +--------v----------+
|  Agent Hooks     |  SessionEnd    |   Ollama           |
|  (auto-archival  |--------------->|   bge-m3           |
|   + extraction)  |                |   (embeddings)     |
+------------------+                +-------------------+
```

## Quick Start

### Prerequisites

- PostgreSQL 17+ with [pgvector](https://github.com/pgvector/pgvector) extension
- [Ollama](https://ollama.com/) with the `bge-m3` model pulled
- Python 3.11+

### Install

```bash
# Via pip
pip install kg-memory-mcp

# Or run directly with uvx (no install needed)
uvx kg-memory-mcp serve
```

### Initialize the Database

```bash
# Create the knowledge_base database first
createdb knowledge_base

# Pull the embedding model
ollama pull bge-m3

# Run schema migrations
kg-memory-mcp init
```

### Configure Your MCP Client

Add the following to your MCP client configuration:

```json
{
  "mcpServers": {
    "kg-memory": {
      "command": "uvx",
      "args": ["kg-memory-mcp", "serve"]
    }
  }
}
```

**Claude Code** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "kg-memory": {
      "command": "uvx",
      "args": ["kg-memory-mcp", "serve"]
    }
  }
}
```

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.kg-memory]
command = "uvx"
args = ["kg-memory-mcp", "serve"]
```

**Gemini CLI** (`~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "kg-memory": {
      "command": "uvx",
      "args": ["kg-memory-mcp", "serve"]
    }
  }
}
```

## MCP Tools

### Knowledge Graph

| Tool | Description |
|------|-------------|
| `create_entities` | Create entities with name, type, description, and optional observations |
| `add_observations` | Add observations to an entity (auto-dedup + sensitive filter) |
| `create_relations` | Create directional relations between entities |
| `delete_entities` | Delete entities (cascades to observations and relations) |
| `delete_observations` | Delete specific observations from an entity |
| `delete_relations` | Delete specific relations |
| `search_nodes` | Hybrid FTS + vector search with 1-hop graph expansion |
| `read_graph` | Read the entire knowledge graph |

### Conversation Archival

| Tool | Description |
|------|-------------|
| `search_chats` | Search chat messages across all agents |
| `get_session` | Get a complete chat session with all messages |
| `list_sessions` | List chat sessions with optional agent filter |

## CLI Reference

```
kg-memory-mcp [OPTIONS] COMMAND [ARGS]

Commands:
  serve                  Start the MCP server (stdio transport)
  init                   Run schema migrations (create/upgrade tables)
  migrate JSONL_PATH     Migrate from memory.jsonl (mcp-server-memory format)
  collect                Collect conversation transcripts from AI agents
    --agent TEXT          Only collect from: claude-code, codex, gemini-cli
  export jsonl           Export all data to JSONL files
    --output-dir PATH    Output directory (default: ./export)
  export sqlite          Export all data to a single SQLite file
    --output PATH        Output file path (default: ./kg-memory-backup.db)
  reset                  Drop all kg-memory-mcp tables (with confirmation)
  hooks install AGENT    Install hook for a specific agent
  hooks uninstall AGENT  Remove hook for a specific agent
  hooks status           Check installation status of all hooks
  hooks run AGENT        Run a hook directly (called by agent integrations)
```

### Examples

```bash
# Start the MCP server
kg-memory-mcp serve

# Initialize database schema
kg-memory-mcp init --db-name knowledge_base --db-user postgres

# Migrate from mcp-server-memory
kg-memory-mcp migrate ~/.claude/memory.jsonl

# Collect all agent transcripts
kg-memory-mcp collect

# Collect only Claude Code transcripts
kg-memory-mcp collect --agent claude-code

# Install auto-archival hook for Claude Code
kg-memory-mcp hooks install claude-code

# Uninstall a hook
kg-memory-mcp hooks uninstall claude-code

# Check hook status
kg-memory-mcp hooks status

# Export data to JSONL (for migration to other tools)
kg-memory-mcp export jsonl --output-dir ./my-export

# Export data to SQLite (single-file backup)
kg-memory-mcp export sqlite --output ./backup.db
```

## Supported Agents

| Agent | Collector | Hook | Transcript Location |
|-------|-----------|------|---------------------|
| Claude Code | Yes | SessionEnd | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | Yes | notify (agent-turn-complete) | `~/.codex/sessions/**/*.jsonl` |
| Gemini CLI | Yes | SessionEnd | `~/.gemini/tmp/*/chats/session-*.json` |
| OpenCode | Yes | session.idle (plugin) | `~/.local/share/opencode/storage/` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KG_DB_NAME` | `knowledge_base` | PostgreSQL database name |
| `KG_DB_USER` | `postgres` | PostgreSQL user |
| `KG_DB_HOST` | `localhost` | PostgreSQL host |
| `KG_DB_PORT` | `5432` | PostgreSQL port |
| `KG_DB_PASSWORD` | *(none)* | PostgreSQL password (if required) |
| `KG_DB_SSL` | *(disabled)* | Set to `require` to enable SSL for remote DB connections |
| `KG_CHAT_SANITIZE` | *(disabled)* | Set to `true` to filter messages containing secrets before archival |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API base URL |
| `OLLAMA_EMBED_MODEL` | `bge-m3` | Ollama embedding model name |
| `ATTACHMENT_DIR` | `~/.local/share/kg-memory/attachments` | Directory for storing image attachments |

## Privacy & Security

**All data stays local by default.** The MCP server, database, and embeddings run entirely on your machine. No data is sent to external services unless you explicitly opt in.

- **Database storage**: Conversation transcripts and knowledge graph data are stored in plaintext in your local PostgreSQL database. Ensure your database has appropriate access controls.
- **Ollama embeddings**: Vector embeddings are generated locally via Ollama. No data leaves your machine for embedding generation.
- **Gemini knowledge extraction** (opt-in): If you set the `GEMINI_API_KEY` environment variable, the SessionEnd hooks will send conversation summaries (up to 15KB) to Google's Gemini API for knowledge extraction. This is **disabled by default** -- without the API key, no data is sent externally. Conversation content is automatically filtered to remove lines containing API keys, passwords, and tokens before transmission, but project paths and code snippets may still be included.
- **Hook transcript access**: Hooks only read transcript files from expected directories (`~/.claude/`, `~/.codex/`, `~/.gemini/`). Path traversal is validated.
- **Sensitive content filtering**: The `quality.py` module filters out API keys, passwords, and tokens when writing to the knowledge graph via MCP tools and hooks. Note: bulk operations like `migrate` and `collect` also apply this filter.

## Uninstall

```bash
# 1. Remove hooks from all agents
kg-memory-mcp hooks uninstall claude-code
kg-memory-mcp hooks uninstall codex
kg-memory-mcp hooks uninstall gemini

# 2. (Optional) Drop all tables from the database
kg-memory-mcp reset

# 3. Remove the MCP server config from your client
#    Delete the "kg-memory" entry from mcpServers in your settings

# 4. Uninstall the package
pip uninstall kg-memory-mcp    # if installed via pip
uv tool uninstall kg-memory-mcp  # if installed via uv
```

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/liangquanzhou/kg-memory-mcp.git
cd kg-memory-mcp
pip install -e ".[dev]"

# Run linting
ruff check kg_memory_mcp/
pyright kg_memory_mcp/

# Run tests (requires PostgreSQL with pgvector)
pytest tests/ -v
```

## License

MIT
