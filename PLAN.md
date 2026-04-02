# Loom — Framework Planning Document

> *The loom is what the harness belongs to. Claude Code is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

---

## 1. Vision

Loom 是一個 **harness-first、memory-native、self-directing** 的 agent 框架。

它的目標不是取代 Claude Code，而是成為一個能夠：
- 從任何優秀的 agent 專案中**學習並吸收其設計精華**
- 在沒有人類觸發的情況下**自主判斷行動**
- 主動找到使用者、**推送通知或請求確認**
- 以**插件方式無縫擴充**新能力，不需要修改核心

---

## 2. 設計信條

| 信條 | 含義 |
|------|------|
| **Harness-first** | 工具生命週期是一等公民，不是功能附加物 |
| **Memory-native** | 記憶是架構的 substrate，不是 plugin |
| **Reflexive** | Agent 能觀察並推理自己的執行路徑 |
| **Absorbent** | 框架本身能學習其他框架的優點並整合進來 |
| **Self-directing** | 可在無人觸發下自主決策並行動 |

---

## 3. 核心架構

```
┌───────────────────────────────────────────────────────────┐
│                      Platform Layer                        │
│           CLI · IDE Extension · REST API · Bot             │
│                  Webhook · Notification                    │
├───────────────────────────────────────────────────────────┤
│                     Cognition Layer                        │
│         LLM Router · Context Budget · Reflection API       │
├───────────────────────────────────────────────────────────┤
│                      Harness Layer          ← 骨幹         │
│   Middleware Pipeline · Tool Registry · Trust Hierarchy    │
│              Blast Radius Control · Audit Log              │
├───────────────────────────────────────────────────────────┤
│                      Memory Layer                          │
│    Episodic · Semantic · Procedural (Skills) · Relational  │
├───────────────────────────────────────────────────────────┤
│                      Task Engine                           │
│            DAG Graph · Scheduler · Subagent Pool           │
├───────────────────────────────────────────────────────────┤
│                     Autonomy Engine                        │
│       Cron Runtime · Trigger Evaluator · Action Planner    │
├───────────────────────────────────────────────────────────┤
│                   Extensibility Layer                      │
│         Lens System · Adapter Registry · Skill Import      │
└───────────────────────────────────────────────────────────┘
```

---

## 4. 各層詳細設計

### 4.1 Harness Layer ✅ 已實作

Claude Code 是兩點式 (`PreToolUse` / `PostToolUse`)。
Loom 改為**全管道 Middleware Pipeline**：

```python
pipeline = MiddlewarePipeline([
    LogMiddleware(console),         # 終端機輸出每次 tool call
    TraceMiddleware(on_trace=...),  # 執行追蹤 → 寫入 Episodic Memory
    BlastRadiusMiddleware(          # 影響範圍評估 + 用戶確認
        perm_ctx=perm,
        confirm_fn=confirm,
    ),
])
```

**實際實作的 Middleware：**

| Middleware | 功能 |
|------------|------|
| `LogMiddleware` | Rich 格式化輸出每次工具調用與結果 |
| `TraceMiddleware` | 計時 + 非同步回呼，每次 tool result 寫入 episodic memory |
| `BlastRadiusMiddleware` | 依 TrustLevel 決定是否需要確認；GUARDED 首次確認後 session 內免再詢問；CRITICAL 每次都需確認 |

**Tool Trust Hierarchy（三級）：**

| 等級 | 類型 | 行為 |
|------|------|------|
| `safe` | 唯讀、本地、可逆 | 自動執行，session 開始時預授權 |
| `guarded` | 寫入、網路、有副作用 | 首次需確認，session 內授權後免再詢問 |
| `critical` | 破壞性、跨系統、不可逆 | 每次強制確認，不可 session 授權 |

**ToolRegistry** 同時提供 `to_anthropic_schema()` 和 `to_openai_schema()`，供不同 provider 使用。

---

### 4.2 Memory Layer ✅ 已實作

**四種記憶類型（SQLite WAL 後端）：**

| 類型 | 內容 | 生命週期 | 特性 |
|------|------|----------|------|
| `episodic` | 本次 session 發生的事 | session 結束後壓縮 | 順序性，每次 tool call 自動寫入 |
| `semantic` | 關於世界 / codebase 的事實 | 長期，可驗證失效 | upsert by key、substring search |
| `procedural` | 學到的技能、有效的做法 | 長期，有版本號 | Skill Genome（含 EMA confidence）|
| `relational` | 用戶偏好、協作風格 | 長期，可更新 | Phase 4 展開 |

**Skill Genome（procedural memory 的核心）：**

```yaml
skill:
  id: "refactor_extract_function"
  version: 3
  confidence: 0.87          # EMA，從使用結果回饋計算
  usage_count: 14
  success_rate: 0.92
  parent_skill: "refactor_base"
  deprecation_threshold: 0.3   # confidence ≤ 此值時自動廢棄
  tags: ["refactor", "python", "function"]
  body: |
    當函數超過 30 行且有明顯可分離邏輯時...
```

**Session 壓縮流程：**
session 結束時，episodic entries 送入 LLM 提取 FACT，寫入 semantic memory，帶 `source: session:<id>` 標記。

---

### 4.3 Task Engine ✅ 已實作

DAG（有向無環圖），Kahn's topological sort 演算法，自動推導可並行節點：

```python
graph = TaskGraph()
a = graph.add("分析現有 API 結構")
b = graph.add("設計新端點 schema",    depends_on=[a])
c = graph.add("實作端點",             depends_on=[b])
d = graph.add("撰寫測試",             depends_on=[b])   # c, d 可並行
e = graph.add("更新 API 文檔",        depends_on=[c, d])

plan = graph.compile()
# plan.levels     == [[a], [b], [c, d], [e]]
# plan.parallel_groups == [[c, d]]
```

`TaskScheduler` 使用 `asyncio.gather` 在同一 level 內並發執行，支援 `stop_on_failure` 使下游節點自動 SKIP。

---

### 4.4 Cognition Layer ✅ 已實作

#### LLM Router

模型名稱前綴自動路由到正確 provider：

| 前綴 | Provider |
|------|----------|
| `MiniMax-` / `minimax-` | MiniMaxProvider（minimax.io，OpenAI-compatible） |
| `claude-` | AnthropicProvider |
| `gpt-` | （預留）OpenAI |

**MiniMax 特殊處理：**
- 標準 OpenAI tool_calls 優先
- 若 content 含 `<minimax:tool_call>` XML，啟用 fallback XML parser
- 訊息格式統一為 OpenAI-canonical（internal），Anthropic 轉換在 provider 內部處理

**`_to_anthropic_messages()`：**
將 OpenAI-canonical message history 轉為 Anthropic wire format，包含 tool result 合併為 user message 的正確處理。

#### Context Budget Manager

- 總 token 上限：MiniMax M2.7 = 204,800 tokens
- 壓縮閾值：80%（可設定）
- `record_response(input, output)` 追蹤實際用量（replace，不 accumulate）
- `should_compress()` 觸發後呼叫 `_smart_compact()`

#### Reflection API

| 方法 | 回傳 |
|------|------|
| `session_summary(sid)` | 一段 session 活動摘要 |
| `recent_tool_calls(sid, n)` | 最近 n 次 tool call（most-recent-first）|
| `tool_success_rate(sid)` | 各工具 per-session 成功率 |
| `skill_health_report()` | 所有活躍 skill 的 confidence/usage 統計 |

---

### 4.5 Autonomy Engine ✅ 已實作

#### 觸發機制（三種）

```
┌─────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  CronTrigger    │   │  EventTrigger    │   │ ConditionTrigger │
│  "0 9 * * 1-5" │   │  emit("deploy")  │   │  lambda: cpu>80  │
└────────┬────────┘   └────────┬─────────┘   └────────┬─────────┘
         └────────────────────►▼◄────────────────────────┘
                         TriggerEvaluator
                    (每分鐘 evaluate_cron + poll)
```

**CronTrigger** 支援完整 5-field cron 語法：`*`、exact、range、list、step（`*/n`）。
Weekday 對齊標準 cron 慣例（0=Sunday），內部轉換 Python `weekday()`。

#### Action Planner 決策管道

```
Trigger fires
  → Context Assembly（recent semantic facts + fire_context）
  → Trust Level 解析
  → Decision mapping：
      safe     → EXECUTE（直接執行）
      guarded  + notify=true  → NOTIFY（通知 + 等待確認）
      guarded  + notify=false → EXECUTE
      critical → HOLD（強制確認，timeout 不降級）
      disabled → SKIP
```

#### Autonomy Daemon

從 `loom.toml` 載入 triggers，背景 loop 每 60 秒評估一次：

```bash
loom autonomy start              # 啟動前台 daemon
loom autonomy status             # 顯示已載入的 triggers
loom autonomy emit <event_name>  # 手動觸發 EventTrigger
```

---

### 4.6 Notification Layer ✅ 已實作

#### NotificationRouter

fan-out 到所有已註冊 notifier，使用 `asyncio.gather`；單一 channel 失敗不阻擋其他 channel。

#### NotificationType

| 類型 | 用途 |
|------|------|
| `INFO` | 純資訊，不需回應 |
| `CONFIRM` | yes/no，有 timeout 後返回 TIMEOUT |
| `INPUT` | 需要用戶輸入才能繼續 |
| `ALERT` | 緊急警告 |
| `REPORT` | 週期性摘要（autonomy 執行結果） |

#### 已實作 Notifier

| Notifier | 狀態 | 說明 |
|----------|------|------|
| `CLINotifier` | ✅ | Rich Panel 輸出 + stdin 確認 |
| `WebhookNotifier` | ✅ | HTTP POST JSON + `push_reply()` 解鎖 `wait_reply()` |
| `TelegramNotifier` | ✅ | Bot API sendMessage + `/yes` `/no` reply hook |

#### ConfirmFlow

```python
flow = ConfirmFlow(send_fn=router.send, wait_fn=cli.wait_reply)
result = await flow.ask(notification)
# → APPROVED / DENIED / TIMEOUT
```

timeout 後返回 `ConfirmResult.TIMEOUT`；NOTIFY 路徑 timeout 時發送 INFO 告知已跳過。

---

## 5. 實際目錄結構

```
Project_Next/
├── loom.toml                        # 主設定檔
├── pyproject.toml                   # Python 套件定義
├── loom/
│   ├── __init__.py
│   ├── core/
│   │   ├── harness/
│   │   │   ├── __init__.py
│   │   │   ├── middleware.py        # ToolCall/ToolResult + Pipeline + 3 middleware
│   │   │   ├── permissions.py       # TrustLevel + PermissionContext
│   │   │   └── registry.py          # ToolDefinition + ToolRegistry
│   │   ├── memory/
│   │   │   ├── __init__.py
│   │   │   ├── store.py             # SQLiteStore（WAL 模式，統一初始化）
│   │   │   ├── episodic.py          # EpisodicEntry + EpisodicMemory
│   │   │   ├── semantic.py          # SemanticEntry + SemanticMemory
│   │   │   ├── procedural.py        # SkillGenome（EMA） + ProceduralMemory
│   │   │   ├── index.py             # MemoryIndex + MemoryIndexer（輕量目錄）
│   │   │   └── search.py            # MemorySearch（BM25 + TF-IDF 加權搜尋）
│   │   ├── cognition/
│   │   │   ├── __init__.py
│   │   │   ├── providers.py         # LLMProvider / MiniMaxProvider / AnthropicProvider（async client reuse）
│   │   │   ├── router.py            # LLMRouter（prefix routing）+ stream_chat
│   │   │   ├── context.py           # ContextBudget（replace semantics on record_response）
│   │   │   ├── reflection.py        # ReflectionAPI
│   │   │   └── prompt_stack.py      # PromptStack（SOUL → Agent → Personality 三層棧）
│   │   └── tasks/
│   │       ├── __init__.py
│   │       ├── graph.py             # TaskNode + TaskGraph（Kahn's sort）
│   │       └── scheduler.py         # TaskScheduler（asyncio.gather per level）
│   ├── autonomy/
│   │   ├── __init__.py
│   │   ├── triggers.py              # CronTrigger / EventTrigger / ConditionTrigger
│   │   ├── evaluator.py             # TriggerEvaluator（cron dedup + event emit + condition poll）
│   │   ├── planner.py               # ActionPlanner（trust→decision + prompt builder）
│   │   └── daemon.py                # AutonomyDaemon（config loader + execute_plan）
│   ├── notify/
│   │   ├── __init__.py
│   │   ├── types.py                 # NotificationType / Notification / ConfirmResult
│   │   ├── router.py                # NotificationRouter（fan-out）
│   │   ├── confirm.py               # ConfirmFlow（send + wait + timeout）
│   │   └── adapters/
│   │       ├── __init__.py
│   │       ├── cli.py               # CLINotifier（Rich + stdin）
│   │       └── webhook.py           # WebhookNotifier + TelegramNotifier
│   ├── extensibility/
│   │   ├── __init__.py
│   │   ├── lens.py                  # BaseLens 抽象（extract_skills/middleware/adapters）
│   │   ├── hermes.py                # HermesLens（procedural memory 格式轉換）
│   │   ├── claw.py                  # ClawCodeLens（harness pattern 引入）
│   │   ├── pipeline.py              # Skill Import Pipeline（沙盒評估 + confidence gate）
│   │   └── adapter.py               # Adapter Registry（@loom.tool decorator）
│   └── platform/
│       └── cli/
│           ├── __init__.py
│           ├── main.py              # CLI entry（chat / memory / reflect / autonomy）
│           │                        # LoomSession + _smart_compact + stream_turn
│           ├── ui.py                # UI components（TextChunk/ToolBegin/ToolEnd/TurnDone）
│           │                        # PromptSession + SlashCompleter
│           └── tools.py             # 6 builtin tools（read_file/write_file/list_dir/run_bash/recall/memorize）
└── tests/
    ├── __init__.py
    ├── test_harness.py              # 29 tests — Harness Layer
    ├── test_memory.py               # 29 tests — Memory Layer
    ├── test_integration.py          # 18 tests — Pipeline×Memory + builtin tools + compression
    ├── test_cognition.py            # 39 tests — Cognition Layer + ReflectionAPI
    ├── test_tasks.py                # 32 tests — DAG + Scheduler
    ├── test_autonomy.py             # 59 tests — Autonomy + Notify
    ├── test_extensibility.py        # Lens system + Skill Import Pipeline
    └── test_prompt_stack.py         # PromptStack 三層注入
```

**測試總計：352 tests，全部通過，Python 3.14 / pytest 9.0**

---

## 6. 設定檔格式（`loom.toml`）— 當前版本

```toml
[loom]
name = "loom"
version = "0.1.0"

[cognition]
default_model = "MiniMax-M2.7"        # 主力 LLM
max_tokens = 8096

[memory]
backend = "sqlite"
db_path = "~/.loom/memory.db"
episodic_retention_days = 7
skill_deprecation_threshold = 0.3

[harness]
default_trust_level = "guarded"
require_audit_log = true

[autonomy]
enabled = false                        # 改為 true 啟動 daemon
timezone = "Asia/Taipei"

[[autonomy.schedules]]
name = "daily_review"
cron = "0 9 * * 1-5"                  # 週一至週五 09:00
intent = "回顧進度，整理優先任務，主動通知用戶"
trust_level = "guarded"
notify = true

[[autonomy.schedules]]
name = "weekly_memory_prune"
cron = "0 2 * * 0"                    # 每週日 02:00
intent = "清理低 confidence skill，壓縮過期 episodic memory"
trust_level = "safe"
notify = false

[[autonomy.triggers]]
name = "on_error_spike"
event = "error_rate_threshold"
intent = "分析近期錯誤，產生診斷報告，主動推送給用戶"
trust_level = "guarded"
notify = true

[notify]
default_channel = "cli"

[notify.telegram]
enabled = false
bot_token = ""
chat_id = ""
```

---

## 7. CLI 指令總覽

```bash
# 互動式 agent session
loom chat
loom chat --model MiniMax-M2.7-highspeed
loom chat --model claude-sonnet-4-6

# 記憶系統
loom memory list
loom memory list --limit 50

# 反射報告
loom reflect --session <session_id>

# 自主引擎
loom autonomy status              # 顯示 loom.toml 中的 triggers
loom autonomy start               # 啟動 daemon（前台，Ctrl-C 停止）
loom autonomy emit <event_name>   # 手動觸發 EventTrigger
```

---

## 8. 開發路線圖

### Phase 1：骨幹（Harness + Memory） ✅ 完成
- [x] Middleware Pipeline 核心引擎（LogMiddleware / TraceMiddleware / BlastRadiusMiddleware）
- [x] Tool Registry 與 Trust Hierarchy（三級，dual schema）
- [x] 四種 Memory 類型（SQLite WAL 後端）
- [x] Skill Genome 資料結構（EMA confidence，自動廢棄）
- [x] 基礎 CLI Platform（4 builtin tools）

### Phase 2：思考層（Cognition + Tasks） ✅ 完成
- [x] Multi-LLM Router（MiniMax-M2.7 主力 + Anthropic fallback）
- [x] MiniMax XML tool call fallback parser
- [x] Anthropic message format 轉換（`_to_anthropic_messages`）
- [x] Context Budget Manager（80% 壓縮閾值，MiniMax 204k context）
- [x] DAG Task Engine（Kahn's sort，cycle detection）
- [x] TaskScheduler（asyncio.gather，stop_on_failure）
- [x] Reflection API（session summary / tool success rate / skill health）

### Phase 3：自主層（Autonomy + Notify） ✅ 完成
- [x] CronTrigger（完整 5-field cron，標準 weekday 慣例）
- [x] EventTrigger + ConditionTrigger
- [x] TriggerEvaluator（dedup / emit / poll / run_forever）
- [x] Action Planner 決策管道（trust→decision，context assembly）
- [x] Autonomy Daemon（loom.toml 載入，execute_plan 三路分岔）
- [x] NotificationRouter（fan-out，per-channel send）
- [x] ConfirmFlow（timeout 降級，APPROVED / DENIED / TIMEOUT）
- [x] CLINotifier（Rich Panel + stdin y/n）
- [x] WebhookNotifier（HTTP POST + push_reply）
- [x] TelegramNotifier（Bot API + reply hook）
- [x] CLI 指令：`loom autonomy start / status / emit`

### Phase 4：學習層（Extensibility + Memory-as-Attention + Prompt Stack） ✅ 完成

#### 4A. 三層提示詞棧

Agent 運行時的提示詞不再是「一堆規則一次全讀」，而是有結構的三層棧：

```
┌─────────────────────────────────────────┐
│  Personality.md  ← 臨時疊加，可替換     │  session-scoped
├─────────────────────────────────────────┤
│  Agent.md        ← 專案/環境特定        │  project-scoped，可由 agent 自寫
├─────────────────────────────────────────┤
│  SOUL.md         ← 核心身份，全局通用   │  global，極精簡，永駐 context
└─────────────────────────────────────────┘
        注入順序：SOUL → Agent → Personality
        後層疊加補充，不覆蓋前層原則
```

**SOUL.md**（已建立）
- 核心身份、blast radius 思維、與用戶的互動原則
- 永駐 system prompt，約 300 tokens，永遠不增長

**Agent.md**（待建立）
- 當前專案技術棧、用戶協作偏好、專案特定的行為限制
- 類似 CLAUDE.md 的「活的版本」——agent 可在 session 中自主更新
- 存放於專案根目錄（`./Agent.md`）

**Personality.md**（待建立）
- 臨時疊加的認知濾鏡，不是角色扮演，而是思考視角的切換
- 存放於 `personalities/<name>.md`，可管理多份、隨時切換
- 內建視角範例：

| 名稱 | 核心問題 |
|------|---------|
| `adversarial` | 什麼會出錯？主動找破綻，質疑每個假設 |
| `minimalist` | 這真的必要嗎？刪除優先於新增 |
| `architect` | 五年後這個設計還成立嗎？審視系統邊界 |
| `researcher` | 我們還不知道什麼？探索邊緣案例 |
| `operator` | 凌晨三點這個會 pager 嗎？聚焦可靠性 |

啟動方式：
```toml
# loom.toml
[identity]
soul = "SOUL.md"
agent = "Agent.md"
personality = "personalities/adversarial.md"   # 預設人格
```

```bash
# session 中臨時切換（Phase 4 後期斜線指令）
/personality minimalist
/personality off    # 恢復無人格疊加
```

#### 4B. 記憶索引與主動召回（Memory as Attention）

Agent 面對的不是「每次讀一堆記憶」，而是面對**索引**，按需主動拉取。

**Push 模式（目前）→ Pull 模式（Phase 4）**

```
舊：session 開始 → 載入全部 semantic facts → context 被無關內容稀釋
新：session 開始 → 載入 MemoryIndex（輕量目錄）
                → agent 按需呼叫 recall(query) → 只取相關片段
```

**MemoryIndex（永駐 context，極輕量）**

```
Memory Index
─────────────────────────────────────
Semantic  : 47 facts  [topics: python, loom, minimax, testing]
Skills    : 12 active [tags: refactor, bash, git, python]
Episodes  : 8 sessions compressed
─────────────────────────────────────
Use recall(query) to retrieve relevant entries.
```

**`recall` 工具（SAFE，agent 自主呼叫）**

```python
# agent 自行決定何時、查什麼
recall(query="loom configuration defaults")
  → 返回相關 semantic facts（BM25 加權排序）

recall(query="last session errors", type="episodic")
  → 返回最近 episodic 壓縮摘要中的錯誤記錄

recall(query="refactor python function", type="skill")
  → 返回最匹配的 skill genomes
```

搜尋機制分兩階段：
- **Phase 4 初期**：BM25 / TF-IDF 關鍵字加權（純本地，無外部依賴）
- **Phase 4 後期**：embedding 相似度（MiniMax Embedding API）

**與現有架構對應**

| 現有 | Phase 4 升級 |
|------|-------------|
| `SemanticMemory.search(substring)` | 升級為 BM25 加權 + 相關性排序 |
| `ReflectionAPI.session_summary()` | 壓縮輸出同時更新 MemoryIndex |
| SOUL.md 全文注入 | Core 永駐 + Agent.md/Personality 按層載入 |
| — | `recall` tool（SAFE，自主召回）|
| — | `MemoryIndex` 生成與維護 |
| — | `memorize(key, value)` tool（GUARDED，agent 主動寫入 semantic）|

#### 4C. Extensibility（Lens 系統）

- [ ] BaseLens 抽象（`extract_skills / extract_middleware / extract_platform_adapters`）
- [ ] HermesLens（NousResearch/hermes-agent procedural memory 格式轉換）
- [ ] ClawCodeLens（instructkr/claw-code harness pattern 引入）
- [ ] Skill Import Pipeline（沙盒評估 + confidence gate）
- [ ] Adapter Registry 公開 API（`@loom.tool` decorator）
- [ ] Relational Memory 讀寫 API（用戶偏好 / 協作風格）

**Phase 4 完整 checklist：**
- [x] `Agent.md` 規格定義與 loader
- [x] `personalities/` 目錄結構 + 5 個內建人格文件（adversarial / minimalist / architect / researcher / operator）
- [x] 三層提示詞棧注入機制（SOUL → Agent → Personality，`PromptStack`）
- [x] `loom.toml` `[identity]` 區塊支援
- [x] `MemoryIndex` 資料結構與生成邏輯（`loom/core/memory/index.py`）
- [x] BM25 搜尋實作（`loom/core/memory/search.py`）
- [x] `recall` tool（接入 ToolRegistry，SAFE）
- [x] `memorize` tool（GUARDED，agent 寫入 semantic memory）
- [x] `/personality <name>` 斜線指令（session 中切換）
- [x] Extensibility：BaseLens + HermesLens + ClawCodeLens + Skill Import Pipeline + Adapter Registry
- [ ] Skill 評估回路接通（tool result → `SkillGenome.record_outcome()`）← 結構完整，尚未接線

### Phase 4.5：CLI Platform 成熟化 ✅ 完成

CLI 從最小原型升級為接近生產品質的互動介面：

**Streaming 架構（`ui.py` + `stream_turn()`）**
- 事件模型：`TextChunk / ToolBegin / ToolEnd / TurnDone` 型別化事件流
- `AsyncOpenAI` / `AsyncAnthropic` 真實 async streaming（client 在 `__init__` 建立，session 內複用）
- 直接 `console.print(chunk, end="")` 逐 token 輸出，無 Rich Live 問題

**Input 體驗（`prompt_toolkit`）**
- `PromptSession` + `InMemoryHistory`（↑/↓ 瀏覽歷史）
- `SlashCompleter`（Tab 自動補全斜線指令）
- 斜線指令：`/personality <name>` · `/compact` · `/help`

**Context 管理**
- `ContextBudget.record_response()` 改為 replace 語義（修正累加 bug）
- `_smart_compact()`：LLM 摘要最老的 1/2 輪次 → 2 條摘要訊息（節省 token 同時保留語義）
  - 僅在 `≥3` 個 user turn 時啟用（避免摘要空對話）
  - 失敗時 fallback 到 `_compress_context()`（安全的 turn-boundary 刪截）
- 自動觸發：每輪開頭 + 工具迴圈中（閾值 80%）
- 手動觸發：`/compact` 斜線指令

**Bug 修正**
- 雙重工具列：移除 `LogMiddleware`，改由 `ToolBegin/ToolEnd` 事件渲染
- Rich Live 阻擋 stdin：完全移除 Live，改用 `console.print(end="")`
- GUARDED 工具確認無響應：改用 `prompt_toolkit.prompt` via `run_in_executor`
- Session compression 無 fallback：LLM 未輸出 `FACT:` 前綴時保存原始摘要
- Dead code：移除未使用的 `run_turn()`

---

### Phase 5：生態 ⬜ 待開發
- [ ] REST API Platform（FastAPI）
- [ ] Discord / Slack / Email Notifier
- [ ] IDE Extension 支援（VS Code）
- [ ] Lens Marketplace 概念驗證
- [ ] 文檔網站
- [ ] Skill 評估回路接通（`record_outcome()` 接 tool result）
- [ ] TaskGraph 接入 LoomSession（自動並行多工具計畫）
- [ ] AutonomyDaemon 狀態持久化（trigger_history 表）
- [ ] Request timeout + Retry / Circuit Breaker（provider 層）
- [ ] Relational Memory 讀寫 API

---

## 9. 與現有專案的定位關係

| 特性 | Claude Code | hermes-agent | claw-code | **Loom** |
|------|-------------|--------------|-----------|----------|
| Harness 嚴謹度 | ★★★★★ | ★★☆☆☆ | ★★★★☆ | ★★★★★ |
| 記憶系統結構化 | ★★★☆☆ | ★★★★☆ | ★★☆☆☆ | ★★★★★ |
| 自主行動能力 | ★☆☆☆☆ | ★★☆☆☆ | ★☆☆☆☆ | ★★★★★ |
| 主動通知能力 | ★☆☆☆☆ | ★★★★☆ | ★☆☆☆☆ | ★★★★★ |
| 模型無關性 | ★☆☆☆☆ | ★★★★★ | ★★★☆☆ | ★★★★★ |
| 學習/擴充能力 | ★★★☆☆ | ★★★☆☆ | ★★☆☆☆ | ★★★★☆ |
| 反射/自我觀察 | ★★☆☆☆ | ★★☆☆☆ | ★★☆☆☆ | ★★★★☆ |

---

## 10. 已知邊界與下一步重點

| 項目 | 現況 | 下一步 |
|------|------|--------|
| Skill 自動評估回路 | Genome 結構完整，尚未接 tool result → `record_outcome()` | Phase 5 |
| TaskGraph 未接 LoomSession | DAG engine 完整但只在測試中使用 | Phase 5 |
| Relational Memory | 資料表已建立，尚未有讀寫 API | Phase 5 |
| AutonomyDaemon 狀態非持久化 | 重啟後 last_fire_time 丟失 | Phase 5（trigger_history 表）|
| Request timeout | provider 無 timeout 參數，網路阻塞時永久等待 | Phase 5 |
| Retry / Circuit Breaker | API 瞬斷直接失敗 | Phase 5 |
| BM25 index 每次重建 | `recall()` 每次重建語料庫，n 增大時 O(n) | Phase 5（cache）|
| Webhook reply endpoint | `push_reply()` 存在，需 HTTP server 接收外部回覆 | Phase 5 REST API |
| Windows asyncio pipe warning | Python 3.14 + Proactor 的已知問題，不影響功能 | 等 CPython 修復 |

### Code Review 問題處置（小晴 審查，2026-04-02）

| 問題 | 評估 | 處置 |
|------|------|------|
| C-1 Context compression 破壞完整性 | 已在 Phase 4.5 修復（safe turn-boundary cut） | ✅ 已修 |
| C-2 Session compression 無 fallback | 已修：LLM 未產生 `FACT:` 時儲存原始文字 | ✅ 已修 |
| C-3 SQLite transaction 管理 | aiosqlite 關閉時自動 commit，可接受 | 不處理 |
| C-4 每次 stream 創建新 HTTP client | 已修：client 移至 `__init__`，session 內複用 | ✅ 已修 |
| C-5 BM25 index 每次重建 | 效能問題，需 cache 機制 | Phase 5 |
| D-1 Relational Memory 空殼 | 已知缺口，資料表存在 | Phase 5 |
| D-2 Middleware chain 每次重建 | 微優化，影響忽略不計 | 不處理 |
| D-3 ConditionTrigger 不可序列化 | 設計限制，code-only 使用屬預期 | 不處理 |
| D-4 PermissionContext 非持久化 | session-scoped by design | 不處理 |
| D-5 AutonomyDaemon 狀態非持久化 | 真實問題 | Phase 5 |
| R-1 無 Request timeout | 生產環境風險 | Phase 5 |
| R-2 Cron 驗證不完整 | 隱藏 bug（如 `60 * * * *`） | Phase 5 |
| R-3 無 Cancellation 處理 | Ctrl-C 不取消 HTTP 請求 | Phase 5 |
| R-5 Blocking input with Live | 已在 Phase 4.5 修復（移除 Live） | ✅ 已修 |
| A-1 TaskGraph 未使用 | 已知，等 Phase 5 接入 LoomSession | Phase 5 |
| N-5 `run_turn()` dead code | 已移除 | ✅ 已修 |
