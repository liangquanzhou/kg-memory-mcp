"""轻量 schema 迁移执行器：编号 SQL 文件 + schema_version 表"""

import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent


def _find_psql() -> str:
    for p in [
        "psql",
        "/opt/homebrew/opt/postgresql@18/bin/psql",
        "/opt/homebrew/opt/postgresql@17/bin/psql",
        "/usr/local/bin/psql",
    ]:
        try:
            subprocess.run([p, "--version"], capture_output=True, check=True)
            return p
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("Error: psql not found.", file=sys.stderr)
    sys.exit(1)


def _psql_run(psql: str, dsn: dict, sql: str) -> subprocess.CompletedProcess:
    """Execute a SQL string via psql."""
    import os

    env = os.environ.copy()
    if dsn.get("password"):
        env["PGPASSWORD"] = dsn["password"]
    return subprocess.run(
        [psql, "-h", dsn["host"], "-p", dsn["port"], "-U", dsn["user"], "-d", dsn["dbname"],
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, env=env,
    )


def _psql_file(psql: str, dsn: dict, filepath: Path) -> subprocess.CompletedProcess:
    """Execute a SQL file via psql."""
    import os

    env = os.environ.copy()
    if dsn.get("password"):
        env["PGPASSWORD"] = dsn["password"]
    return subprocess.run(
        [psql, "-h", dsn["host"], "-p", dsn["port"], "-U", dsn["user"], "-d", dsn["dbname"],
         "-v", "ON_ERROR_STOP=1", "-f", str(filepath)],
        capture_output=True, text=True, env=env,
    )


def _get_migration_files() -> list[tuple[int, Path]]:
    """Scan migrations/ for numbered SQL files, return sorted (version, path) pairs."""
    pattern = re.compile(r"^(\d{3})_.+\.sql$")
    results = []
    for f in MIGRATIONS_DIR.iterdir():
        m = pattern.match(f.name)
        if m:
            results.append((int(m.group(1)), f))
    return sorted(results)


def run_migrations(dsn: dict) -> int:
    """Run all pending migrations. Returns the final schema version.

    DSN keys: dbname, user, host, port, password
    """
    psql = _find_psql()

    # 1. Ensure schema_version table exists
    _psql_run(psql, dsn, """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INT PRIMARY KEY,
            applied     TIMESTAMPTZ DEFAULT NOW(),
            description TEXT
        );
    """)

    # 2. Detect existing database without schema_version records
    #    (upgraded from pre-migration schema.sql)
    result = _psql_run(psql, dsn, "SELECT COALESCE(MAX(version), 0) FROM schema_version;")
    current_version = 0
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                current_version = int(line)
                break

    if current_version == 0:
        # Check if tables already exist (pre-migration install)
        check = _psql_run(psql, dsn,
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'kg_entities';")
        has_tables = "1" in check.stdout
        if has_tables:
            # Mark baseline as already applied
            _psql_run(psql, dsn,
                "INSERT INTO schema_version (version, description) VALUES (1, 'baseline (detected existing tables)') "
                "ON CONFLICT DO NOTHING;")
            current_version = 1
            print("  Detected existing tables, marked baseline as v1")

    # 3. Find and run pending migrations
    migrations = _get_migration_files()
    applied = 0

    for version, filepath in migrations:
        if version <= current_version:
            continue

        desc = filepath.stem.split("_", 1)[1] if "_" in filepath.stem else filepath.stem
        print(f"  Applying {filepath.name} ...")

        result = _psql_file(psql, dsn, filepath)
        if result.returncode != 0:
            print(f"  FAILED: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)

        # Record successful migration
        _psql_run(psql, dsn,
            f"INSERT INTO schema_version (version, description) VALUES ({version}, '{desc}');")
        current_version = version
        applied += 1

    if applied == 0:
        print(f"  Schema is up to date (v{current_version})")
    else:
        print(f"  Applied {applied} migration(s), now at v{current_version}")

    return current_version
