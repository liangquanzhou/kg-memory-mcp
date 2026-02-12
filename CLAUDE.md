# kg-memory-mcp

PostgreSQL 知识图谱 + 对话存档 MCP Server，替代 mcp-server-memory (JSONL) + mem0 (云端)。

## 技术栈
- PostgreSQL 18 + pgvector 0.8.1，数据库 `knowledge_base`
- Ollama bge-m3 embedding (1024 维)
- FastMCP (Python)，asyncpg 驱动

## 运行
```bash
# 开发
uv run kg-memory-mcp serve

# 安装后
kg-memory-mcp serve
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
