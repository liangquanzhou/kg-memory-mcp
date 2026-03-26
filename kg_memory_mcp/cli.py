"""CLI 入口 (click)"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import click

from . import __version__


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入 JSON 文件：先写临时文件再 rename，防中断导致半写"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


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
# psql helpers
# ============================================================

def _find_psql() -> str:
    """Find psql binary, exit if not found."""
    from .psql import find_psql
    return find_psql()


def _psql_env(db_password: str) -> dict[str, str]:
    """Build env dict with PGPASSWORD if password is provided."""
    env = os.environ.copy()
    if db_password:
        env["PGPASSWORD"] = db_password
    return env


# ============================================================
# init
# ============================================================

@main.command()
@click.option("--db-name", envvar="KG_DB_NAME", default="knowledge_base", help="Database name")
@click.option("--db-user", envvar="KG_DB_USER", default="didi", help="Database user")
@click.option("--db-host", envvar="KG_DB_HOST", default="localhost", help="Database host")
@click.option("--db-port", envvar="KG_DB_PORT", default="5432", help="Database port")
@click.option("--db-password", envvar="KG_DB_PASSWORD", default="", help="Database password")
def init(db_name: str, db_user: str, db_host: str, db_port: str, db_password: str):
    """Run schema migrations (create/upgrade tables)."""
    from .migrations.runner import run_migrations

    click.echo(f"Running migrations on {db_name}@{db_host}:{db_port} ...")
    dsn = {"dbname": db_name, "user": db_user, "host": db_host, "port": db_port, "password": db_password}
    version = run_migrations(dsn)
    click.echo(f"Schema version: v{version}")


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
# export
# ============================================================

@main.group()
def export():
    """Export data to JSONL or SQLite format."""


@export.command("jsonl")
@click.option("--output-dir", default="./export", help="Output directory for JSONL files")
def export_jsonl(output_dir: str):
    """Export all data to JSONL files."""
    asyncio.run(_export_jsonl(output_dir))


async def _export_jsonl(output_dir: str):
    from . import db
    from .export import export_jsonl as do_export
    counts = await do_export(output_dir)
    click.echo(f"\nExported to {output_dir}/")
    for table, count in counts.items():
        click.echo(f"  {table}: {count}")
    await db.close_pool()


@export.command("sqlite")
@click.option("--output", default="./kg-memory-backup.db", help="Output SQLite file path")
def export_sqlite(output: str):
    """Export all data to a single SQLite file."""
    asyncio.run(_export_sqlite(output))


async def _export_sqlite(output: str):
    from . import db
    from .export import export_sqlite as do_export
    counts = await do_export(output)
    click.echo(f"\nExported to {output}")
    for table, count in counts.items():
        click.echo(f"  {table}: {count}")
    await db.close_pool()


# ============================================================
# collect
# ============================================================

@main.command()
@click.option(
    "--agent", type=click.Choice(["claude-code", "codex", "gemini-cli", "opencode"]),
    help="Only collect from this agent",
)
def collect(agent: str | None):
    """Collect conversation transcripts from AI agents."""
    asyncio.run(_collect(agent))


async def _collect(agent: str | None):
    from . import db
    from .collector import claude_code, codex, gemini, opencode

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

    if agent is None or agent == "opencode":
        s, m = await opencode.collect()
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
# reset
# ============================================================

@main.command()
@click.option("--db-name", envvar="KG_DB_NAME", default="knowledge_base", help="Database name")
@click.option("--db-user", envvar="KG_DB_USER", default="didi", help="Database user")
@click.option("--db-host", envvar="KG_DB_HOST", default="localhost", help="Database host")
@click.option("--db-port", envvar="KG_DB_PORT", default="5432", help="Database port")
@click.option("--db-password", envvar="KG_DB_PASSWORD", default="", help="Database password")
@click.confirmation_option(prompt="This will DROP all kg-memory-mcp tables. Are you sure?")
def reset(db_name: str, db_user: str, db_host: str, db_port: str, db_password: str):
    """Drop all kg-memory-mcp tables from the database."""
    import subprocess

    psql_bin = _find_psql()

    drop_sql = (
        "DROP TABLE IF EXISTS chat_attachments CASCADE; "
        "DROP TABLE IF EXISTS chat_messages CASCADE; "
        "DROP TABLE IF EXISTS chat_sessions CASCADE; "
        "DROP TABLE IF EXISTS kg_relations CASCADE; "
        "DROP TABLE IF EXISTS kg_observations CASCADE; "
        "DROP TABLE IF EXISTS kg_entities CASCADE; "
        "DROP TABLE IF EXISTS schema_version CASCADE;"
    )

    click.echo(f"Dropping all kg-memory-mcp tables from {db_name}@{db_host}:{db_port} ...")
    result = subprocess.run(
        [psql_bin, "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name, "-c", drop_sql],
        capture_output=True, text=True, env=_psql_env(db_password),
    )

    if result.returncode != 0:
        click.echo(f"Error:\n{result.stderr}", err=True)
        sys.exit(1)

    click.echo("All kg-memory-mcp tables dropped successfully.")


# ============================================================
# hooks
# ============================================================

@main.group()
def hooks():
    """Manage agent hooks (install, uninstall, status)."""


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
        _install_opencode_hook()


@hooks.command("uninstall")
@click.argument("agent", type=click.Choice(list(HOOK_AGENTS.keys())))
def hooks_uninstall(agent: str):
    """Uninstall hook for a specific agent."""
    if agent == "claude-code":
        _uninstall_claude_code_hook()
    elif agent == "codex":
        _uninstall_codex_hook()
    elif agent == "gemini":
        _uninstall_gemini_hook()
    elif agent == "opencode":
        _uninstall_opencode_hook()


def _hook_command_exists(entries: list, keyword: str) -> bool:
    """Check if a hook command containing keyword exists in Claude Code hook entries."""
    for entry in entries:
        # Claude Code format: {"hooks": [{"type": "command", "command": "..."}]}
        for h in entry.get("hooks", []):
            if keyword in h.get("command", ""):
                return True
        # Also check flat format (legacy)
        if keyword in entry.get("command", ""):
            return True
    return False


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

    if _hook_command_exists(session_end, "kg-memory-mcp"):
        click.echo("Claude Code hook already installed.")
        return

    # Claude Code requires: {"hooks": [{"type": "command", ...}]}
    hook_entry = {
        "hooks": [
            {
                "type": "command",
                "command": "kg-memory-mcp hooks run claude-code",
            }
        ]
    }
    session_end.append(hook_entry)

    _atomic_write_json(settings_path, settings)

    click.echo(f"Installed Claude Code hook → {settings_path}")


def _install_codex_hook():
    """Install Codex notify hook into ~/.codex/config.toml"""
    import re as _re

    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    content = config_path.read_text() if config_path.exists() else ""
    if "kg-memory-mcp" in content:
        click.echo("Codex hook already installed.")
        return

    notify_line = 'notify = ["kg-memory-mcp", "hooks", "run", "codex"]'

    # 如果已有 notify = [...] 行，追加到数组中而不是新建一行
    if _re.search(r'^notify\s*=\s*\[', content, _re.MULTILINE):
        click.echo("Warning: existing notify config found. Please manually add kg-memory-mcp.", err=True)
        return

    # 确保末尾有换行再追加
    if content and not content.endswith("\n"):
        content += "\n"
    content += notify_line + "\n"

    # 原子写入：写临时文件再 rename
    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, config_path)
    except BaseException:
        os.unlink(tmp_path)
        raise

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

    if _hook_command_exists(session_end, "kg-memory-mcp"):
        click.echo("Gemini hook already installed.")
        return

    # Gemini CLI uses same hooks format as Claude Code
    hook_entry = {
        "hooks": [
            {
                "type": "command",
                "command": "kg-memory-mcp hooks run gemini",
            }
        ]
    }
    session_end.append(hook_entry)

    _atomic_write_json(settings_path, settings)

    click.echo(f"Installed Gemini hook → {settings_path}")


def _uninstall_claude_code_hook():
    """Remove kg-memory-mcp hook from ~/.claude/settings.json"""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        click.echo("Claude Code settings not found, nothing to uninstall.")
        return

    with open(settings_path) as f:
        settings = json.load(f)

    session_end = settings.get("hooks", {}).get("SessionEnd", [])
    if not _hook_command_exists(session_end, "kg-memory-mcp"):
        click.echo("Claude Code hook not installed, nothing to uninstall.")
        return

    # Filter out entries containing kg-memory-mcp
    filtered = [
        entry for entry in session_end
        if not any("kg-memory-mcp" in h.get("command", "") for h in entry.get("hooks", []))
        and "kg-memory-mcp" not in entry.get("command", "")
    ]
    settings["hooks"]["SessionEnd"] = filtered

    # Clean up empty structures
    if not filtered:
        del settings["hooks"]["SessionEnd"]
    if not settings["hooks"]:
        del settings["hooks"]

    _atomic_write_json(settings_path, settings)

    click.echo(f"Uninstalled Claude Code hook from {settings_path}")


def _uninstall_codex_hook():
    """Remove kg-memory-mcp notify from ~/.codex/config.toml"""
    import re as _re

    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        click.echo("Codex config not found, nothing to uninstall.")
        return

    content = config_path.read_text()
    if "kg-memory-mcp" not in content:
        click.echo("Codex hook not installed, nothing to uninstall.")
        return

    # 精确匹配我们安装的那一行，避免误删用户其他 notify 配置
    exact_pattern = r'^notify\s*=\s*\["kg-memory-mcp",\s*"hooks",\s*"run",\s*"codex"\]\s*\n?'
    if _re.search(exact_pattern, content, _re.MULTILINE):
        filtered = _re.sub(exact_pattern, '', content, flags=_re.MULTILINE)
    else:
        # notify 行含 kg-memory-mcp 但格式不匹配（用户手动编辑过），提示手动处理
        click.echo("Warning: notify config contains kg-memory-mcp but in unexpected format.", err=True)
        click.echo("Please manually remove kg-memory-mcp from ~/.codex/config.toml", err=True)
        return

    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(filtered)
        os.replace(tmp_path, config_path)
    except BaseException:
        os.unlink(tmp_path)
        raise

    click.echo(f"Uninstalled Codex hook from {config_path}")


def _uninstall_gemini_hook():
    """Remove kg-memory-mcp hook from ~/.gemini/settings.json"""
    settings_path = Path.home() / ".gemini" / "settings.json"
    if not settings_path.exists():
        click.echo("Gemini settings not found, nothing to uninstall.")
        return

    with open(settings_path) as f:
        settings = json.load(f)

    session_end = settings.get("hooks", {}).get("SessionEnd", [])
    if not _hook_command_exists(session_end, "kg-memory-mcp"):
        click.echo("Gemini hook not installed, nothing to uninstall.")
        return

    filtered = [
        entry for entry in session_end
        if not any("kg-memory-mcp" in h.get("command", "") for h in entry.get("hooks", []))
        and "kg-memory-mcp" not in entry.get("command", "")
    ]
    settings["hooks"]["SessionEnd"] = filtered

    if not filtered:
        del settings["hooks"]["SessionEnd"]
    if not settings["hooks"]:
        del settings["hooks"]

    _atomic_write_json(settings_path, settings)

    click.echo(f"Uninstalled Gemini hook from {settings_path}")


def _install_opencode_hook():
    """Copy kg-memory.ts plugin to ~/.config/opencode/plugins/"""
    plugin_dir = Path.home() / ".config" / "opencode" / "plugins"
    plugin_file = plugin_dir / "kg-memory.ts"

    if plugin_file.exists():
        click.echo("OpenCode hook already installed.")
        return

    # Source: bundled .ts file next to this module
    src = Path(__file__).parent / "hooks" / "opencode.ts"
    if not src.exists():
        click.echo(f"Error: plugin source not found at {src}", err=True)
        return

    plugin_dir.mkdir(parents=True, exist_ok=True)

    import shutil
    shutil.copy2(src, plugin_file)
    click.echo(f"Installed OpenCode plugin \u2192 {plugin_file}")


def _uninstall_opencode_hook():
    """Remove kg-memory.ts plugin from ~/.config/opencode/plugins/"""
    plugin_file = Path.home() / ".config" / "opencode" / "plugins" / "kg-memory.ts"

    if not plugin_file.exists():
        click.echo("OpenCode hook not installed, nothing to uninstall.")
        return

    plugin_file.unlink()
    click.echo(f"Uninstalled OpenCode plugin from {plugin_file}")


@hooks.command("run")
@click.argument("agent", type=click.Choice(["claude-code", "codex", "gemini", "opencode"]))
def hooks_run(agent: str):
    """Run a hook directly (called by agent integrations)."""
    if agent == "claude-code":
        from .hooks.claude_code import main as hook_main
    elif agent == "codex":
        from .hooks.codex import main as hook_main
    elif agent == "gemini":
        from .hooks.gemini import main as hook_main
    elif agent == "opencode":
        from .hooks.opencode import main as hook_main
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
                    plugin_file = settings_path / "kg-memory.ts"
                    installed = plugin_file.exists()
                else:
                    with open(settings_path) as f:
                        data = json.load(f)
                    hooks_data = data.get("hooks", {})
                    for event_hooks in hooks_data.values():
                        if _hook_command_exists(event_hooks, "kg-memory-mcp"):
                            installed = True
                            break
            except Exception as e:
                click.echo(f"  {agent:15s} {'error':15s} ({settings_path}): {e}", err=True)
                continue

        status = "installed" if installed else "not installed"
        click.echo(f"  {agent:15s} {status:15s} ({settings_path})")
