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
    source: str = "builtin"         # 來源：builtin / plugin / lens
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
        source="builtin",
    )
)
```

### @loom.tool 裝飾器

```python
from loom.extensibility.adapter import loom

@loom.tool(trust_level=TrustLevel.GUARDED)
def my_tool(arg1: str, arg2: int) -> str:
    """My tool description"""
    return f"{arg1}: {arg2}"
```

這個 decorator 會自動：
1. 從函數簽名推斷 JSON Schema
2. 從 docstring 提取 description
3. 包裝為 `executor` 函數
4. 註冊進全域 AdapterRegistry

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

# 按來源篩選
plugin_tools = [t for t in registry.list() if t.source == "plugin"]
```

---

## AdapterRegistry（Plugin 工具）

`AdapterRegistry` 是 Plugin 系統中的工具註冊表，通過 `@loom.tool` decorator 收集：

```python
class AdapterRegistry:
    _tools: list[ToolDefinition] = []
    _lock = asyncio.Lock()

    @classmethod
    def register_tool(cls, tool: ToolDefinition):
        """Decorator 或直接呼叫的內部方法"""
        ...

    @classmethod
    def get_all_tools(cls) -> list[ToolDefinition]:
        ...
```

### loom_tools.py 工作區自動掃描

Loom 會自動掃描以下位置的 `loom_tools.py` 檔案：
1. 當前專案根目錄
2. `~/.loom/plugins/` 下的各 plugin 目錄

每個 `loom_tools.py` 中使用 `@loom.tool` 裝飾的函數會自動註冊。

---

## 衝突處理

當多個來源註冊同名工具時：

| 優先級 | 來源 | 行為 |
|--------|------|------|
| 最高 | Platform CLI 內建 | 覆蓋其他 |
| 中 | Plugin 註冊 | 覆蓋 Lens 匯入 |
| 最低 | Lens 自動發現 | 可被覆寫 |

---

## loom_tools.py 範例

```python
# my_plugin/loom_tools.py
from loom.extensibility.adapter import loom

@loom.tool(trust_level=TrustLevel.GUARDED)
def deploy_service(service_name: str, env: str = "staging") -> str:
    """部署服務到指定環境"""
    return f"Deployed {service_name} to {env}"
```

Plugin 安裝時，這個工具會自動出現在 ToolRegistry 中。

---

## 工具定義的最佳實踐

1. **description 要具體** — 告訴 LLM 何時應該呼叫這個工具
2. **input_schema 要完整** — 包含所有必要參數與可選參數
3. **Trust Level 要準確** — 避免過度寬鬆導致安全漏洞
4. **executor 要非同步化** — 使用 `async def`，避免阻塞 event loop
5. **executor 不拋异常** — 錯誤透過 `ToolResult(success=False, error=...)` 回傳
