# kg-memory-mcp

PostgreSQL 知识图谱 + 对话存档 MCP Server，替代 mcp-server-memory (JSONL) + mem0 (云端)。

## 技术栈
- PostgreSQL 18 + pgvector 0.8.1，数据库 `knowledge_base`
- Ollama bge-m3 embedding (1024 维)
- FastMCP (Python)，asyncpg 驱动

## 安装与运行

当前使用 **安装模式**（`uv tool install`），各 CLI 工具的 MCP 配置指向 `~/.local/bin/kg-memory-mcp`。

```bash
# 首次安装 / 改代码后重装（必须！否则新代码不生效）
uv tool install --force --from . kg-memory-mcp

# 启动（MCP 配置自动调用，一般不需要手动跑）
kg-memory-mcp serve

# 开发调试（直接用项目 venv，改代码立即生效，不需要重装）
uv run kg-memory-mcp serve
```

## 数据库
- 建表: `kg-memory-mcp init`
- 默认数据库: `knowledge_base`

## 项目结构
- kg_memory_mcp/server.py: MCP 入口，注册所有工具
- kg_memory_mcp/db.py: 知识图谱 CRUD (asyncpg)
- kg_memory_mcp/embedding.py: Ollama embedding 封装
- kg_memory_mcp/search.py: FTS + 向量 + RRF + 1-hop 图扩展
- kg_memory_mcp/quality.py: 写入去重 + 敏感词过滤
- kg_memory_mcp/chat_db.py: 对话存档数据库操作
- kg_memory_mcp/collector/: 对话采集器
- kg_memory_mcp/hooks/: Hook 脚本
- kg_memory_mcp/cli.py: CLI 入口
- kg_memory_mcp/migrate.py: memory.jsonl 迁移
