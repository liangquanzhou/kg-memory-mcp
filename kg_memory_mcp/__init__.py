"""kg-memory-mcp: Knowledge graph memory + conversation archival MCP server"""

import os as _os

# 防止 macOS fork 子进程中 Core Foundation API 崩溃 (SIGSEGV)
# libpq/asyncpg 连接时会触发 _scproxy / Kerberos，这些 API 不是 fork-safe 的
_os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
_os.environ.setdefault("no_proxy", "*")

__version__ = "0.1.0"
