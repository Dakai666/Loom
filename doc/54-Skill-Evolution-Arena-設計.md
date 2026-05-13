# Skill Evolution Arena 設計文件 — Quest D Phase 1（觀察基礎）

> **狀態**：Round 1 草稿（2026-05-13）
> **來源**：issue #362（七層 → 兩觸發點）+ doc/52 §1.3 Quest D 願景 + 2026-05-13 閉環報告 + Round 1 對話
> **下一步**：在對應實作 issue 的 comment 區逐題討論（§8 open questions），鎖定後出實作 issue
>
> 本文件是 Skill Evolution Arena Phase 1 的設計權威。實作分歧時以本文件為準；本文件未涵蓋的細節，按 §2「設計原則」推導。

---

## 1. 文件目的

Loom 已有完整的技能演化基礎建設（SkillGenome / SkillMutator / SkillPromoter / SkillGate / CounterFactualReflector / Grader / 七階段工作流），但**從未被完整觸發執行過一次**。

2026-05-13 自我健檢發現：

- 18 個技能中 17 個「從未被 Grader 評估」
- `loom.db` 存在但 schema 未初始化 → candidate 寫入失敗、promote 永遠 `Candidate not found`
- Event Ledger 已累積 6584 筆事件，但 `skill_id` 欄位從未被寫入（NULL）
- code_weaver 是唯一活著的技能 — 但靠的是「使用者陪伴 + 直接 edit SKILL.md」，**不是**任何自動 grading 迴路

問題根因不是「沒人踩油門」這麼簡單。Round 1 的關鍵推論：

> **技能不是工具。工具有結構化成功指標（exit code / return value），技能是工作流程 — 「過程訊號順不順」≠「成品好不好」。** loom reflect 那些數字感受不到價值，是因為它確實只衡量「運行順暢度」，不是成品。

→ 沒有可信的「成品 grading」substrate，所有自動 shadow/promote 機制都是 garbage in / garbage out。

### 1.1 重新定位：本 milestone 是觀察基礎，不是演化系統

Quest D 拆成三段，本文件只負責第一段：

| 階段 | 範圍 | 依賴 |
|------|------|------|
| **本文件 — Quest D Phase 1** | 觀察基礎：ledger 追蹤、weekly 現況報告、整併退役 | 無 |
| **Evaluator Skill**（未來 milestone） | 任務拆解 + 成功標的 + 成果驗證 — 真正的 grader substrate | Phase 1 |
| **Quest D Phase 2**（更未來） | Self-training：基於 Evaluator 訊號做 mutation/promote | Evaluator |

對應 doc/52 §1.3。Phase 1 不假裝在做演化、不寫沒辦法驗證的 metric。

### 1.2 為什麼即時反饋通道夠用（在 Phase 1）

code_weaver 97% 不是靠任何自動框架，是靠：

```
使用者反饋 → 絲絲直接 edit SKILL.md → 下次更好
```

這條已經在跑。Phase 1 不打算改造它、不打算把它「升級為框架」— 它就是 conversational 形式，最簡單也最有效。其他 17 個技能拿不到這條 = 它們還沒被使用者投入足夠注意力，那就讓 weekly worker 在報告裡 surface 它們，由使用者決定要不要花時間。

---

## 2. 設計原則

按優先級排序：

1. **可驗證的才自動化** — 「成品好不好」目前沒有可信判定，那就不自動演化。Phase 1 純觀察、純報告。
2. **完全刪除 > 暫存** — 退役元件代碼與 config 一併移除。未來 Evaluator 重起時重寫，別讓死代碼長靈藏。
3. **入口收斂** — Agent 暴露的技能相關 tool 只剩 `load_skill` / `unload_skill`。動詞型 mutate tool 全部移除。
4. **即時反饋走對話、不走框架** — 「絲絲根據反饋 edit SKILL.md」是 conversational flow，不該被包成 candidate/promote 框架。
5. **觀察走排程、走 ledger** — weekly worker 從 ledger 抽現況、產報告、附該關注清單。報告是給使用者讀的，不是給 agent 自動消費的。
6. **Append-only ledger 純度** — Phase 1 不新增 event_type，只把 `skill_id` 寫進既有 `tool_lifecycle` payload。
7. **預留比 migration 便宜，但別預留死規格** — 未來 Evaluator 需要的新 event_type 留給 Evaluator milestone 定義（屆時設計已成熟）。Phase 1 不預先發明 `skill_evaluation` 之類的 schema。

---

## 3. 退役清單（完全刪除）

每一行的「session.py register 行」欄記載**對應 `LoomSession.start()` 中要一併拿掉的註冊呼叫**。Round 1 review（#363）抓到的 dependency ordering 問題：若刪除 module 但留下 register 行，會 ImportError；刪除 register 但留下 module 則該 tool 對 agent 不可見但代碼仍在。**同一個 commit 同時處理才是 atomic**。

| 元件 | 檔案 | 處置 | session.py register 行 |
|------|------|------|------------------------|
| `SkillGate` (shadow_mode auto_c) | `loom/core/cognition/skill_gate.py` | 刪除整檔 | `_skill_gate` 欄位 + `make_load_skill_tool(skill_gate=...)` 引用 |
| `SkillPromoter` | `loom/core/cognition/skill_promoter.py` | 刪除整檔 | `_skill_promoter` 欄位 + `task_reflector` 引用 + `subscribe(_fan_promotion)` |
| `SkillMutator` | `loom/core/cognition/skill_mutator.py` | 刪除整檔 | `_skill_mutator` 欄位 + `task_reflector(mutator=...)` |
| `SkillGenome` dataclass + ProceduralMemory candidate 介面 | `loom/core/memory/procedural.py` | 刪除 candidate / genome 相關函式，保留 procedural memory 其他用途 | — |
| `memory.db` 中的 skill_genomes / skill_candidates / skill_version_history 表 | `loom/core/memory/store.py` (DDL @ lines 46+) | 刪除 DDL；下次 init 時表不再 create。**注意**：實際 host 是 `memory.db` 不是 `loom.db`（後者實際為空，是個 ghost file，可額外刪除） | — |
| `generate_skill_candidate_from_batch` tool | `loom/platform/cli/tools.py` | 刪除 factory + executor | `make_generate_skill_candidate_from_batch_tool(...)` register (#363 後 session.py:1453) |
| `promote_skill_candidate` tool | 同上 | 刪除 | **已在 #363 移除** (was session.py:1447) |
| `skill_rollback` tool | 同上 | 刪除 | **已在 #363 移除** (was session.py:1448) |
| `set_skill_maturity` tool | 同上 | 刪除 | `make_set_skill_maturity_tool(...)` register (#363 後 session.py:1456) |
| `loom.toml [mutation]` section | `loom.toml` + parser | 整節移除 | — |
| `loom review` CLI 子命令（舊版） | `loom/platform/cli/main.py:3041` `cli.command("review")` | 刪除 | — |
| `meta-skill-engineer` 七階段工作流文件 | `.claude/skills/meta-skill-engineer/` 或 `skills/meta-skill-engineer/` | 移除 skill；相關 Grader / Comparator / Analyzer prompt 一併刪 | — |
| `~/.loom/loom.db` ghost file | runtime 副作用 | 不用 migration；下次 LoomSession start 不會再寫 | — |
| `outputs/doc/skill_system_evolution_plan_2026-05-02.md` | doc | 標記為「已被 doc/54 取代」並移到 `outputs/doc/archive/` 或直接刪 | — |

**保留**：

| 元件 | 理由 |
|------|------|
| `CounterFactualReflector` | 寫 anti-pattern 進 SemanticMemory，不依賴 grading。是已在跑的有用副作用 |
| `load_skill` / `unload_skill` | 主路徑 |
| `procedural memory` 其他功能（非 candidate 部分） | 通用 procedural memory，不只服務 skill |
| `SkillCheckManager` / `skill_checks.py` | 與演化無關，是 precondition gate |

---

## 4. 三條訊號通道

### 4.1 通道 A — 對話內即時反饋

這條通道分兩層、已在跑，本文件不重新發明任何一層：

**層 1：系統自動回灌**

`ToolCallDimension`（`loom/core/infra/telemetry.py:128`）在對話中渲染 tool 統計與 anomaly：

- summary 行：`tool:97% | lat:230ms | n=42` 出現在對話 telemetry 渲染
- failure rate ≥ 30% 且樣本 ≥ 5 → 觸發 anomaly 提示
- last failure message 在 detail 視圖暴露

→ 使用者「注意到技能有問題」的主要觸發源就是這層，不是憑空察覺。技能載入後緊隨的 tool calls 直接被追蹤，是隱性的即時反饋通道。

**層 2：使用者反饋 → 絲絲 edit SKILL.md**

```
使用者看到 ToolCallDimension 訊號 / 自行觀察到問題
        ↓
口頭說「這裡可以更好」
        ↓
絲絲根據反饋直接 Edit SKILL.md（git commit 是版本控制）
        ↓（同步副作用）
memorize 寫 SemanticMemory → ledger memory_op 事件
```

- **不需要框架** — 兩層都本來在跑
- **不需要 candidate / shadow / promote** — 層 2 直接覆寫 SKILL.md
- **規模化限制公開承認** — 層 2 只發生在使用者陪伴的技能上。但**層 1 對所有技能無差別作用**，這提升了「使用者察覺低活躍技能問題」的機率

> **注意**：ToolCallDimension 是 per-tool 維度，不是 per-skill。它的 in-memory state 也是 session-local。但 ledger `tool_lifecycle` END payload 帶 `error` 欄位，可從 ledger 派生 per-skill 的失敗率 — weekly worker（§4.2）會用 ledger 重建這個訊號，而不是查 telemetry。

### 4.2 通道 B — Weekly 現況報告（含該關注清單）

```toml
[[autonomy.schedules]]
name = "skill_weekly_review"
cron = "0 10 * * 6"   # 每週六上午十點
intent = """
從 ledger 抽過去 7 天的技能使用紀錄、反饋紀錄、turn outcome 統計，
產出現況報告寫到 outputs/self_check/{date}-skill-weekly.md。
不評分、不下指導。在報告末段附「該關注清單」，由使用者決定是否花時間。
"""
```

**Worker 內部動作**（純查詢 + 模板渲染、沒有 agent loop）：

1. ledger query：過去 7 天 `tool_lifecycle` where `tool_name='load_skill'` group by `skill_id`
2. ledger query：載入該技能的 turn_id → 看 turn_end outcome 分布
3. ledger query：載入該技能後 N 分鐘內，使用者觸發的 `memory_op write` 事件（即時反饋訊號）
4. 渲染報告：每個技能一段、附近期載入次數、turn outcome 分布、反饋密度
5. 渲染該關注清單（§4.3）

**重要**：worker 不是 LLM agent。它是純 SQL + 模板。Grader agent 整個退役。

### 4.3 通道 C — 對話內 ledger 調閱（最核心的優化迴路）

這是使用者實際走的優化流程，本文件 Round 1 才釘清楚：

```
觸發時機（任一）：
  - 技能剛使用完畢（使用者親眼看過該次運用）
  - Weekly 報告產出後（使用者讀完報告）
        ↓
使用者問絲絲：「回顧一下你剛剛使用了 xxx 技能有遇到什麼狀況」
        ↓
絲絲呼叫 skill_review(skill_id, since=...) 工具
        ↓
回傳 ledger 中該技能的使用回顧（events 摘要）
        ↓
絲絲在對話中口述觀察、使用者補充情境
        ↓
共識後絲絲直接 Edit SKILL.md / contexts/*.md / checks.py
        ↓
git commit（=版本紀錄，取代 candidate/promote 機制）
```

**這是 Loom 演化的真正主路徑**。即時反饋通道（4.1）是訊號觸發源、Weekly 報告通道（4.2）是低頻全景巡視、本通道 C 才是**深入優化的對話迴路**。

#### 4.3.1 `skill_review` tool 設計

唯一新增的 agent 暴露 tool。**只讀、不 mutate**，trust_level=`SAFE`：

```python
skill_review(
    skill_id: str,
    days: float = 7,                           # window size; numeric for ergonomics
    max_episodes: int = 30,                    # cap 回傳體積（episodes）
    max_events_per_episode: int = 30,          # cap 回傳體積（每 episode 內事件）
) -> SkillUsageDigest
```

> **Round 1 review note (#363)**：原 Round 0 規格寫 `since="7d" / "30d" / ISO timestamp`、`include=[...]`、`max_events=100`。實作（PR #363 `make_skill_review_tool`）採用更扁平的 `days` + 雙層 max — 簡單、ergonomic、不需要 string parsing。`include` 過濾留給 Watch issue #364 收集真實使用反饋後再決定要不要做。本 doc 已對齊實作。

回傳結構（skill-scoped 而非 session-scoped）：

```python
@dataclass
class SkillUsageDigest:
    skill_id: str
    window: tuple[float, float]                # since_ts, now_ts
    load_count: int
    unload_count: int

    # 跨 session 聚合
    sessions: list[str]                        # 載入過的 session_id
    turns_with_load: list[TurnSummary]
        # turn_id, started_at, outcome (clean/retry/abandoned/error),
        # 該 turn 內該技能載入後的 tool calls 摘要

    tool_calls: list[ToolCallSummary]
        # tool_name, args_digest, result (success/error), error_msg, duration_ms
        # 只包含「載入該技能後 / unload 該技能前」窗口內的 tool call

    feedback_events: list[FeedbackEvent]
        # 載入該技能後 N 分鐘內、unload 之前，使用者觸發的 memory_op write
        # (這是「使用者反饋密度」的事件源)

    failures: list[FailureSummary]
        # tool failure + execution_error + judge_verdict=FAIL（如果未來有）
```

絲絲消費這份 digest 後**自然產生回顧**，不再需要框架幫忙摘要。

#### 4.3.2 與 Weekly Worker 共用 Query 層

通道 B 跟通道 C 用同一份 ledger 資料、同一個聚合邏輯，差別只在「呈現」：

| 通道 | 呈現 | 觸發 |
|------|------|------|
| B（weekly worker） | Markdown 報告寫到 `outputs/self_check/` | cron 排程 |
| C（skill_review tool） | 結構化 dict 回傳給絲絲 | agent 對話中呼叫 |

實作層：**先寫 pure function `query_skill_ledger(skill_id, since, ...) -> SkillUsageDigest`**，B 跟 C 都呼叫它。B 把結果 render 成 markdown、C 把結果序列化回 agent。

→ 兩條通道的查詢邏輯只寫一次。修 bug、加維度、改聚合策略也只改一處。

放在 `loom/core/skill_review/query.py`（新增模組）。

### 4.4 「該關注清單」判定條件

不下成績、只指出「值得使用者花時間看一眼」的觀察。每條都是**結構性觀察**而非品質判定：

| 觀察類型 | 判定 | 為什麼值得關注 |
|----------|------|---------------|
| **悶頭跑** | 使用頻次高、反饋密度低 | 可能在錯誤模式下被反覆呼叫，使用者沒注意到 |
| **反饋未消化** | 反饋密度高、SKILL.md 久未 commit | 反饋被收進 memory 但沒回灌到 skill 主體 |
| **異常結尾** | 載入該技能的 turn 中 `outcome=abandoned/error` 占比 > X | 可能正在卡關 |
| **久未使用** | 過去 30 天從未被載入 | 是否還需要存在？或定義太窄被冷落 |
| **存在但未啟動** | SKILL.md 存在但從未被載入過 | 17/18 純文案的根本問題 |

清單**只列觀察**，不附「建議動作」— 使用者自己會判斷。

---

## 5. P0 修復清單

實作順序按依賴：

| # | 修復 | 檔案 | 工作量 | 阻擋什麼 |
|---|------|------|--------|----------|
| P0-1 | `load_skill` executor 寫入 `skill_id` 進 ledger `tool_lifecycle` payload | `loom/platform/cli/tools.py` `_load_skill` | S | 所有下游查詢 |
| P0-2 | `unload_skill` executor 同上 | 同上 | S | 同上 |
| P0-3 | 驗證 ledger pull 對 `skill_id` virtual column 走 index | `loom/core/ledger/pull.py` | S | 查詢效率 |
| P0-4 | 新增 `loom/core/skill_review/query.py` — `query_skill_ledger()` pure function | 新模組 | M | 通道 B + 通道 C |
| P0-5 | 新增 `skill_review` tool（包裝 P0-4） | `loom/platform/cli/tools.py` | S | 通道 C |
| P0-6 | 新增 weekly worker（呼叫 P0-4 + render markdown） | 新檔 | M | 通道 B |
| P0-7 | `loom.toml` 新增 `skill_weekly_review` schedule 條目 | `loom.toml` + autonomy schedules parser | S | 通道 B 觸發 |
| P0-8 | `loom.db` 真相確認 — 該檔案實際**完全空白**（no tables），技能表的真實 host 是 `memory.db`。退役工作 = 從 `memory.db` 刪除 skill_genomes / skill_candidates / skill_version_history DDL（Phase 1.C 範圍）；`~/.loom/loom.db` ghost file 不需要 migration | `loom/core/memory/store.py` | — | Phase 1.C |

**今日優先做 P0-1 與 P0-2**：沒有 `skill_id` payload，下游全部沒資料可吃。其他可以分次。

---

## 6. 與 Evaluator Skill 未來 milestone 的邊界

本文件刻意**不**設計：

- 成品 grading 機制
- 任務拆解 / 成功標的定義
- replay corpus（即使是小範圍）
- shadow A/B
- 自動 promote 機制

理由：這些都依賴「Evaluator」這個尚未存在的能力 — 「能把任意任務拆解成可驗證 sub-goal、並判定最終成品是否達成」。在 Evaluator 未存在前先設計演化機制，等於替沒有測量儀器的實驗室排實驗順序。

**Evaluator Skill 設計時可能需要新增 ledger event_type**（暫定候選名：`task_outcome` 或 `skill_evaluation`），屆時走 doc/53 §9.4 v1→v2 schema migration。Phase 1 不預先佔位。

---

## 7. Phase 規劃（鎖定後拆 issue）

| Phase | 內容 | 預期 |
|-------|------|------|
| **1.A** | P0 修復（§5）— `skill_id` payload + weekly schedule entry | 0.5-1 週 |
| **1.B** | weekly worker 實作（純 SQL + 模板，無 agent loop）+ 該關注清單渲染（§4.3） | 1 週 |
| **1.C** | 退役清單執行（§3）— 大刪除 PR，一次性處理 | 0.5-1 週 |
| **1.D** | 第一份 weekly 報告產出 + 走查 17 個低活躍技能 → 由使用者決定哪些該關注 / 該刪 | 0.5 週 |

預期總時程 **2.5-4 週**。Phase 1.C 大刪除是高 blast radius 動作，要 gitnexus impact 先跑。

---

## 8. Open Questions

| # | 問題 | Round 1 暫定 |
|---|------|---------------|
| Q1 | weekly worker 是 sync script（cron 觸發）還是走 autonomy daemon？ | autonomy daemon（既有機制），但 worker 本身是純 SQL，不是 agent |
| Q2 | 該關注清單的閾值（悶頭跑、異常結尾比例）怎麼定？ | 第一次發報告觀察一兩週後再校準 |
| Q3 | `loom.db` 整個檔案能不能廢除？除了 skill 表還有沒有別人在用？ | 待 §5 P0 階段確認 |
| Q4 | `CounterFactualReflector` 寫 anti-pattern 進 SemanticMemory 後，weekly worker 要不要從 SemanticMemory 抽相關 entries 進報告？ | 暫不，避免雙資料源混淆。Phase 1 報告純從 ledger 派生 |
| Q5 | 17 個純文案技能，要不要在 Phase 1.D 直接刪掉用不到的？ | 由 1.D 走查時使用者個別決定，不在本文件先判 |
| Q6 | 退役 `[mutation]` config 後，現有 `loom.toml` 用戶設定要不要做 migration 警告？ | 加一行 warning 即可，不做兼容 |
| Q7 | `ToolCallDimension` 是否該長出 per-skill slice？例如載入 skill X 期間的 tool failure rate 獨立追蹤、在對話 telemetry 渲染中分別顯示？ | Phase 1 不做（純查 ledger 重建）。但這是 Evaluator milestone 之前值得評估的低成本增強 — 即時反饋層 1 變得「skill-aware」對使用者更直觀 |

---

## 9. 與其他文件的關係

- `doc/50-未來改善路線圖.md` — 顧問版 Quest D 出處
- `doc/51-Agent-能力評級系統.md` — capability sheet 將來吃 Evaluator 訊號（不是本文件）
- `doc/52-主線與支線.md §1.3` — Loom 自家 Quest D 願景，本文件只接住 Phase 1
- `doc/53-AgentLedger-設計.md` — ledger schema；本文件不擴充 event_type
- `issue #362` — 原始七層 → 兩觸發點提案，本文件進一步收斂為「觀察 + 退役」
- `outputs/self_check/2026-05-13-skill-closed-loop-report.md` — 觸發本文件的閉環報告
- `outputs/doc/skill_system_evolution_plan_2026-05-02.md` — **舊版規劃，本文件 §3 列入退役**

---

## 10. 設計決策追溯

Round 0 → Round 1 的主要轉變記錄，避免日後重新爭論：

- **Round 0 §5 健康指標表** → **Round 1 刪除**。理由：過程訊號不等於成品品質
- **Round 0 §6 candidate 改寫進 ledger** → **Round 1 candidate 機制整個退役**。理由：沒可信 grader → candidate 生命週期沒意義
- **Round 0 §7 Replay corpus** → **Round 1 移除**。理由：replay 需要明確可驗證任務目標，這是 Evaluator milestone 範疇
- **Round 0 §4.2 weekly Grader batch** → **Round 1 改為純 SQL + 模板的 weekly worker**。理由：沒可信 grading 標準，跑 Grader agent 是 garbage in/out
- **Round 0 §9 Q1 新 event_type `skill_candidate`** → **Round 1 不新增**。理由：candidate 機制退役，event_type 不需要
- **Round 0「整併保留」基調** → **Round 1「完全刪除」**。理由：未來 Evaluator 重起會重寫，別讓死代碼長靈藏（使用者明確選擇）
- **Round 1 §4.1 補強**：明確區分即時反饋的兩層 — `ToolCallDimension` 已是隱性層 1，使用者反饋是層 2。先前只寫層 2 太單薄，等於沒看到系統已暴露的訊號通道

---

*Round 1 草稿：絲絲・Loom 與 Dakai 對話 | 2026-05-13*
*下一步：在對應實作 issue 的 comment 區逐題討論 §8 open questions*
