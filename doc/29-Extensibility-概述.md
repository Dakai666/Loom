> **#708 S4a 退役註記（2026-11-27）**：`@loom.tool`（`AdapterRegistry`）、`LoomPlugin`（`PluginRegistry`）、
> `loom.register_plugin` 與 `LoomSession._load_plugins`（`~/.loom/plugins/*.py`、`loom_tools.py` 自動掃描）
> 已退役，下文相關章節僅供歷史參考；現行擴充通道為 MCP（`mcp_server.py` 與 `mcp_client.py`）。

> **#675 退役註記（2026-11-19）**：下文的 TaskReflector／TaskDiagnostic、
> diagnostic post-hook 的 self_reflection、SkillEvolutionHook、confidence EMA、
> `[reflection]` 與 `loom diagnostic` 描述僅供歷史參考，已非現行功能。
> skill activation／tier routing、ledger review、shared triple store 與歷史 metrics readers 保留。

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

實作位置（已於 #708 S4a 退役）：`loom/extensibility/adapter.py` 的 `AdapterRegistry`。模組層 `_default_registry` 是 sink，session 啟動時 `install_into(self.registry)` 全量灌入。 <!-- doc-integrity:ignore — retired in #708 S4a -->

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

實作位置（已於 #708 S4a 退役）：`loom/extensibility/plugin.py` 的 `LoomPlugin` ABC + `PluginRegistry`。同樣 session 啟動時呼叫 `install_into(self)`，把 tools 進 registry、middleware prepend 到 pipeline、notifier 註冊到 NotificationRouter。 <!-- doc-integrity:ignore — retired in #708 S4a -->

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
- self-reflection → `loom/core/cognition/self_reflection.py`，由 `TaskReflector` 觸發（Issue #120 / audit-B / #399） <!-- doc-integrity:ignore — deleted in #675; historical reference -->

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

實際被讀取的擴充配置只有 MCP servers（若 `loom.toml` 殘留已退役的 `[plugins]`／`[adapter]`／`[adapters]` 區段，載入時會印 `WARNING` 並忽略）：

```toml
[[mcp.servers]]
name        = "filesystem"
command     = "npx"
args        = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
trust_level = "safe"
```

### `[[mcp.servers]]` 宣告式工具效果（`tool_effects`，#708 S4a）

對於 `trust_level = "guarded"` 的外部 MCP server，Loom **不再**依工具名稱後綴（如 `*__web_search`、`*__understand_image`）硬編碼放行特例，也**絕不信任**遠端 MCP server 自行回報的 `annotations`（如 `readOnlyHint`）。操作者必須在本地 `loom.toml` 對應的 `[[mcp.servers]]` 條目中以 `tool_effects` 顯式宣告已知唯讀效果：

```toml
[[mcp.servers]]
name         = "minimax_coding"
command      = "uvx"
args         = ["minimax-coding-plan-mcp", "-y"]
env          = { MINIMAX_API_KEY = "${MINIMAX_API_KEY}", MINIMAX_API_HOST = "${MINIMAX_API_HOST}" }
trust_level  = "guarded"
tool_effects = { web_search = "public_search", understand_image = "media_read" }
```

契約重點：

- **封閉詞彙**：僅接受 `public_search`（公開網頁搜尋：在 `SAFE`／`SELF` 走 Luna 審查、`YOLO` 通過硬護欄、皆套用敏感 query 與 routine network ceiling 檢查）與 `media_read`（唯讀看圖／媒體解析：在 `SELF` 有操作者時走日常規則放行，仍受路徑／憑證硬護欄與 routine network ceiling 約束；`SAFE` 與未宣告的無人值守呼叫照舊攔截）。
- **Per-server 隔離**：`tool_effects` 的鍵是該 server 暴露的**裸工具名**（例如 `web_search`），只對該 `[[mcp.servers]]` 生效，其他 server 就算暴露同名工具也不會繼承宣告。
- **Fail-closed**：未宣告、非 table 格式、未知效果值，或 `trust_level = "critical"` 皆會記錄警告並忽略（無唯讀 `scope_resolver`，在 `YOLO` 命中 `hard: undeclared_effects`）。
- **Mutating 關鍵字不可降級**：若工具名稱或描述命中 `loom/extensibility/mcp_client.py` 的 `_MUTATING_KEYWORDS`（如 `delete`、`write`、`overwrite`、`edit` 等），會拒絕套用唯讀宣告並印 `WARNING`，保留 `ToolCapability.MUTATES` 與必填 `justification` 參數。
- **未命中工具名警告**：連線取得遠端工具清單後，若 `tool_effects` 宣告了該 server 實際未暴露的工具名（例如打錯字寫成 `websearch`），會印一行 `WARNING` 點出 server 名與未命中名稱，並維持 fail-closed。

---

## 總結

| 通道 | 適用情境 |
|------|----------|
| MCP Server（`loom mcp serve`） | 把 Loom 工具開放給其他 agent 平台 |
| MCP Client（`[[mcp.servers]]`） | 直接吃外部 MCP 生態的 tools，並可於本地宣告 `tool_effects` |

> 全部走「對 session 一次 install」的模式，runtime 中不熱裝載 / 不熱卸載；調整 MCP 設定後需重啟 session／daemon。
