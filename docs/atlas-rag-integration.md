# Atlas RAG MCP 集成

Nexus Agent 通过通用 MCP Client 接入独立的 Atlas RAG。Nexus 不导入 RAG 的 Python 包，也不加载 Embedding、Reranker 或 Qdrant；它只通过 stdio 启动 `rag-mcp`，发现并调用 `search` 工具。

```text
Nexus Agent --stdio MCP--> rag-mcp --HTTP--> rag-service --> Qdrant
```

## 启动 Atlas RAG

推荐让 `rag-mcp` 只作为轻量 MCP 适配器，把检索请求转发给独立的 `rag-service`：

```powershell
cd D:\project\Praxis-RAG
$env:RAG_BACKEND = "qdrant"
$env:QDRANT_URL = "http://127.0.0.1:6333"
$env:RAG_COLLECTION = "mini_agent_phase3_hybrid"
$env:RAG_EMBEDDING_DEVICE = "cpu"
$env:RAG_RERANKER_DEVICE = "cpu"
uv run --project . rag-service
```

默认服务地址是 `http://127.0.0.1:8011`。先通过 Atlas Console 上传知识文档，并确认 namespace 与 MCP 配置一致。

## 配置 Nexus

在 Nexus 设置页的 MCP Server 配置中加入以下条目。Windows 下直接使用 Atlas 虚拟环境中的 Python，可以避免依赖全局命令：

```json
{
  "mcpServers": {
    "atlas": {
      "type": "stdio",
      "command": "D:\\project\\Praxis-RAG\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rag_mcp.server"],
      "cwd": "D:\\project\\Praxis-RAG",
      "env": {
        "RAG_SERVICE_URL": "http://127.0.0.1:8011",
        "RAG_NAMESPACE": "default"
      },
      "timeout": 60,
      "disabled": false
    }
  }
}
```

启用 Nexus 的 MCP 能力并新建一次 Agent run。Nexus 会发现 Atlas 的 `search`，内部工具名为 `mcp_atlas_search`。调用结果包含 `evidence_status`、`source`、`chunk_id` 和检索片段，模型据此生成最终回答。

`RAG_SERVICE_TOKEN` 等敏感值不要写入配置文件明文，应使用 Nexus 的凭据引用机制。未配置 `RAG_SERVICE_URL` 时，`rag-mcp` 也可以在自己的进程中使用 mock 或本地 RAG 后端；这适合协议测试，生产运行推荐独立 `rag-service` 模式。

## 集成测试

跨进程测试默认跳过，不影响普通单元测试。设置 Atlas 仓库路径后运行：

```powershell
$env:PRAXIS_ATLAS_RAG_ROOT = "D:\project\Praxis-RAG"
uv run python -m pytest tests/test_atlas_rag_mcp.py -q
```

测试会启动真实 `rag-mcp` stdio 进程，验证 Nexus 能发现 `mcp_atlas_search`、发起调用并解析带 `source` 和 `chunk_id` 的证据结果。测试使用 Atlas 的 mock backend，不需要下载模型或启动 Qdrant；真实检索链路应在启动 `rag-service` 后再手工验证。
