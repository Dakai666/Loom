# Plugin 系統

Plugin 是 Loom 的「功能插件」。Plugin 可以同時攜帶 tools、middleware、lenses、notifiers，並透過 `PluginRegistry.install_into(session)` 無縫整合進 LoomSession。

---

## LoomPlugin 抽象（loom/extensibility/plugin.py）

```python
class LoomPlugin(ABC):
    """Plugin 抽象基類"""

    name: str = ""       # 唯一識別名稱
    version: str = "1.0" # 版本字串

    def tools(self) -> list["ToolDefinition"]:
        """返回要註冊進 session 的工具"""
        return []

    def middleware(self) -> list["Middleware"]:
        """返回要加入 pipeline 的 middleware"""
        return []

    def lenses(self) -> list["BaseLens"]:
        """返回要註冊到 LensRegistry 的 Lens"""
        return []

    def notifiers(self) -> list["BaseNotifier"]:
        """返回要加入 NotificationRouter 的 notifier"""
        return []

    def on_session_start(self, session: object) -> None:
        """所有貢獻安裝完成後呼叫"""

    def on_session_stop(self, session: object) -> None:
        """session.stop() 前呼叫"""
```

---

## PluginRegistry

```python
class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: list[LoomPlugin] = []

    def register(self, plugin: LoomPlugin) -> None:
        """註冊 Plugin（同名替換）"""
        self._plugins = [p for p in self._plugins if p.name != plugin.name]
        self._plugins.append(plugin)

    def all(self) -> list[LoomPlugin]: ...

    def install_into(self, session: object) -> dict[str, int]:
        """
        將所有 Plugin 的貢獻安裝進 live session。
        返回摘要：{plugin_name: tools_installed, ...}
        """
        summary: dict[str, int] = {}
        for plugin in self._plugins:
            count = 0
            for tool_def in plugin.tools():
                session.registry.register(tool_def)  # type: ignore
                count += 1
            # Middleware → 插入 pipeline 頭部（TraceMiddleware 之前）
            for mw in plugin.middleware():
                if session._pipeline is not None:
                    session._pipeline._chain.insert(0, mw)  # type: ignore
            # Notifier → 加入 NotificationRouter
            for notifier in plugin.notifiers():
                router = getattr(session, "_notifier_router", None)
                if router is not None:
                    router.register(notifier)
            plugin.on_session_start(session)  # type: ignore
            summary[plugin.name or "(anonymous)"] = count
        return summary
```

---

## 內建 Plugin

Loom 框架目前自帶一個內建 Plugin，位於 `loom/extensibility/`：

| Plugin | 檔案 | 說明 |
|--------|------|------|
| `DreamingPlugin` | `dreaming_plugin.py` | `dream_cycle` 離線夢境（v0.2.5.3）|

> **v0.2.6.1 架構修復**：DreamingPlugin 之前定義在 `cognition/dreaming.py`，違反「下層不能依賴上層」的架構約束，v0.2.6.1 移至 `loom/extensibility/`。
>
> **Issue #120 PR 1**：原 `SelfReflectionPlugin` 已移除；`run_self_reflection` 保留於 `loom/autonomy/self_reflection.py`，改由 `TaskReflector` 作為結構化診斷的 post-hook 呼叫。

### DreamingPlugin

```python
# loom/extensibility/dreaming_plugin.py
class DreamingPlugin(LoomPlugin):
    name = "dreaming"
    version = "1.0"

    def tools(self) -> list[ToolDefinition]:
        return [dream_cycle_tool]  # dream_cycle: SAFE tool
```

---

## @loom.tool 裝飾器

對於簡單的單工具擴展，不需要完整的 Plugin 類。可以直接使用 `@loom.tool` decorator：

```python
# loom/extensibility/adapter.py
@loom.tool(trust_level="guarded", description="部署服務")
async def deploy(call: ToolCall) -> ToolResult:
    service = call.args.get("service_name")
    return ToolResult(success=True, output=f"Deployed {service}")
```

`@loom.tool` 會自動：
1. 從函數簽名推斷 JSON Schema
2. 從 docstring 提取 description
3. 包裝為 `executor` 函數
4. 註冊進模組級的 `AdapterRegistry`

簡單工具視為一個「匿名 Plugin」，由 Session 載入時統一處理。

---

## 簡單 Plugin 範例

```python
# ~/.loom/plugins/git_tools/plugin.py
from loom.extensibility import LoomPlugin
from loom.core.harness.registry import ToolDefinition
from loom.core.harness.permissions import TrustLevel, ToolCapability

class GitPlugin(LoomPlugin):
    name = "git"
    version = "1.0"

    def tools(self) -> list[ToolDefinition]:
        return [git_status_tool, git_diff_tool]

# ~/.loom/plugins/git_tools/loom_tools.py
# 此檔案會被 loom_tools.py 工作區掃描器自動發現並載入
```

---

## MCP 整合（v0.2.6.0）

`loom/extensibility/` 中有兩個 MCP 相關檔案：

| 檔案 | 說明 |
|------|------|
| `mcp_client.py` | MCP Client：將外部 MCP 伺服器工具導入 Loom |
| `mcp_server.py` | MCP Server：將 Loom 工具暴露給外部 MCP 客戶端 |

### MCP Server 啟動

```bash
# 啟動 Loom MCP Server（將 Loom 工具暴露給 MCP 客戶端）
loom mcp serve
```

SAFE 工具自動暴露；GUARDED 工具標記 `x-loom-guarded=true`；CRITICAL 工具不暴露。

### MCP Client 配置

在 `loom.toml` 中新增 MCP servers：

```toml
[[mcp.servers]]
name        = "filesystem"
command     = "npx"
args        = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
trust_level = "safe"
```

外部工具以前綴 `server_name:` 註冊（如 `filesystem:list_files`）。

### 工具發現

```bash
# 探索 MCP 伺服器上的可用工具
loom mcp connect "npx -y @modelcontextprotocol/server-filesystem /tmp"
```

---

## 安全性：首次執行確認

Plugin 首次被發現時（新的 `.py` 檔案），使用者會被詢問是否批准：

```
⚠️ New plugin found: git_tools
Path: ~/.loom/plugins/git_tools/plugin.py
Tools: git_status, git_diff
Trust level: SAFE

Approve? [y/N]:
```

批准記錄寫入 `RelationalMemory`（`plugin:approved` 三元組），\
未來 session 無需再確認。

---

## 與 Lens 的分工

| | Plugin | Lens |
|---|---|---|
| 攜帶內容 | tools + middleware + lenses + notifiers | 只攜帶 tools/schema |
| 注入方式 | `PluginRegistry.install_into(session)` | `LensRegistry.extract()` |
| 觸發時機 | Session 啟動 | 外部技能匯入時 |
| 用途 | 功能擴充 | 外部框架相容性 |

---

## Plugin 放置位置

| 位置 | 掃描時機 | 用途 |
|------|----------|------|
| `~/.loom/plugins/<name>/` | 每次 session | 用戶全域 plugins |
| `./loom/plugins/<name>/` | 每次 session | 專案 plugins |
| `loom/extensibility/` | 框架啟動時 | 內建模組（DreamingPlugin、MCP）|

> **不建議**將 Plugin 放入 `loom/extensibility/`——那是框架內建擴充的範圍。自訂 Plugin 應放在 `~/.loom/plugins/` 或專案 `loom/plugins/`。

---

## 總結

| 功能 | 說明 |
|------|------|
| 多類型貢獻 | tools / middleware / lenses / notifiers |
| 無縫整合 | `install_into(session)` 自動合併進 live session |
| 匿名工具 | `@loom.tool` decorator 支援簡單擴展 |
| 內建 Plugin | DreamingPlugin、MCP server/client |
| 首次確認 | 新 plugin 首次被發現時詢問用戶 |
| 同名替換 | 同一 session 內後註冊的同名 Plugin 覆蓋先前的 |
| 生命週期 | `on_session_start` / `on_session_stop` 鉤子 |
| MCP 整合 | 雙向 MCP Server/Client 支援 |
