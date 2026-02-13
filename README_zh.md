[English](README.md) | [中文](README_zh.md)

# kg-memory-mcp

**知识图谱记忆 + 对话存档 MCP 服务器**

自托管的 [Model Context Protocol](https://modelcontextprotocol.io/) 服务器，基于 PostgreSQL + pgvector。提供持久化知识图谱存储（实体、观察、关系）和多 Agent 对话存档，支持混合搜索——替代 `mcp-server-memory`（JSONL）和云端记忆服务。

## 功能特性

- **知识图谱** — 创建带类型观察和有向关系的实体。自动去重（哈希 + 语义）和敏感内容过滤。
- **对话存档** — 采集并存储 Claude Code、Codex CLI、Gemini CLI、OpenCode 的对话记录。完整会话历史及元数据。
- **混合搜索** — 全文搜索（PostgreSQL tsvector）+ 向量相似度（pgvector HNSW），通过 RRF（Reciprocal Rank Fusion）融合，并支持 1-hop 图扩展。
- **Hook 系统** — 会话结束后自动存档和知识提取（需 3 轮以上对话）。可选通过 Gemini API 进行结构化知识提取。一条命令即可为支持的 Agent 安装 Hook。
- **本地 Embedding** — 使用 Ollama + bge-m3（1024 维）生成所有向量。数据不离开本机。
- **Schema 迁移** — 轻量级编号 SQL 迁移系统。安全升级现有数据库，不丢失数据。
- **数据导出** — 导出为 JSONL（人类可读、可互操作）或 SQLite（单文件备份）。兼容迁移到其他工具。
- **图片存档** — 从 Agent 对话记录中提取 base64 图片到本地文件系统，SHA256 去重，并记录元数据。单张最大 50 MB。

## 架构

```
+------------------+     stdio      +-------------------+
|   MCP 客户端     |<-------------->|  kg-memory-mcp    |
|  (Claude Code,   |                |  (FastMCP server) |
|   Codex, Gemini, |                +--------+----------+
|   OpenCode)      |                         |
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
|  (自动存档       |--------------->|   bge-m3           |
|   + 知识提取)    |                |   (embeddings)     |
+------------------+                +-------------------+
```

## 快速开始

### 前置条件

- PostgreSQL 17+，安装 [pgvector](https://github.com/pgvector/pgvector) 扩展
- [Ollama](https://ollama.com/)，并拉取 `bge-m3` 模型
- Python 3.11+

### 安装

```bash
# 通过 pip
pip install kg-memory-mcp

# 或通过 uvx 直接运行（无需安装）
uvx kg-memory-mcp serve
```

### 初始化数据库

```bash
# 先创建数据库
createdb knowledge_base

# 拉取 embedding 模型
ollama pull bge-m3

# 运行 schema 迁移
kg-memory-mcp init
```

### 配置 MCP 客户端

将以下配置添加到你的 MCP 客户端：

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

**Claude Code** (`~/.claude/settings.json`)：

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

**Codex CLI** (`~/.codex/config.toml`)：

```toml
[mcp_servers.kg-memory]
command = "uvx"
args = ["kg-memory-mcp", "serve"]
```

**Gemini CLI** (`~/.gemini/settings.json`)：

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

## MCP 工具

### 知识图谱

| 工具 | 描述 |
|------|------|
| `create_entities` | 创建实体，包含名称、类型、描述及可选观察 |
| `add_observations` | 向实体添加观察（自动去重 + 敏感信息过滤） |
| `create_relations` | 创建实体间的有向关系 |
| `delete_entities` | 删除实体（级联删除观察和关系） |
| `delete_observations` | 删除实体的特定观察 |
| `delete_relations` | 删除特定关系 |
| `search_nodes` | 混合 FTS + 向量搜索，支持 1-hop 图扩展 |
| `read_graph` | 读取完整知识图谱 |

### 对话存档

| 工具 | 描述 |
|------|------|
| `search_chats` | 跨 Agent 搜索对话消息 |
| `get_session` | 获取完整对话会话及所有消息 |
| `list_sessions` | 列出对话会话，可按 Agent 过滤 |

## CLI 参考

```
kg-memory-mcp [OPTIONS] COMMAND [ARGS]

命令:
  serve                  启动 MCP 服务器（stdio 传输）
  init                   运行 schema 迁移（创建/升级表）
  migrate JSONL_PATH     从 memory.jsonl 迁移（mcp-server-memory 格式，自动拆分大实体）
  collect                采集 AI Agent 对话记录
    --agent TEXT          仅采集: claude-code, codex, gemini-cli, opencode
  export jsonl           导出所有数据为 JSONL 文件
    --output-dir PATH    输出目录（默认: ./export）
  export sqlite          导出所有数据为单个 SQLite 文件
    --output PATH        输出文件路径（默认: ./kg-memory-backup.db）
  reset                  删除所有 kg-memory-mcp 表（需确认）
  hooks install AGENT    为指定 Agent 安装 Hook
  hooks uninstall AGENT  卸载指定 Agent 的 Hook
  hooks status           检查所有 Hook 的安装状态
  hooks run AGENT        直接运行 Hook（由 Agent 集成调用）
```

### 示例

```bash
# 启动 MCP 服务器
kg-memory-mcp serve

# 初始化数据库 schema
kg-memory-mcp init --db-name knowledge_base --db-user postgres

# 从 mcp-server-memory 迁移
kg-memory-mcp migrate ~/.claude/memory.jsonl

# 采集所有 Agent 对话记录
kg-memory-mcp collect

# 仅采集 Claude Code 对话记录
kg-memory-mcp collect --agent claude-code

# 为 Claude Code 安装自动存档 Hook
kg-memory-mcp hooks install claude-code

# 卸载 Hook
kg-memory-mcp hooks uninstall claude-code

# 检查 Hook 状态
kg-memory-mcp hooks status

# 导出为 JSONL（方便迁移到其他工具）
kg-memory-mcp export jsonl --output-dir ./my-export

# 导出为 SQLite（单文件备份）
kg-memory-mcp export sqlite --output ./backup.db
```

## 支持的 Agent

| Agent | 采集器 | Hook | 对话记录位置 |
|-------|--------|------|-------------|
| Claude Code | 是 | SessionEnd | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | 是 | notify (agent-turn-complete) | `~/.codex/sessions/**/*.jsonl` |
| Gemini CLI | 是 | SessionEnd | `~/.gemini/tmp/*/chats/session-*.json` |
| OpenCode | 是 | session.idle (plugin) | `~/.local/share/opencode/storage/` |

## 环境变量

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `KG_DB_NAME` | `knowledge_base` | PostgreSQL 数据库名 |
| `KG_DB_USER` | `postgres` | PostgreSQL 用户 |
| `KG_DB_HOST` | `localhost` | PostgreSQL 主机 |
| `KG_DB_PORT` | `5432` | PostgreSQL 端口 |
| `KG_DB_PASSWORD` | *（无）* | PostgreSQL 密码（如需要） |
| `KG_DB_SSL` | *（禁用）* | 设为 `require` 以启用远程 DB 的 SSL 连接 |
| `KG_CHAT_SANITIZE` | *（禁用）* | 设为 `true` 以在存档前过滤含密钥的消息 |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API 地址 |
| `OLLAMA_EMBED_MODEL` | `bge-m3` | Ollama embedding 模型名 |
| `ATTACHMENT_DIR` | `~/.local/share/kg-memory/attachments` | 图片附件存储目录 |
| `GEMINI_API_KEY` | *（无）* | 启用 Hook 中的自动知识提取（可选，会向 Google 发送数据） |

## 隐私与安全

**所有数据默认保留在本地。** MCP 服务器、数据库和 embedding 完全运行在你的机器上。除非你主动启用，否则不会向外部服务发送任何数据。

- **数据库存储**：对话记录和知识图谱数据以明文存储在本地 PostgreSQL 数据库中。请确保数据库有适当的访问控制。
- **Ollama embedding**：向量 embedding 通过 Ollama 在本地生成，数据不离开本机。
- **Gemini 知识提取**（可选启用）：如果设置了 `GEMINI_API_KEY` 环境变量，SessionEnd Hook 会将对话摘要（最多 15KB）发送到 Google Gemini API 进行知识提取。此功能**默认禁用**——没有 API Key 则不会向外部发送数据。对话内容会自动过滤掉包含 API Key、密码和 Token 的行，但项目路径和代码片段仍可能被包含。
- **Hook 文件访问**：Hook 仅从预期目录（`~/.claude/`、`~/.codex/`、`~/.gemini/`、`~/.local/share/opencode/`）读取对话记录文件。路径遍历已做校验。
- **敏感内容过滤**：`quality.py` 模块在通过 MCP 工具和 Hook 写入知识图谱时过滤 API Key、密码和 Token。注意：`migrate` 和 `collect` 等批量操作也会应用此过滤。

## 卸载

```bash
# 1. 卸载所有 Agent 的 Hook
kg-memory-mcp hooks uninstall claude-code
kg-memory-mcp hooks uninstall codex
kg-memory-mcp hooks uninstall gemini
kg-memory-mcp hooks uninstall opencode

# 2.（可选）删除数据库中所有表
kg-memory-mcp reset

# 3. 从客户端移除 MCP 服务器配置
#    删除 settings 中 mcpServers 里的 "kg-memory" 条目

# 4. 卸载包
pip uninstall kg-memory-mcp    # 如果通过 pip 安装
uv tool uninstall kg-memory-mcp  # 如果通过 uv 安装
```

## 开发

```bash
# 克隆并安装开发依赖
git clone https://github.com/liangquanzhou/kg-memory-mcp.git
cd kg-memory-mcp
pip install -e ".[dev]"

# 运行 lint
ruff check kg_memory_mcp/
pyright kg_memory_mcp/

# 运行测试（需要 PostgreSQL + pgvector）
pytest tests/ -v
```

## 许可证

MIT
