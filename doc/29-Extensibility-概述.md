# Extensibility 概述

Extensibility 是 Loom 對外開放的擴充介面：新增工具、新增 plugin、接入 MCP 生態，全部不需要 fork 核心。

---

## 三條擴充通道

```
┌───────────────────────────────────────────────────────────┐
│                  Loom 擴充系統（current）                  │
├───────────────────────────────────────────────────────────┤
│                                                           │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│   │  @loom.tool  │  │  LoomPlugin  │  │     MCP      │ │
│   │              │  │              │  │              │ │
│   │  單一 tool   │  │  tools +     │  │  雙向接入    │ │
│   │  快速註冊    │  │  middleware  │  │  外部生態    │ │
│   │              │  │  + notifier  │  │              │ │
│   └──────────────┘  └──────────────┘  └──────────────┘ │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

---

## `@loom.tool` — 單一 tool 快速註冊

最簡單的擴充：寫一個 async 函式，加上 `@loom.tool` 裝飾器，丟進 `~/.loom/plugins/` 或工作區 `loom_tools.py`。

```python
# ~/.loom/plugins/git_log.py
import loom
from loom.core.harness.middleware import ToolCall, ToolResult

@loom.tool(trust_level="safe", description="Show last 5 git commits")
async def git_log(call: ToolCall) -> ToolResult:
    ...
```

實作位置：`loom/extensibility/adapter.py` 的 `AdapterRegistry`。模組層 `_default_registry` 是 sink，session 啟動時 `install_into(self.registry)` 全量灌入。

---

## `LoomPlugin` — 多元件擴充

當你要同時貢獻 tools + middleware + notifier，繼承 `LoomPlugin`：

```python
# ~/.loom/plugins/my_pack.py
import loom
from loom.extensibility import LoomPlugin

class MyPack(LoomPlugin):
    name = "my_pack"
    version = "1.0"

    def tools(self):         return [...]
    def middleware(self):    return [...]
    def notifiers(self):     return [...]
    def on_session_start(self, session): ...

loom.register_plugin(MyPack())
```

實作位置：`loom/extensibility/plugin.py` 的 `LoomPlugin` ABC + `PluginRegistry`。同樣 session 啟動時呼叫 `install_into(self)`，把 tools 進 registry、middleware prepend 到 pipeline、notifier 註冊到 NotificationRouter。

詳見 [31-Plugin-系統.md](31-Plugin-系統.md)。

---

## Plugin 載入流程

```
LoomSession.start()
  └─ _load_plugins()
        └─ scan ~/.loom/plugins/*.py
              └─ 首次見到 → 顯示前 N 行 → ask user approve
                    └─ 透過 relational_bridge.upsert_triple 寫入 SemanticMemory
                          （`plugin:<path>` predicate=approved；#451 phase B）
                          └─ exec_module()  # 觸發 @loom.tool / register_plugin()
                                └─ _get_default_registry().install_into(session.registry)
                                └─ _get_default_plugin_registry().install_into(session)
```

只掃 `~/.loom/plugins/`（沒有多重路徑也沒有 manifest.toml）。一次性人工核可，approval 落 SQLite，之後 silent 重載。

---

## MCP 整合

| 方向 | 入口 | 用途 |
|------|------|------|
| **MCP Server** | `loom mcp serve` | 把 Loom 內建 tools 暴露給 Claude Desktop / Cursor / Continue 等 MCP client |
| **MCP Client** | `loom mcp connect <cmd>` 或 `loom.toml` 的 `[[mcp.servers]]` | 把外部 MCP server 的 tools 拉進當前 session |

實作：`loom/extensibility/mcp_server.py`、`loom/extensibility/mcp_client.py`。Client 路徑在 `LoomSession.start()` 內呼叫 `load_mcp_servers_into_session()`，持有的 client 物件存在 `session._mcp_clients`，stop() 時收尾。

詳見 [31b-MCP-Server-實作.md](31b-MCP-Server-實作.md)。

---

## 退役通道（2026-05-23）

歷史上 `loom/extensibility/` 還有兩個子系統，現已退役：

| 退役模組 | 原始用途 | 退役原因 |
|----------|----------|----------|
| Lens 系統（`lens.py` / `hermes.py` / `openai_tools.py`）| 從 Hermes / OpenAI JSON 抽 skill + tool 的轉換層 | 從未實際被用；skill 系統收斂到 `.claude/skills/` |
| Skill Import Pipeline（`pipeline.py` + `loom import` CLI）| trust 加權後寫進 ProceduralMemory | 同上；skill ingestion 改走 ProceduralMemory 直連 |

snapshot 存於 `_archive/extensibility-lens-retired-2026-05-23/`（gitignored，僅作考古）。

更早期還有 `DreamingPlugin` / `SelfReflectionPlugin` 兩個內建 plugin，現已併入核心模組：

- `dream_cycle` 邏輯 → `loom/core/memory/`（v0.2.6.4 / #172）
- self-reflection → `loom/core/cognition/self_reflection.py`，由 `TaskReflector` 觸發（Issue #120 / audit-B / #399）

所以目前 `loom/extensibility/` **沒有**內建 LoomPlugin 實作，只剩抽象介面（`adapter.py` / `plugin.py`）與 MCP runtime（`mcp_client.py` / `mcp_server.py`）。

---

## 與 `skills/` 的命名分工

| 放置位置 | 類型 | 用途 |
|----------|------|------|
| `~/.loom/plugins/<name>.py` | Python plugin | 含 `@loom.tool` 或 `LoomPlugin` 的可執行擴充 |
| `.claude/skills/<name>/SKILL.md` | Skill 包 | Markdown + YAML frontmatter 形式的技能描述 |
| `loom/extensibility/` | 框架代碼 | Plugin 抽象 + MCP runtime |

`skills/` 跟 `loom/extensibility/` 是兩個截然不同的層：前者是 agent 行為 spec，後者是 Python 擴充機制。

---

## loom.toml 對應段落

實際被讀取的擴充配置只有 MCP servers：

```toml
[[mcp.servers]]
name        = "filesystem"
command     = "npx"
args        = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
trust_level = "safe"
```

Plugin 路徑、auto_discover、sandbox 等 keys 並沒有實作 —— Plugin 一律 hard-coded 走 `~/.loom/plugins/`。

---

## 總結

| 通道 | 適用情境 |
|------|----------|
| `@loom.tool` | 單一 async 函式即可解決 |
| `LoomPlugin` | 同時要貢獻 tools + middleware + notifier |
| MCP Server | 把 Loom 工具開放給其他 agent 平台 |
| MCP Client | 直接吃外部 MCP 生態的 tools |

> 全部走「對 session 一次 install」的模式，runtime 中不熱裝載 / 不熱卸載；要改 plugin 必須重啟 session。
