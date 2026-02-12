"""psql binary lookup — shared between cli.py and migrations/runner.py"""

import subprocess
import sys


def find_psql() -> str:
    """Find psql binary, exit if not found."""
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
    print("Error: psql not found. Please install PostgreSQL.", file=sys.stderr)
    sys.exit(1)
