# loom.toml 參考

`loom.toml` 是 Loom 的主設定檔，放在專案根目錄（`./loom.toml`）。  
個人設定（API key 等）放在 `.env`，不放在這裡。

---

## 快速開始

```bash
cp loom.toml.example loom.toml
# 填入你的設定值
```

---

## 完整結構

```toml
[loom]           # 基本 metadata
[identity]       # Prompt Stack：SOUL / Agent / personality
[cognition]      # 模型設定
[memory]         # 記憶層設定
[harness]        # Middleware / trust 設定
[autonomy]       # Autonomy daemon 總開關
[[autonomy.schedules]]   # Cron 排程（可多個）
[[autonomy.triggers]]    # Event 觸發（可多個）
[notify]         # 通知層設定
[mcp]            # MCP Server/Client 設定（v0.2.6.0）
[session]        # Session 行為設定（v0.2.6.0）
```

---

## [loom]

純 metadata，目前不影響執行行為。

```toml
[loom]
name    = "loom"
version = "0.1.0"
```

---

## [identity]

控制 Prompt Stack 三層組合。每層在 session 啟動時載入並拼接成 system prompt。

```toml
[identity]
soul        = "SOUL.md"        # 核心身份，永駐 context，建議不常動
agent       = "Agent.md"       # 專案 / 環境描述，agent 可自己改寫
personality = ""               # 臨時認知濾鏡，留空 = 不啟用
                               # 例："personalities/adversarial.md"
```

| 欄位 | 類型 | 說明 |
|------|------|------|
| `soul` | string | SOUL.md 路徑（相對於 cwd） |
| `agent` | string | Agent.md 路徑 |
| `personality` | string | personalities/ 下的任意 .md，空字串停用 |

**可用 personality：** `adversarial` · `minimalist` · `architect` · `researcher` · `operator`

---

## [cognition]

```toml
[cognition]
default_model = "claude-sonnet-4-6"
max_tokens    = 8096
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `default_model` | string | `"MiniMax-M2.7"` | `loom autonomy start` / `loom discord start` 使用的模型；CLI 優先用 `--model` flag |
| `max_tokens` | integer | `8096` | LLM 每次呼叫的最大輸出 token |

**模型前綴路由：**

| 前綴 | Provider | 需要的 .env key |
|------|----------|----------------|
| `MiniMax-*` | MiniMax | `MINIMAX_API_KEY` |
| `claude-*` | Anthropic | `ANTHROPIC_API_KEY` |

---

## [memory]

```toml
[memory]
backend                     = "sqlite"
db_path                     = "~/.loom/memory.db"
episodic_retention_days     = 7
skill_deprecation_threshold = 0.3
episodic_compress_threshold = 30
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `backend` | string | `"sqlite"` | 目前只支援 sqlite |
| `db_path` | string | `"~/.loom/memory.db"` | SQLite 檔案路徑（支援 `~` 展開） |
| `episodic_retention_days` | integer | `7` | Episodic entries 保留天數 |
| `skill_deprecation_threshold` | float | `0.3` | Skill EMA confidence 低於此值標記為 deprecated |
| `episodic_compress_threshold` | integer | `30` | 每 session 累積多少條**未壓縮** episodic entries 就壓縮成 semantic facts；Discord 長跑 session 建議調低至 `10`。單批上限 60 條（超過的最舊部分會被截斷）。 |

### 記憶壓縮流程

```
每次 tool call → EpisodicMemory.write()
    ↓
每個 turn 結束，若未壓縮 episodic count ≥ episodic_compress_threshold
    → compress_session()
    → Admission Gate（MemoryGovernor）過濾低品質事實
    → 轉換為 FACT → MemoryGovernor.governed_upsert()
    → 矛盾偵測 → SemanticMemory.upsert() 或跳過
    → 舊 episodic entries soft-delete（compressed_at 標記）
    ↓
Bot 關機 / session.stop()
    → 最終一次 compress_session()
    → MemoryGovernor.run_decay_cycle()（TTL 硬刪 compressed_at 過期行 + 衰減條目）
```

**Soft-delete 設計**：壓縮後的 episodic rows 不立即刪除，只在 `compressed_at` 寫入時間戳；
- `count_session()` / `compress_session()` 預設只看未壓縮 rows，避免重複處理
- `read_session()` 預設讀全部（包含已壓縮），reflection / session_summary 仍能取得完整軌跡
- TTL 到期由 `MemoryGovernor._prune_episodic_ttl` 統一硬刪，依 `episodic_retention_days`
- 若 LLM 抽 FACT 時漏掉關鍵資訊，原始 episodic 仍在磁碟上,可在保留期內 audit / 重抽

---

## [memory.governance]（v0.2.9.0）

Memory Governance 治理層的細部設定。所有欄位均有預設值，可不設定。

```toml
[memory.governance]
admission_threshold      = 0.5    # Admission Gate 門檻，0.0–1.0
episodic_ttl_days        = 30     # Episodic entries TTL（天）
semantic_decay_threshold = 0.1    # Semantic prune 門檻
relational_decay_factor  = 1.5    # Dreaming triples 加速衰減係數
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `admission_threshold` | float | `0.5` | Admission Gate：低於此分數的事實在壓縮時被過濾 |
| `episodic_ttl_days` | integer | `30` | Episodic TTL：超過此天數的 episodic entries 在 decay cycle 中刪除 |
| `semantic_decay_threshold` | float | `0.1` | Semantic prune：有效信心值低於此閾值的條目被清除 |
| `relational_decay_factor` | float | `1.5` | Dreaming triples 的加速衰減係數，有效半衰期 = 90 / factor 天 |

---

## [session]（v0.2.6.0）

控制 Session 行為選項。

```toml
[session]
prefetch_enabled = false    # Predictive Memory Pre-fetcher
prefetch_top_n   = 3        # 每次批次前預取的記憶條數
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `prefetch_enabled` | boolean | `false` | 是否在每個並行工具批次前執行輕量搜尋並注入相關記憶 |
| `prefetch_top_n` | integer | `3` | 每次預取的記憶條數（結果以 ephemeral 注入，不進入歷史）|

> `prefetch_enabled` 預設為 `false`，不影響既有行為。開啟後，工具批次執行前自動查詢 `MemorySearch` 並將 top N 結果注入上下文，批次結束後自動移除，保持歷史乾淨。失敗時完全非 fatal（包在 try/except 中）。

---

## [harness]

```toml
[harness]
default_trust_level = "guarded"
require_audit_log   = true
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `default_trust_level` | string | `"guarded"` | 工具預設信任等級（`"safe"` / `"guarded"` / `"critical"`） |
| `require_audit_log` | boolean | `true` | 是否啟用 audit log（目前為記錄用，不影響執行） |

**Trust Level 行為：**

| 等級 | 行為 |
|------|------|
| `safe` | 直接執行，無需確認 |
| `guarded` | 首次需確認，session 內後續自動允許；`exec_auto=true` 時支援工作區範圍內工具的白名單免確認 |
| `critical` | 每次都需確認 |

---

## [autonomy]

Autonomy Engine 的總開關與時區設定。

```toml
[autonomy]
enabled  = true
timezone = "Asia/Taipei"   # IANA timezone
```

| 欄位 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | false 時 `load_config()` 不載入任何 trigger |
| `timezone` | string | `"UTC"` | 全域預設時區，可被每個 schedule 覆蓋 |

---

## [[autonomy.schedules]]

Cron 排程，可定義多個（TOML 陣列語法 `[[ ]]`）。

```toml
[[autonomy.schedules]]
name         = "morning_briefing"
cron         = "0 9 * * *"
intent       = "抓取今日重要新聞，寫入 news/YYYY-MM-DD/briefing.md"
timezone     = "Asia/Taipei"     # 覆蓋全域 timezone
trust_level  = "safe"
notify       = false
notify_thread = 0                # Discord thread ID，0 = 預設 notify channel
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `name` | string | ✅ | trigger 唯一識別名稱 |
| `cron` | string | ✅ | 5-field cron 表達式（見下） |
| `intent` | string | ✅ | 自然語言描述任務；直接傳入 LLM 作為 prompt |
| `timezone` | string | | 覆蓋 `[autonomy]` 的全域時區 |
| `trust_level` | string | | `"safe"` / `"guarded"` / `"critical"`，預設 `"guarded"` |
| `notify` | boolean | | 是否需要人工確認（見下方說明），預設 `true` |
| `notify_thread` | integer | | 結果投遞的 Discord thread ID，`0` = 用 `--notify-channel` |

### Cron 語法

```
┌─── minute  (0–59)
│  ┌─── hour    (0–23)
│  │  ┌─── day of month (1–31)
│  │  │  ┌─── month  (1–12)
│  │  │  │  ┌─── day of week (0=Sun … 6=Sat)
│  │  │  │  │
*  *  *  *  *
```

| 範例 | 說明 |
|------|------|
| `0 9 * * *` | 每天 09:00 |
| `30 17 * * 1-5` | 週一到週五 17:30 |
| `0 0 * * 0` | 每週日午夜 |
| `*/30 * * * *` | 每 30 分鐘 |

### trust_level + notify 互動

| trust_level | notify | 行為 |
|------------|--------|------|
| `safe` | 任意 | 直接執行，不發確認 |
| `guarded` | `false` | 直接執行，不發確認 |
| `guarded` | `true` | Discord 發 Allow/Deny 按鈕，等待 60 秒；無回應 → 跳過 |
| `critical` | 任意 | 必須確認，等待 300 秒 |

---

## [[autonomy.triggers]]

事件觸發，由 `loom autonomy emit <event_name>` 或程式呼叫觸發。

```toml
[[autonomy.triggers]]
name         = "deploy_check"
event        = "deployment_done"   # 與 emit 的 event_name 對應
intent       = "跑 smoke test 並回報結果"
trust_level  = "guarded"
notify       = true
notify_thread = 0
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `name` | string | ✅ | trigger 名稱 |
| `event` | string | ✅ | 監聽的 event 名稱（預設等於 `name`） |
| `intent` | string | ✅ | 任務描述 |
| `trust_level` | string | | 同 schedules |
| `notify` | boolean | | 同 schedules |
| `notify_thread` | integer | | 同 schedules |

---

## [notify]

```toml
[notify]
default_channel = "cli"
```

| 欄位 | 類型 | 說明 |
|------|------|------|
| `default_channel` | string | 目前為參考用；實際通知通道由啟動指令決定 |

**實際通道由啟動方式決定：**

| 啟動方式 | 通知通道 |
|---------|---------|
| `loom autonomy start` | CLI（terminal 輸入確認） |
| `loom discord start --autonomy` | DiscordBotNotifier（Allow/Deny 按鈕） |

---

## [mcp]（v0.2.6.0）

Model Context Protocol 整合設定。支援多個外部 MCP 伺服器。

```toml
[[mcp.servers]]
name        = "filesystem"
command     = "npx"
args        = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
trust_level = "safe"
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `name` | string | ✅ | 伺服器唯一識別名稱；工具前綴（如 `filesystem:list_files`）|
| `command` | string | ✅ | 啟動命令（絕對路徑或 PATH 中的可執行檔）|
| `args` | list[string] | ✅ | 命令列參數 |
| `trust_level` | string | | 此伺服器所有工具的預設 trust level，`"safe"` / `"guarded"` |

> **MCP Server**（`loom mcp serve`）與 **MCP Client**（`[[mcp.servers]]`）為同一設定區塊。SAFE 工具自動暴露至 MCP；GUARDED 工具標記擴展欄位；CRITICAL 不暴露。

---

## .env 環境變數

API key 一律放 `.env`，不放 `loom.toml`。

```env
# LLM Providers
MINIMAX_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here       # optional

# Discord bot (loom discord start)
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=123456789          # optional: restrict to one channel
DISCORD_USER_ID=987654321             # optional: restrict to one user

# Web search (fetch_url + web_search tools)
brave_search_key=your_brave_key       # optional

# MCP（loom mcp serve）
# MCP SDK 由 pip install -e ".[mcp]" 安裝
```

---

## 啟動指令快速參考

```bash
# CLI 對話
loom chat
loom chat --model claude-sonnet-4-6
loom chat --resume                    # 續接上次 session
loom chat --tui                       # TUI 介面

# Discord bot（含 Autonomy）
loom discord start --autonomy --channel <CHANNEL_ID>
loom discord start --autonomy \
  --channel <CHANNEL_ID> \
  --notify-channel <NOTIFY_CHANNEL_ID> \
  --autonomy-config loom.toml \
  --autonomy-interval 60

# Autonomy daemon（純 CLI，無 Discord）
loom autonomy start --config loom.toml
loom autonomy status
loom autonomy emit <event_name>

# MCP
loom mcp serve                        # 啟動 Loom MCP Server
loom mcp connect "npx -y @modelcontextprotocol/server-filesystem /tmp"

# 記憶
loom memory list
loom reflect --session <session_id>
```
