"""CLI 入口 (click)"""

import asyncio
import json
import os
import sys
from importlib.resources import files
from pathlib import Path

import click

from . import __version__


@click.group()
@click.version_option(version=__version__, prog_name="kg-memory-mcp")
def main():
    """Knowledge graph memory + conversation archival MCP server."""


# ============================================================
# serve
# ============================================================

@main.command()
def serve():
    """Start the MCP server (stdio transport)."""
    from .server import mcp
    mcp.run()


# ============================================================
# init
# ============================================================

@main.command()
@click.option("--db-name", envvar="KG_DB_NAME", default="knowledge_base", help="Database name")
@click.option("--db-user", envvar="KG_DB_USER", default="postgres", help="Database user")
@click.option("--db-host", envvar="KG_DB_HOST", default="localhost", help="Database host")
@click.option("--db-port", envvar="KG_DB_PORT", default="5432", help="Database port")
def init(db_name: str, db_user: str, db_host: str, db_port: str):
    """Create tables and indexes (execute schema.sql)."""
    import subprocess

    schema_path = files("kg_memory_mcp").joinpath("schema.sql")

    # 尝试常见 psql 路径
    psql_paths = [
        "psql",
        "/opt/homebrew/opt/postgresql@18/bin/psql",
        "/opt/homebrew/opt/postgresql@17/bin/psql",
        "/usr/local/bin/psql",
    ]

    psql_bin = None
    for p in psql_paths:
        try:
            subprocess.run([p, "--version"], capture_output=True, check=True)
            psql_bin = p
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    if psql_bin is None:
        click.echo("Error: psql not found. Please install PostgreSQL.", err=True)
        sys.exit(1)

    click.echo(f"Running schema.sql on {db_name}@{db_host}:{db_port} ...")
    result = subprocess.run(
        [psql_bin, "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name, "-f", str(schema_path)],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        click.echo(f"Error:\n{result.stderr}", err=True)
        sys.exit(1)

    click.echo("Schema initialized successfully.")


# ============================================================
# migrate
# ============================================================

@main.command()
@click.argument("jsonl_path", type=click.Path(exists=True))
def migrate(jsonl_path: str):
    """Migrate from memory.jsonl (mcp-server-memory format)."""
    from .migrate import migrate as do_migrate
    asyncio.run(do_migrate(jsonl_path))


# ============================================================
# collect
# ============================================================

@main.command()
@click.option("--agent", type=click.Choice(["claude-code", "codex", "gemini-cli"]), help="Only collect from this agent")
def collect(agent: str | None):
    """Collect conversation transcripts from AI agents."""
    asyncio.run(_collect(agent))


async def _collect(agent: str | None):
    from .collector import claude_code, codex, gemini
    from . import db

    os.makedirs(os.path.expanduser("~/.local/share/kg-memory/attachments"), exist_ok=True)

    total_s = total_m = 0

    if agent is None or agent == "claude-code":
        s, m = await claude_code.collect()
        total_s += s
        total_m += m

    if agent is None or agent == "codex":
        s, m = await codex.collect()
        total_s += s
        total_m += m

    if agent is None or agent == "gemini-cli":
        s, m = await gemini.collect()
        total_s += s
        total_m += m

    pool = await db.get_pool()
    session_count = await pool.fetchval("SELECT COUNT(*) FROM chat_sessions")
    message_count = await pool.fetchval("SELECT COUNT(*) FROM chat_messages")
    click.echo(f"\n总计: {total_s} sessions, {total_m} messages imported")
    click.echo(f"  chat_sessions: {session_count}")
    click.echo(f"  chat_messages: {message_count}")

    await db.close_pool()


# ============================================================
# hooks
# ============================================================

@main.group()
def hooks():
    """Manage agent hooks (install, status)."""


HOOK_AGENTS = {
    "claude-code": {
        "settings_path": Path.home() / ".claude" / "settings.json",
        "hook_event": "PostToolUse",  # example, actual is SessionEnd
        "description": "Claude Code SessionEnd auto-memory hook",
    },
    "codex": {
        "settings_path": Path.home() / ".codex" / "config.toml",
        "description": "Codex notify hook for real-time archival",
    },
    "gemini": {
        "settings_path": Path.home() / ".gemini" / "settings.json",
        "description": "Gemini CLI SessionEnd hook",
    },
    "opencode": {
        "settings_path": Path.home() / ".config" / "opencode" / "plugins",
        "description": "OpenCode plugin for conversation archival",
    },
}


@hooks.command("install")
@click.argument("agent", type=click.Choice(list(HOOK_AGENTS.keys())))
def hooks_install(agent: str):
    """Install hook for a specific agent."""
    if agent == "claude-code":
        _install_claude_code_hook()
    elif agent == "codex":
        _install_codex_hook()
    elif agent == "gemini":
        _install_gemini_hook()
    elif agent == "opencode":
        click.echo("OpenCode plugin: not yet implemented. Copy hooks/opencode.ts manually.")


def _install_claude_code_hook():
    """Install Claude Code SessionEnd hook into ~/.claude/settings.json"""
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        with open(settings_path) as f:
            settings = json.load(f)

    hooks_config = settings.setdefault("hooks", {})
    session_end = hooks_config.setdefault("SessionEnd", [])

    hook_entry = {
        "type": "command",
        "command": "kg-memory-mcp hooks run claude-code",
    }

    # 检查是否已安装
    for h in session_end:
        if "kg-memory-mcp" in h.get("command", ""):
            click.echo("Claude Code hook already installed.")
            return

    session_end.append(hook_entry)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    click.echo(f"Installed Claude Code hook → {settings_path}")


def _install_codex_hook():
    """Install Codex notify hook into ~/.codex/config.toml"""
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 检查是否已安装
    if config_path.exists():
        content = config_path.read_text()
        if "kg-memory-mcp" in content:
            click.echo("Codex hook already installed.")
            return

    # 追加 notify 配置
    notify_line = '\nnotify = ["kg-memory-mcp", "hooks", "run", "codex"]\n'
    with open(config_path, "a") as f:
        f.write(notify_line)

    click.echo(f"Installed Codex hook → {config_path}")


def _install_gemini_hook():
    """Install Gemini CLI SessionEnd hook into ~/.gemini/settings.json"""
    settings_path = Path.home() / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        with open(settings_path) as f:
            settings = json.load(f)

    hooks_config = settings.setdefault("hooks", {})
    session_end = hooks_config.setdefault("SessionEnd", [])

    hook_entry = {
        "type": "command",
        "command": "kg-memory-mcp hooks run gemini",
    }

    for h in session_end:
        if "kg-memory-mcp" in h.get("command", ""):
            click.echo("Gemini hook already installed.")
            return

    session_end.append(hook_entry)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    click.echo(f"Installed Gemini hook → {settings_path}")


@hooks.command("run")
@click.argument("agent", type=click.Choice(["claude-code", "codex", "gemini"]))
def hooks_run(agent: str):
    """Run a hook directly (called by agent integrations)."""
    if agent == "claude-code":
        from .hooks.claude_code import main as hook_main
    elif agent == "codex":
        from .hooks.codex import main as hook_main
    elif agent == "gemini":
        from .hooks.gemini import main as hook_main
    else:
        click.echo(f"Unknown agent: {agent}", err=True)
        sys.exit(1)

    hook_main()


@hooks.command("status")
def hooks_status():
    """Check installation status of all hooks."""
    for agent, config in HOOK_AGENTS.items():
        settings_path = config["settings_path"]
        installed = False

        if settings_path.exists():
            try:
                if agent == "codex":
                    content = settings_path.read_text()
                    installed = "kg-memory-mcp" in content
                elif agent == "opencode":
                    installed = any(settings_path.iterdir()) if settings_path.is_dir() else False
                else:
                    with open(settings_path) as f:
                        data = json.load(f)
                    hooks_data = data.get("hooks", {})
                    for event_hooks in hooks_data.values():
                        for h in event_hooks:
                            if "kg-memory-mcp" in h.get("command", ""):
                                installed = True
                                break
            except Exception:
                pass

        status = "installed" if installed else "not installed"
        click.echo(f"  {agent:15s} {status:15s} ({settings_path})")
