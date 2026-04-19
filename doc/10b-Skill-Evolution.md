# Skill Evolution — 技能進化生命週期

> Issue #120 — 讓 SKILL.md 在使用中被自己改寫。

## 為什麼需要這一層

[10-Skill-Genome.md](10-Skill-Genome.md) 的 EMA confidence 能告訴你「這個 skill 狀況不好」，但改不了 skill 本身。長期下來 skill body 會落後於實際需要。

Issue #120 在 EMA 自評之上補了一條完整的進化鏈：**每輪結束時寫一份 TaskDiagnostic → 低分時 LLM 改寫 SKILL.md 成候選 → 候選先在 shadow 模式小規模試用 → 驗證好再 promote → 不對勁就 rollback**。整條鏈預設只開到 diagnostic；開一個開關才會開始改寫 skill，再開一個才會真的替換。

## 三段式 PR 骨架

| PR | 模組 | 職責 |
|----|------|------|
| PR 1 | `TaskReflector` | 每輪 TurnDone 產出結構化 `TaskDiagnostic`（instructions_followed / violated / quality_score / mutation_suggestions…） |
| PR 2 | `SkillMutator` | 看到低分 diagnostic 就呼叫 LLM 改寫 parent SKILL.md → 存成 `SkillCandidate`（狀態 `generated`） |
| PR 3 | `SkillPromoter` + `SkillGate` | 狀態機 `generated → shadow → promoted` / `deprecated` / `rolled_back`；載入時決定要服務 parent 還是 shadow body |

## 資料流一覽

```text
turn done
   ↓
TaskReflector.reflect()
   ├─▶ TaskDiagnostic（skill:<name>:diagnostic:<ts>）
   │      quality_score 1–5 / mutation_suggestions[]
   │      instructions_violated[] / failure_patterns[]
   ↓  （fire-and-forget）
SkillMutator.should_propose?
   ├─ quality_score ≤ quality_ceiling
   ├─ len(mutation_suggestions) ≥ min_suggestions
   ├─ mutation.enabled = true
   ↓  LLM 改寫 SKILL.md（保留 frontmatter）
SkillCandidate(status="generated")
   ↓  （fire-and-forget）
SkillPromoter.maybe_auto_shadow()
   ├─ shadow_mode = "auto_c"
   ├─ parent.confidence ≤ auto_shadow_confidence_ceiling
   ↓
SkillCandidate(status="shadow")

後續 load_skill("<name>")：
SkillGate.resolve()
   ├─ off → parent
   ├─ manual_b → parent（除非有 force_shadow 覆寫）
   └─ auto_c → 用 SHA1(session_id|skill_name) 切分，
               決定這個 session 看 parent 還是 shadow body

使用者或 agent 判斷 shadow 表現夠好：
   loom skill promote <candidate_id>  或 tool promote_skill_candidate
   ↓
skill_version_history 封存 parent 舊 body
parent.body = candidate.candidate_body
parent.version += 1
parent.confidence = 1.0（重新累積信心）
sibling shadows → deprecated

後悔的話：
   loom skill rollback <skill_name>  或 tool rollback_skill
   ↓
skill_version_history 也封存現況
parent.body ← 先前版本
parent.version += 1
promoted candidate → rolled_back
```

## 三個 SQL 表（新增/擴充）

```sql
-- PR 2
CREATE TABLE skill_candidates (
    id TEXT PRIMARY KEY,
    parent_skill_name TEXT NOT NULL,
    parent_version INTEGER NOT NULL,
    candidate_body TEXT NOT NULL,
    mutation_strategy TEXT NOT NULL,     -- 目前只有 "apply_suggestions"
    status TEXT NOT NULL DEFAULT 'generated',
    pareto_scores TEXT,                  -- JSON dict[task_type, score]
    diagnostic_keys TEXT,                -- JSON list[semantic_key]
    origin_session_id TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

-- PR 3
CREATE TABLE skill_version_history (
    id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'promote',   -- promote / rollback / manual
    source_candidate_id TEXT,
    archived_at TEXT NOT NULL,
    UNIQUE(skill_name, version, archived_at)
);
```

五種候選狀態：

| 狀態 | 意義 | 可以轉到 |
|------|------|----------|
| `generated` | Mutator 剛產出，還沒服務任何 session | `shadow` / `deprecated` / `promoted` |
| `shadow` | 被 Gate 分流給一部分 session 實測中 | `promoted` / `deprecated` |
| `promoted` | 已替換 parent body | `rolled_back`（透過 rollback） |
| `deprecated` | 人為或系統淘汰 | 終端狀態 |
| `rolled_back` | 被 promote 後又 rollback | 終端狀態 |

## SkillGate 的決策

| `shadow_mode` | 行為 |
|---------------|------|
| `off` | 永遠服務 parent body，lifecycle 指令還是可以用 |
| `auto_c`（預設）| 有 shadow 候選時，用 SHA1(`session_id\|skill_name`) 的 first-4-byte int 對 `shadow_fraction` 做確定性切分；同一個 session 看到的永遠同一側 |
| `manual_b` | 不自動切分，要用 `SkillGate.force_shadow(skill_name, candidate_id)` 明確指定才會服務 shadow |

確定性切分的用意是讓 TaskReflector 能乾淨做 A/B 比較——同一 session 整段歷史只看過同一個 body，對應出來的 quality_score 才不會混進另一側。

## loom.toml 配置

```toml
[mutation]
# PR 2 — 候選產出
enabled          = false     # 預設關閉；打開才會開始改寫
model            = "auto"
quality_ceiling  = 3.5       # diagnostic.quality_score ≤ 這個值才改寫
min_suggestions  = 1
max_body_chars   = 6000

# PR 3 — 生命週期路由
shadow_mode      = "auto_c"  # off / auto_c / manual_b
shadow_fraction  = 0.5       # auto_c 下有多少比例 session 會看到 shadow
auto_shadow_confidence_ceiling = 0.7  # parent confidence 超過這個就不 auto_shadow
```

兩個開關都預設安全值：`mutation.enabled = false` 就不會改寫任何東西；`shadow_mode = auto_c` 即便開了 mutation 也只對 confidence 不到 0.7 的 skill 自動試驗。

## 使用者操作面

### CLI

| 指令 | 作用 |
|------|------|
| `loom skill candidates [--skill NAME] [--status STATUS] [--show-body]` | 列出候選池 |
| `loom skill promote <candidate_id_prefix> [--reason ...]` | 把候選升為 parent；支援 8 字元 prefix |
| `loom skill rollback <skill_name> [--to-version N] [--reason ...]` | 回滾到先前版本（預設最近一筆 archive） |
| `loom skill history <skill_name>` | 看某個 skill 的版本封存紀錄 |

promote / rollback 都是 GUARDED 級別——走到 `BlastRadiusMiddleware`、會要求授權。

### Agent tools

同樣兩個操作也以 tool 形式暴露給 agent：

- `promote_skill_candidate(candidate_id, reason?)` — GUARDED
- `rollback_skill(skill_name, to_version?, reason?)` — GUARDED

agent 可以在發現某個 shadow 明顯表現更好時自行 promote；但因為 trust level 是 GUARDED，仍會受 session 授權配置約束（沒授權就會進 confirm flow）。

### Session 訂閱事件

每次狀態機轉換會發 `PromotionEvent`（`promote` / `rollback` / `auto_shadow` / `deprecate`）。三個平台各自掛了訂閱者：

- **CLI** — 轉換時 dim 色印一行，不打斷對話流
- **TUI** — `app.notify(...)`，rollback / deprecate 會升 severity=warning
- **Discord** — 在 session thread 發獨立訊息，前面帶 icon（🔁 / ↩️ / 🫥 / 🗑️）

要自訂訂閱者：

```python
session.subscribe_promotion(my_callback)  # 可以是 sync / async
```

## 怎麼讓一個 skill 真的進化

### 最小驗證路徑（都不影響生產 skill）

1. 打開 `loom.toml`：`[reflection] auto_reflect = true`（通常已預設開啟）
2. 驗證 PR 1：跑 `loom chat` 幾輪，`loom memory list` 應該有 `skill:<name>:diagnostic:<ts>` 類 semantic key
3. 打開 `[mutation] enabled = true`，模擬幾個低分 turn（故意違反 skill 的指示），`loom skill candidates` 應該會有新列
4. 確認 `shadow_mode = auto_c` 且該 skill `confidence ≤ 0.7`：幾輪之後 candidate status 會變 `shadow`
5. `loom skill promote <prefix>` → `loom skill history <name>` 看到 archive；`load_skill` 應該取得新 body
6. `loom skill rollback <name>` 再跑一次 history，body 回到舊版、版本繼續 +1

### Mode 選擇建議

- **想看 agent 自主進化，但又怕被改壞** → `shadow_mode = auto_c`、`shadow_fraction` 先放 0.2~0.3；觀察 shadow 和 parent 兩側的 diagnostic
- **所有改寫都要自己批** → `shadow_mode = manual_b`；候選只會落在 `generated`，等 `loom skill promote` 才動
- **只想收集候選不上線** → 開 `mutation.enabled = true`、`shadow_mode = off`；候選會累積但永遠不會服務

## 邊界與注意事項

- **Mutation 失敗 non-fatal**：LLM 超時 / 輸出不像 SKILL.md / embedding 失敗——全部 swallow + debug log。`stream_turn` 不會因此中斷。
- **Candidate plausibility 檢查**：`_looks_like_skill_md()` 會要求候選與 parent 至少共享一行，避免完全不相干的輸出污染候選池。
- **確定性切分不會重新洗牌**：`shadow_fraction` 從 0.5 改成 0.3 不會把原本分到 shadow 的 session 改回 parent——切分是 stateless hash，調整當下剛好 hash < 0.3 的 session 才會被分到 shadow。
- **Sibling shadow 去重**：promote 一個候選後，同一個 parent 下其他還是 shadow 的候選會被標成 deprecated（shadowing 一個已經不存在的 body 沒意義）。
- **Rollback 連鎖**：rollback 本身也會封存當下的 body 到 history，所以可以 rollback 後再 rollback 回來。

## 與既有機制的關係

| | EMA self-assessment（10-Skill-Genome）| Skill Evolution（10b 本篇）|
|---|---|---|
| **改什麼** | `SkillGenome.confidence` / `success_rate` | `SkillGenome.body` |
| **單位** | 數字（1–5 → 0–1）| 整份 SKILL.md |
| **頻率** | 每次 `load_skill` → TurnDone | 低分 turn 才觸發 |
| **可逆** | EMA 本身是累加——無法「回滾某一次評分」| 有 `skill_version_history`，可以精確回到任一版本 |
| **預設開關** | 隨 reflection 打開 | `enabled=false`（保守） |

兩層互補：EMA 告訴你 skill 品質趨勢，Evolution 在趨勢不好時提案修改 body；提案透過 shadow A/B 實測，真的有改善才會落地。
