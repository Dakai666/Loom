# CLI 命令

Loom CLI 提供命令列介面來操作 Loom 的各個功能。

---

## 安裝

```bash
pip install loom[api]    # 含 CLI
# 或
uv tool install loom-cli
```

---

## 全域選項

| 選項 | 說明 |
|------|------|
| `--version` | 顯示版本 |
| `--config <path>` | 指定 loom.toml 位置 |
| `--debug` | 開啟除錯日誌 |

---

## 聊天命令

### `loom chat`

```bash
loom chat [options]

# 選項
-p, --personality <name>   人格名稱（預設: architect）
-s, --session <id>         指定 session ID，不指定則新建
-r, --resume              恢復上次 session（等同 -s @last）
--model <model>             覆寫預設模型（loom.toml 中的 default_model）
-t, --tui                  啟動 TUI 介面（Textual）
```

**範例**

```bash
loom chat                              # 新 session，architect 人格
loom chat -p minimalist                # minimalist 人格
loom chat -r                           # 恢復上次 session
loom chat --session abc123             # 指定 session
loom chat --tui                        # TUI 介面
loom chat --tui --session abc123       # TUI + 指定 session
```

---

## 對話中 HITL 命令

在 `loom chat` 或 TUI 的輸入框中即時輸入：

| 命令 | 功能 |
|------|------|
| `/pause` | 切換 HITL 模式：agent 在每批工具執行完畢後暫停，等候人類決策再繼續下一個 LLM 回合 |
| `/stop` | 立即取消當前 turn，不等候邊界；部分輸出保留（相當於 TUI 的 Escape）|
| `/new` | 結束當前 session，開始全新 session |
| `/think` | 開啟上一回覆的完整推理鏈（ThinkModal）|
| `/compact` | 手動觸發對話上下文壓縮 |
| `/sessions` | 開啟 session 選擇器，切换至其他 session |
| `/personality <name>` | 切換認知人格（adversarial / minimalist / architect / researcher / operator）|
| `/personality off` | 移除當前人格設定 |
| `/verbose` | Toggle 工具輸出詳細度 |
| `/help` | 顯示所有命令與快捷鍵的 HelpModal |

> `/pause` 模式下的操作：
> - `r` + Enter — 直接恢復，不改變
> - `c` — 取消其餘回合
> - 任意文字 — 注入為重導向訊息並恢復

---

## 記憶命令

### `loom memory`

```bash
loom memory <subcommand>

# 子命令
list                       # 列出最近 N 筆 semantic entries
search <query>             # Phase 5 向量相似度 → FTS5 BM25 → recency 三層混合搜尋
stats                      # 顯示各記憶類型的 count
```

**範例**

```bash
loom memory list                # 列出 recent entries
loom memory search "信任層級"   # 混合搜尋
loom memory stats               # 顯示計數
```

### `loom recall`

`loom recall` 不是獨立 CLI 命令，而是 **Agent 工具**（由 LLM 在對話中主動呼叫）。Agent 可透過 `recall(query)` 直接召回相關記憶，見 [12-Memory-Index.md](12-Memory-Index.md)。

---

## Autonomy 命令

### `loom autonomy`

```bash
loom autonomy <subcommand>

# 子命令
start              # 前台啟動 AutonomyDaemon（blocking）
status             # 列出已註冊的觸發器與開關狀態
emit <event_name>  # 手動發送事件，觸發 EventTrigger
```

**範例**

```bash
loom autonomy start               # 前台啟動 daemon
loom autonomy status             # 查看觸發器狀態
loom autonomy emit deploy_done    # 觸發 deploy_done 事件
```

> `stop`、`restart`、`logs` 子命令**不存在**。Daemon 的生命週期由 OS 服務管理工具（systemd / supervisord）控制。

---

## Trigger 管理（`emit` 指令）

loom autonomy 沒有獨立的 `trigger` 子命令。觸發器在 `loom.toml` 中靜態定義，由 daemon 啟動時一次性載入，**不支援 runtime 新增/刪除/啟停**。

---

## MCP 命令（v0.2.6.0）

### `loom mcp`

```bash
loom mcp <subcommand>

# 子命令
serve                # 啟動 Loom MCP Server（將 Loom 工具暴露給 MCP 客戶端）
connect <command>    # 探索 MCP 伺服器上的可用工具（不實際連接）
```

**範例**

```bash
# 啟動 Loom MCP Server
loom mcp serve

# 探索 MCP 工具
loom mcp connect "npx -y @modelcontextprotocol/server-filesystem /tmp"
```

---

## Reflection 命令

### `loom reflect`

```bash
loom reflect [options]

# 選項
--session <id>    指定 session（預設: 當前 session）
--format <fmt>   輸出格式：text / json（預設: text）
```

**範例**

```bash
loom reflect                          # 反思當前 session
loom reflect --session abc123         # 指定 session
```

---

## Session 管理

### `loom sessions`

```bash
loom sessions <subcommand>

# 子命令
list              # 列出所有 session（id / 建立時間 / 最近更新）
show <id>         # 顯示 session 詳細資訊
delete <id>       # 刪除指定 session
export <id>       # 匯出 session 歷史（JSON）
```

**範例**

```bash
loom sessions list            # 列出所有 session
loom sessions show abc123     # 查看 abc123 詳情
loom sessions delete abc123   # 刪除
```

---

## Discord Bot 命令（v0.2.3.1）

### `loom discord`

```bash
loom discord start [options]

# 必填選項
--token <token>              # Discord Bot Token
--channel <channel_id>       # 允許的频道 ID

# 選項
--autonomy                    # 同時啟動 AutonomyDaemon
--notify-channel <id>         # 通知投遞的專用频道 ID
--autonomy-config <path>     # Autonomy loom.toml 路徑
--autonomy-interval <secs>   # Autonomy 輪詢間隔（預設 60s）
```

**範例**

```bash
# 啟動 Discord Bot（含 Autonomy）
loom discord start \
  --token "$DISCORD_BOT_TOKEN" \
  --channel 123456789 \
  --autonomy \
  --notify-channel 987654321

# 純 Bot（無 Autonomy）
loom discord start --token "$DISCORD_BOT_TOKEN" --channel 123456789
```

Discord Bot 的 slash commands 與 CLI 對話命令完全對應：`/new` `/sessions` `/think` `/compact` `/personality` `/verbose` `/pause` `/stop` `/budget` `/help`。

GUARDED/CRITICAL 工具的確認透過 Discord Button（Allow/Deny）處理，60 秒超時。

---

## Plugin 管理

### `loom plugin`

```bash
loom plugin <subcommand>

# 子命令
list              # 列出已安裝的 plugins（來自 ~/.loom/plugins/）
info <name>       # 查看 plugin 詳細資訊
```

**範例**

```bash
loom plugin list            # 列出已安裝 plugins
loom plugin info git        # 查看 git plugin 資訊
```

> `install`、`uninstall`、`update`、`search` 子命令**不存在**。Plugin 透過放入 `~/.loom/plugins/<name>/` 目錄由 Loom 自動發現。

---

## 設定命令

### `loom config`

```bash
loom config <subcommand>

# 子命令
show              # 顯示當前 loom.toml 內容（解析後）
validate          # 驗證 loom.toml 格式
```

> `set`、`get`、`edit`、`reset` 子命令**不存在**。設定編輯請直接修改 `loom.toml`。

---

## Tool 管理

### `loom tool`

```bash
loom tool <subcommand>

# 子命令
list              # 列出所有已註冊的工具（名稱 / trust_level / capabilities）
info <name>       # 顯示工具詳細資訊（description / schema）
```

**範例**

```bash
loom tool list              # 列出所有工具
loom tool info read_file    # 查看 read_file 詳細資訊
```

> `test`、`validate` 子命令**不存在**。

---

## 不存在的命令

以下命令在部分文件版本中出現，但**不屬於 Loom CLI**：

| 錯誤命令 | 說明 |
|---------|------|
| `loom task` | Task Scheduler 不是 CLI 工具，由 Session 內部使用 |
| `loom init` | loom.toml 需手動建立，無 init 向導 |
| `loom trigger enable/disable/add/remove` | 觸發器在 loom.toml 中靜態管理 |
| `loom plugin install/uninstall/update/search` | Plugin 透過目錄放置自動發現 |
| `loom config set/get/edit/reset` | 設定直接修改 loom.toml |
| `loom tool test/validate` | 工具由 LoomSession 內部呼叫 |
| `loom recall` | 是 Agent 工具，非 CLI 命令 |
| `loom autonomy stop/restart/logs` | Daemon 生命週期由 OS 管理工具控制 |

---

## 總結

| 命令 | 功能 |
|------|------|
| `loom chat` | 互動式聊天（TUI/CLI） |
| `loom chat --tui` | Textual TUI 介面 |
| `/pause` | HITL 模式：批次工具執行後暫停等候確認 |
| `/stop` | 立即取消當前 turn |
| `/new` | 開始全新 session |
| `/think` | 顯示推理鏈 |
| `/compact` | 手動壓縮上下文 |
| `/sessions` | Session 選擇器 |
| `/personality` | 切換人格 |
| `/verbose` | Toggle 工具輸出詳細度 |
| `/help` | 幫助面板 |
| `loom memory list` | 列出 recent entries |
| `loom memory search` | Phase 5 混合搜尋 |
| `loom memory stats` | 記憶統計 |
| `loom reflect` | Reflection 分析 |
| `loom sessions list` | 列出 sessions |
| `loom sessions show` | 查看 session |
| `loom sessions delete` | 刪除 session |
| `loom sessions export` | 匯出 session |
| `loom plugin list` | 列出 plugins |
| `loom plugin info` | Plugin 詳細資訊 |
| `loom config show` | 顯示設定 |
| `loom config validate` | 驗證設定 |
| `loom tool list` | 列出工具 |
| `loom tool info` | 工具詳細資訊 |
| `loom autonomy start` | 啟動 daemon |
| `loom autonomy status` | 查看觸發器狀態 |
| `loom autonomy emit` | 觸發事件 |
| `loom mcp serve` | 啟動 MCP Server |
| `loom mcp connect` | 探索 MCP 工具 |
| `loom discord start` | 啟動 Discord Bot（含可選 Autonomy）|
