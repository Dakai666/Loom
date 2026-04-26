# MCP Server 實作詳解

> `loom/extensibility/mcp_server.py` + `mcp_client.py` — 雙向 MCP 整合。

---

## 定位

Loom 的 MCP（Model Context Protocol）整合是雙向的：

| 方向 | 檔案 | 角色 |
|------|------|------|
| Loom → 外部 | `mcp_server.py` | 將 Loom 工具暴露給其他 MCP 客戶端 |
| 外部 → Loom | `mcp_client.py` | 將外部 MCP 伺服器工具導入 Loom |

---

## MCP Server（`mcp_server.py`）

### 啟動方式

```bash
loom mcp serve
```

使用 stdio transport 啟動 MCP server，連接 Claude Desktop / Cursor / Continue 等 MCP 客戶端。

在 Claude Desktop 的 `claude_desktop_config.json` 中配置：

```json
{
  "mcpServers": {
    "loom": {
      "command": "loom",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

### 工具暴露策略

```
┌─────────────────────────────────────────┐
│ Loom ToolRegistry                       │
│                                         │
│  SAFE 工具 ──→ 自動暴露（無標記）         │
│  GUARDED 工具 ──→ 暴露，標記 x-loom-guarded=true │
│  CRITICAL 工具 ──→ 完全不暴露              │
└─────────────────────────────────────────┘
```

### 工具名稱

Loom 工具以原名暴露，無前綴。

### Pipeline 整合

當有 pipeline 時，MCP 工具呼叫經過完整 middleware chain：

```python
if pipeline is not None:
    result = await pipeline.execute(call, tool_def.executor)
else:
    result = await tool_def.executor(call)  # 降級，繞過 middleware
```

### 跨呼叫的生命週期

MCP Server 是無狀態的，每次 `call_tool` 都是獨立的。沒有 session、沒有記憶、沒有 context——這是 MCP 的設計限制。

---

## MCP Client（`mcp_client.py`）

### 設定方式

在 `loom.toml` 中配置：

```toml
[[mcp.servers]]
name    = "filesystem"
command = "npx"
args    = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
trust_level = "safe"

[[mcp.servers]]
name    = "github"
command = "uvx"
args    = ["mcp-server-git"]
env     = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }   # 從 .env 讀取
trust_level = "guarded"
```

### 工具名稱前綴

外部工具匯入時會加上 server 前綴，避免名稱衝突：

```
filesystem:list_files
filesystem:read_file
github:get_repos
```

### Trust Level 映射

| loom.toml `trust_level` | Loom `TrustLevel` | 說明 |
|------------------------|------------------|------|
| `safe` | `TrustLevel.SAFE` | 自動執行，不需要確認 |
| `guarded` | `TrustLevel.GUARDED` | 需要確認 |

### Mutating 工具檢測

`LoomMCPClient` 會根據工具名稱和描述自動判斷是否需要 `MUTATES` capability：

```python
_MUTATING_KEYWORDS = frozenset({
    "write", "create", "delete", "update", "patch",
    "put", "insert", "remove", "rename", "move",
    "overwrite", "append", "replace", "edit",
})
```

如果工具名稱或描述包含這些關鍵字，自動加上 `MUTATES` capability，並在 schema 中注入 `justification` 參數（Issue #47 的第一步）。

### Justification 注入

```python
# 當 tools 被識別為 mutating 且 trust_level=GUARDED
schema["properties"]["justification"] = {
    "type": "string",
    "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。"
}
schema["required"] = ["justification", ...]
```

### 連接管理

```
LoomMCPClient.__init__(cfg)
    ↓
connect_and_list_tools()
    ├─ _ensure_connected()
    │   ├─ 建立 StdioServerParameters（合併 env）
    │   ├─ stdio_client → ClientSession
    │   └─ session.initialize()
    ├─ session.list_tools() → ToolDefinition[]
    └─ return list[ToolDefinition]

disconnect()
    ├─ stdio_client.__aexit__()
    └─ 抑制所有 exception（避免 async generator GC 錯誤）
```

### Environment Variable 擴展

`env` 中的 `${VAR}` 語法支援從 .env 檔案讀取敏感資訊：

```toml
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }
```

展開時的查找順序：
1. `extra_env`（通常來自 `.env` 檔案的 dict）
2. `os.environ`

### 連接失敗處理

連接失敗不會阻斷 session 啟動——`load_mcp_servers_into_session()` 會對每個 server individually try/catch，單一 server 失敗不會影響其他：

```python
for cfg in server_configs:
    client = LoomMCPClient(cfg)
    try:
        tools = await client.connect_and_list_tools()
        for tool in tools:
            session.registry.register(tool)
        clients.append(client)
    except Exception as exc:
        logger.warning("mcp_client: failed to connect to %r — skipping", cfg.name)
        await client.disconnect()  # 確保 partial CM 被清理
```

### Session 層級的 loader

```python
async def load_mcp_servers_into_session(
    config: dict,
    session: LoomSession,
    extra_env: dict | None = None,
) -> list[LoomMCPClient]:
    """在 session.start() 自動呼叫"""
```

流程：
1. 解析 `[[mcp.servers]]` 設定
2. 對每個 server 建立 `LoomMCPClient` 並連接
3. 將工具註冊進 session registry
4. 回傳 client 清單（session.stop() 時用於 disconnect）

---

## 依賴

```bash
pip install "loom[mcp]"   # 安裝 mcp>=1.0.0
```

缺少時 import 會報有意義的錯誤：

```
ImportError: MCP SDK not installed. Run: pip install 'loom[mcp]'
```

---

## 與 Plugin 系統的整合

MCP 伺服器和 Plugin 是獨立的擴充機制：

| 特性 | MCP Server | Plugin |
|------|-----------|--------|
| 暴露方向 | Loom → 外部 | Loom 內部 |
| 工具發現 | MCP protocol | `loom_tools.py` 掃描 |
| 連接方式 | stdio subprocess | 直接 import |
| 生命周期 | 隨 Loom 進程 | 隨 session |

---

*文件草稿 | 2026-04-26 03:21 Asia/Taipei*