> **#708 S4a 退役註記（2026-11-27）**：`@loom.tool`、`AdapterRegistry`、`LoomPlugin`、`PluginRegistry`
> 與 `loom_tools.py` 自動掃描已退役。新增工具請直接註冊 `ToolDefinition` 至 `ToolRegistry`，
> 或透過 `loom.toml` 的 `[[mcp.servers]]` 接入外部 MCP 工具（詳見 [29-Extensibility-概述.md](29-Extensibility-概述.md)）。

# Tool Registry

ToolRegistry 是 Loom 的工具管理中心，負責儲存所有工具的定義、提供雙 provider schema 輸出。

---

## 核心資料結構：ToolDefinition

```python
@dataclass
class ToolDefinition:
    name: str                      # 唯一識別名稱
    description: str               # 描述（供 LLM 理解用途）
    trust_level: TrustLevel        # 信任等級
    input_schema: dict[str, Any]   # JSON Schema（arg name / type / required）
    executor: Callable[[ToolCall], Awaitable[ToolResult]]  # 實際執行函數
    tags: list[str] = field(default_factory=list)  # 標籤（可選）
    capabilities: ToolCapability = ToolCapability.NONE
    scope_resolver: Callable[[ToolCall], ScopeRequest] | None = None
```

### ToolResult 結構

工具執行結果的完整定義見 [06-Middleware-詳解.md](06-Middleware-詳解.md#toolresult)。

---

## 註冊流程

### 內建工具

```python
registry = ToolRegistry()

registry.register(
    ToolDefinition(
        name="read_file",
        description="Read contents of a file",
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"}
            },
            "required": ["path"]
        },
        executor=read_file_impl,   # async def (ToolCall) -> ToolResult
    )
)
```

### 外部 MCP 工具

外部工具透過 `loom.toml` 的 `[[mcp.servers]]` 在 `LoomSession.start()` 時由 `load_mcp_servers_into_session()` 連線並以 `<server>__<tool>` 前綴註冊進 `ToolRegistry`；`GUARDED` 唯讀工具可透過 `tool_effects` 顯式宣告效果（詳見 [29-Extensibility-概述.md](29-Extensibility-概述.md) 與 [31b-MCP-Server-實作.md](31b-MCP-Server-實作.md)）。

---

## Schema 輸出

ToolRegistry 提供兩種 schema 格式，用於不同的 LLM provider：

### to_anthropic_schema()

輸出 [Anthropic tool use format](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)：

```python
registry.to_anthropic_schema()
# [
#   {
#     "name": "read_file",
#     "description": "...",
#     "input_schema": {
#       "type": "object",
#       "properties": {...},
#       "required": [...]
#     }
#   }
# ]
```

### to_openai_schema()

輸出 [OpenAI tool calling format](https://platform.openai.com/docs/guides/function-calling)：

```python
registry.to_openai_schema()
# [
#   {
#     "type": "function",
#     "function": {
#       "name": "read_file",
#       "description": "...",
#       "parameters": {...}
#     }
#   }
# ]
```

---

## 工具查詢

```python
# 依名稱查詢
tool = registry.get("read_file")

# 列出所有工具
all_tools = registry.list()

# 按 Trust Level 篩選（需自行過濾）
guarded_tools = [t for t in registry.list() if t.trust_level == TrustLevel.GUARDED]

# 按標籤篩選（例如外部 MCP 工具）
mcp_tools = [t for t in registry.list() if "mcp" in t.tags]
```

---

## 工具定義的最佳實踐

1. **description 要具體** — 告訴 LLM 何時應該呼叫這個工具
2. **input_schema 要完整** — 包含所有必要參數與可選參數
3. **Trust Level 要準確** — 避免過度寬鬆導致安全漏洞
4. **executor 要非同步化** — 使用 `async def`，避免阻塞 event loop
5. **executor 不拋異常** — 錯誤透過 `ToolResult(success=False, error=...)` 回傳
