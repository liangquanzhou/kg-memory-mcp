# Hook System

kg-memory-mcp provides agent hooks that automatically archive conversations and extract knowledge into the PostgreSQL knowledge graph when a session ends.

## What Hooks Do

When an agent session ends, the hook:

1. **Archives the conversation** -- Reads the session transcript, parses messages, and stores them in `chat_sessions` + `chat_messages` tables. Deduplication prevents double-imports.
2. **Extracts knowledge** (optional) -- For conversations with enough substance (3+ user turns), uses Gemini API to extract user preferences, project decisions, solutions, and learned facts. These are saved as observations on knowledge graph entities.

## Supported Agents

| Agent | Hook Type | Trigger Event | Data Source |
|-------|-----------|---------------|-------------|
| Claude Code | SessionEnd (stdin JSON) | Session ends | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | notify (argv JSON) | agent-turn-complete | `~/.codex/sessions/**/*.jsonl` |
| Gemini CLI | SessionEnd (stdin JSON) | Session ends | `~/.gemini/tmp/*/chats/session-*.json` |
| OpenCode | Plugin (not yet implemented) | -- | -- |

## Installation

### CLI Installation (Recommended)

The easiest way to install hooks is via the CLI:

```bash
# Install hook for a specific agent
kg-memory-mcp hooks install claude-code
kg-memory-mcp hooks install codex
kg-memory-mcp hooks install gemini

# Verify installation status
kg-memory-mcp hooks status
```

### Manual Installation

#### Claude Code

Add the following to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "type": "command",
        "command": "kg-memory-mcp hooks run claude-code"
      }
    ]
  }
}
```

Claude Code passes session data via stdin as JSON with fields:
- `transcript_path` -- Path to the JSONL transcript file
- `session_id` -- The session identifier
- `cwd` -- Working directory of the session
- `reason` -- Why the session ended

#### Codex CLI

Add the following to `~/.codex/config.toml`:

```toml
notify = ["kg-memory-mcp", "hooks", "run", "codex"]
```

Codex passes event data as a JSON string in `sys.argv[1]` with:
- `type` -- Event type (hook only processes `agent-turn-complete`)
- `cwd` -- Working directory

The hook finds the most recently modified session file in `~/.codex/sessions/`.

#### Gemini CLI

Add the following to `~/.gemini/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "type": "command",
        "command": "kg-memory-mcp hooks run gemini"
      }
    ]
  }
}
```

Gemini CLI passes session data via stdin as JSON. The hook finds the most recently modified session file in `~/.gemini/tmp/*/chats/`.

#### OpenCode

OpenCode plugin support is not yet implemented. Manual integration is required.

## Configuration

Hooks use the same environment variables as the main server:

| Variable | Default | Description |
|----------|---------|-------------|
| `KG_DB_NAME` | `knowledge_base` | PostgreSQL database name |
| `KG_DB_USER` | `postgres` | PostgreSQL user |
| `KG_DB_HOST` | `localhost` | PostgreSQL host |
| `KG_DB_PORT` | `5432` | PostgreSQL port |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL (for embeddings) |
| `OLLAMA_EMBED_MODEL` | `bge-m3` | Embedding model name |
| `GEMINI_API_KEY` | (empty) | Gemini API key for knowledge extraction (optional) |

If `GEMINI_API_KEY` is not set, hooks will still archive conversations but skip the knowledge extraction step.

## Running Hooks Manually

You can trigger hooks directly for testing:

```bash
# Claude Code (reads from stdin)
echo '{"transcript_path": "/path/to/session.jsonl", "session_id": "abc", "cwd": "/tmp"}' | kg-memory-mcp hooks run claude-code

# Codex (reads from argv)
kg-memory-mcp hooks run codex '{"type": "agent-turn-complete", "cwd": "/tmp"}'

# Gemini (reads from stdin)
echo '{"reason": "manual"}' | kg-memory-mcp hooks run gemini
```

## Troubleshooting

### Check Logs

All hooks write logs to `~/.claude/hooks/`:

```bash
# Claude Code hook log
tail -f ~/.claude/hooks/auto-memory.log

# Codex hook log
tail -f ~/.claude/hooks/codex-notify.log

# Gemini hook log
tail -f ~/.claude/hooks/gemini-session-end.log
```

### Common Issues

**Hook not triggering**

- Run `kg-memory-mcp hooks status` to verify installation.
- Make sure `kg-memory-mcp` is on your PATH (or installed via `pip install kg-memory-mcp`).

**Database connection failed**

- Verify PostgreSQL is running and accessible.
- Check that the `KG_DB_*` environment variables are correct.
- Hooks use short-lived connections (not a pool), so connection timeouts are set to 10 seconds.

**No knowledge extracted**

- Knowledge extraction requires `GEMINI_API_KEY` to be set.
- Conversations shorter than 3 user turns are skipped.
- Conversations with less than 200 characters of formatted content are skipped.

**Embedding errors**

- Verify Ollama is running: `curl http://localhost:11434/api/tags`
- Verify the model is pulled: `ollama pull bge-m3`
- If embeddings fail, hooks continue without them (observations are stored without vectors).
