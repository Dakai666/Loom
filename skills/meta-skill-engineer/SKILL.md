---
name: meta-skill-engineer
description: "元技能工程師：系統化建立、評估、迭代改進 Loom 技能的技能。當使用者要求「建立一個新技能」、「改善現有技能」、「評估技能表現」、「跑技能對比測試」、「系統化迭代技能」時使用。本技能為 Skill Genome 提供評估閉環——Grader 產生 BatchDiagnostic，透過 `generate_skill_candidate_from_batch` 工具轉化為候選版本，進入 shadow → promote 生命週期，形成測試→記憶→演化的完整循環。"
precondition_checks:
  - ref: checks.require_skills_dir_target
    applies_to: [write_file]
    description: "只能寫入 skills/ 目錄，不可修改框架或使用者程式碼"
---

# Meta Skill Engineer

系統化建立、評估、迭代改進 Loom 技能的技能。是 Skill Genome 的「評估層」，讓技能從「能用」進化到「用得好」。

---

## 核心原則

1. **先確認意圖，再動手** — 不清楚要做什麼之前不寫 SKILL.md
2. **評估先於改進** — 沒有數據的優化是猜測，不是工程
3. **閉環是必須** — 每次 Grader 評估後，結果必須寫入 SkillGenome（memorize + relate）
4. **盲測杜絕偏見** — Comparator 永遠不知道誰是 A、誰是 B

---

## 工作流程（七階段）

```
階段 1：意圖確認
  ↓
階段 2：草稿生成（寫 SKILL.md）
  ↓
階段 3：測試集建立（至少 5 個測試案例）
  ↓
階段 4：Grader 評估（量化 pass/fail）
  ↓
階段 5：Comparator 對比（有舊版才做）
  ↓
階段 6：Analyzer 因果分析（Comparator 判定非 TIE 時啟動）
  ↓
階段 7：SkillGenome 寫入 → 重寫 → 迭代
```

每個階段的詳細說明如下。

---

## 階段 1：意圖確認

**目標：把模糊的需求具象化**

與使用者對話，確認以下資訊：

1. **技能目標**：這個技能要解決什麼問題？（不是「要做什麼」，是「解決什麼」）
2. **觸發時機**：什麼訊號表明這個技能應該被啟動？（keyword / 場景描述）
3. **輸出形式**：技能成功執行後，交付什麼？（格式、深度、呈現方式）
4. **能力邊界**：什麼情況下技能不應該被使用？
5. **既有假設**：這個技能依賴 Loom 哪些現有工具？

**產出：** 一段「技能意圖說明書」（不超過 200 字），包含：風險面初步識別（這個技能可能造成什麼損害？）

---

## 階段 2：草稿生成

**目標：產出第一版 SKILL.md**

根據意圖說明書，生成完整的 SKILL.md。

### 標準 SKILL.md 結構

```markdown
---
name: [skill-name]
description: "[觸發描述]。當使用者要求[場景]時使用。"
tags: [tag1, tag2, ...]
precondition_checks:                          # ← 安全護欄（見下方說明）
  - ref: checks.[function_name]
    applies_to: [run_bash]                    # 目標工具名
    description: "這個檢查做什麼"
  - ref: checks.[another_function]
    applies_to: [write_file]
    description: "這個檢查做什麼"
---

# [技能名稱]

[技能一段式描述：這個技能在做什麼]

---

## 核心原則

1. [第一原則]
2. [第二原則]
...

---

## 工作流程

### 步驟一：[名稱]
[具體做法]

### 步驟二：[名稱]
[具體做法]

---

## 輸出格式

[這個技能每次輸出時遵循的固定格式]

---

## 工具使用策略

| 工具 | 使用時機 |
|------|---------|
| [工具名] | [什麼情況用] |
...

---

## 觸發關鍵詞

[列出所有會觸發這個技能的關鍵詞或場景描述]

---

## 紀律提醒

- [這個技能不應該做的事項]
- [這個技能的常見錯誤]
```

**產出：**
- `skills/[skill-name]/SKILL.md`
- `skills/[skill-name]/checks.py`（若有 precondition_checks）

---

## 階段 2.5：Precondition Checks 設計

**目標：為技能建立框架層級的安全護欄**

每個技能在執行工具時可能帶有風險。Precondition checks 是在工具真正執行前（PREPARED gate）由框架自動評估的 async 函式，全部通過才放行，任一失敗則 ABORT——不依賴 SKILL.md 文字紀律，而是用程式碼強制執行。

### 設計流程

1. **識別風險面** — 這個技能會用哪些工具？每個工具最壞情況能造成什麼？

   | 風險模式 | 常見場景 | 典型護欄 |
   |---------|---------|---------|
   | 破壞性指令 | `run_bash` 執行 `rm -rf`、`DROP TABLE` | `reject_destructive_commands` |
   | 環境錯誤 | 在生產環境跑測試工具 | `reject_production_env` |
   | 越界寫入 | 技能修改了不該碰的檔案 | `require_skills_dir_target` |
   | 資料遺失 | 清理操作前無備份 | `require_memory_backup` |
   | 唯讀違反 | 分析型技能不應寫檔 | `reject_write_operations` |

2. **定義檢查函式** — 在 `skills/[name]/checks.py` 中實作：

   ```python
   # skills/my-skill/checks.py
   async def my_precondition(call) -> bool:
       """Return True to allow, False to block."""
       cmd = call.args.get("command", "")
       # 你的檢查邏輯
       return True
   ```

   **函式簽名規則：**
   - 參數：`call`（ToolCall mock，通過 `call.args` 取得工具參數）
   - 回傳：`bool`（True = 放行，False = 阻擋）
   - 必須是 `async def`
   - 不可有副作用（不寫檔、不改狀態）

3. **宣告到 frontmatter** — 在 SKILL.md 的 YAML 區塊中引用：

   ```yaml
   precondition_checks:
     - ref: checks.my_precondition
       applies_to: [run_bash]
       description: "說明這個檢查在做什麼"
   ```

   - `ref`：`checks.` 前綴 + 函式名（對應 `checks.py` 中的函式）
   - `applies_to`：要掛載到哪些工具（`run_bash`、`write_file` 等）
   - `description`：給使用者看的說明（首次載入時會顯示）

4. **寫測試** — 在 `tests/test_skill_preconditions.py` 中驗證：

   ```python
   async def test_my_check_blocks_dangerous():
       from skills.my_skill.checks import my_precondition
       call = _make_call(command="dangerous command")
       assert await my_precondition(call) is False

   async def test_my_check_allows_safe():
       from skills.my_skill.checks import my_precondition
       call = _make_call(command="safe command")
       assert await my_precondition(call) is True
   ```

### 判斷是否需要 Precondition Checks

- **需要**：技能使用 `run_bash`（幾乎所有技能都應該有）、技能修改檔案但有邊界限制、技能操作敏感資源
- **不需要**：純對話型技能（不使用任何工具）、只用 `read_file` + `recall` 的純查詢技能

### 現有技能的 Checks 參考

| 技能 | Checks | 對應工具 | 設計理由 |
|------|--------|---------|---------|
| `loom_engineer` | `require_git_repo`, `reject_force_push` | `run_bash`, `write_file` | 工程技能必須在 git repo 中操作，禁止 force push |
| `systematic_code_analyst` | `reject_write_operations` | `write_file` | 分析技能是唯讀的，絕不寫檔 |
| `meta-skill-engineer` | `require_skills_dir_target` | `write_file` | 只能修改 skills/ 目錄，不碰框架程式碼 |
| `security_assessment` | `reject_destructive_commands`, `reject_production_env` | `run_bash` | 資安掃描不可破壞、不可在生產環境 |
| `memory_hygiene` | `require_memory_backup`, `reject_direct_db_mutation` | `run_bash` | 清理前必須有備份、禁止直接 SQL 修改 |

### 執行時機（框架行為）

```
load_skill("my-skill")
  → 讀取 SkillGenome.precondition_check_refs
  → 首次載入：顯示確認面板，列出所有 checks → 使用者批准
  → 批准後：resolve checks.py → mount 到目標 ToolDefinition
  → 卸載時機：load 另一個 skill 時自動卸載（除非 keep_existing=true）
```

**產出：** `skills/[skill-name]/checks.py` + SKILL.md frontmatter 更新

---

## 階段 3：測試集建立

**目標：建立可重複執行的測試案例**

### 測試案例結構

每個測試案例是一個 markdown 檔案：

```markdown
## 測試案例：{編號}

### Prompt
```
{給 Loom 的實際 prompt}
```

### 預期輸出（Expectations）
- [Expectation 1]
- [Expectation 2]
- [Expectation 3]

### 執行環境
- 工具集合：[允許使用的工具]
- 約束：[時間限制、禁用操作等]
```

### 測試集規模要求

| 迭代階段 | 最低測試數 |
|---------|----------|
| 第一輪 | 5 個 |
| 迭代後 | 10 個 |
| 最終驗收 | 20 個 |

**產出：** `skills/[skill-name]/tests/test-{N}.md`

---

## 階段 4：Grader 評估

**目標：客觀測量技能表現，產出 `BatchDiagnostic`**

使用 Grader Agent 對每個測試案例進行評估，每個測試案例產出一個 `TaskDiagnostic`，全部完成後匯聚成一個 `BatchDiagnostic`。

### 目錄約定（Grader 的檔案入口）

在 Grader 開跑前，階段 3 的測試執行必須把產出寫到**可預測的路徑**，否則 Grader 無從評估：

```
skills/<skill-name>/
├── tests/
│   ├── test-1.md                         # 測試定義（Prompt + Expectations）
│   ├── test-2.md
│   └── ...
└── runs/
    └── r<N>/                             # 第 N 輪評估
        ├── test-1/
        │   ├── transcript.jsonl          # 完整對話記錄
        │   └── output/                   # 技能產出的檔案（若有）
        │       └── ...
        ├── test-2/
        │   └── ...
        └── grader_report.md              # Grader 填這份
```

**配對規則**：`test-{N}.md` 定義 ↔ `runs/r<N>/test-{N}/` 產出。檔名編號一一對應是硬約定，Grader 就能靠 `list_dir("skills/<skill>/tests/")` 枚舉定義、再拼湊 `runs/r<N>/test-{M}/` 找到對應輸出。

### Grader 評估流程

1. **列出測試集**：`list_dir("skills/<skill-name>/tests/")` → 取得所有 `test-{N}.md`
2. **逐一讀入**：`read_file("skills/<skill-name>/tests/test-{N}.md")` → 解析 Prompt 與 Expectations
3. **讀取該 test 的 Transcript**：`read_file("skills/<skill-name>/runs/r<N>/test-{N}/transcript.jsonl")`
4. **檢視該 test 的 Output**：`list_dir("skills/<skill-name>/runs/r<N>/test-{N}/output/")` → `read_file` 各產出檔
5. **逐一評估 Expectation**：
   - **PASS**：有明確證據，且反映真正的任務完成，而非表面合規
   - **FAIL**：無證據，或證據與預期矛盾
   - **WEAK PASS**：正確的檔案名但內容空/錯（假精確）
6. **同時 critique 測試本身**：指出哪個 expectation 太弱或哪個重要結果沒被檢查
7. **每個測試案例 → 一個 `TaskDiagnostic`**，`mutation_suggestions` 填入具體改進方向

### Grader 輸出格式

```markdown
# Grader Report — {skill-name} — Round {N}

## 測試案例：{M}

### Expectation 1：{描述}
- **結果**：PASS / FAIL / WEAK PASS
- **證據**：[引用 transcript 或 output 中的具體文字]

### Expectation 2：{描述}
- **結果**：FAIL
- **證據**：[具體文字]
- **分析**：失敗原因是技能本身的問題還是 expectation 設計問題？

## 總結

| Metric | Value |
|--------|-------|
| Pass Rate | {X}/{Y} ({Z}%) |
| Weak Passes | {N} |
| Expectation Quality Issues | {M} |

## 結構化輸出（BatchDiagnostic）

Grader 完成後產出：
- **per-test `TaskDiagnostic`**：每個測試案例一個，`mutation_suggestions` 填具體 SKILL.md 改動
- **`BatchDiagnostic`**：包含所有 `TaskDiagnostic` + 整體 `pass_rate`
- **SemanticMemory 寫入**：`memorize("skill:{name}:eval:r{N}", "pass_rate={Z}%, {X}/{Y} passed. [關鍵觀察]")`
```

### Grader 呼叫方式

使用 `spawn_agent` 來執行 Grader sub-agent。**注意：所有 agent prompt 檔案都位於本技能目錄下的 `agents/`，不是呼叫方的 workspace**：

```
# 路徑：<loom_repo>/skills/meta-skill-engineer/agents/grader.md
# 用 read_file 載入整個檔案內容作為 task prompt
task: <讀入 skills/meta-skill-engineer/agents/grader.md 的完整內容>
tools: ['read_file', 'list_dir', 'run_bash']
context: {skill_name, test_case, transcript_path, output_dir}
```

> **路徑排查小抄**：若找不到 `agents/grader.md`，先用 `list_dir` 確認 `skills/meta-skill-engineer/` 存在，再列出其下的 `agents/`。這個路徑永遠是 **repo 根目錄相對**，不會出現在使用者 workspace 中。

---

## 階段 5：Comparator 對比（可選）

**目標：比較新舊版本的技能表現差異**

前提：必須同時存在「舊版技能輸出」和「新版技能輸出」。

### Comparator 流程

1. **不知道誰是 A、誰是 B** — 這是 Blind 測試
2. **讀取 A 和 B 的輸出**
3. **根據任務產生評估 Rubric**（Content + Structure 兩個維度）
4. **逐項打分（1-5 分）並給出理由**
5. **判定贏家**（可以平手）

### Comparator 輸出格式

```markdown
# Comparator Report — {skill-name} — v{N} vs v{M}

## 任務：{描述}

## Content Rubric
| Criterion | A | B |
|-----------|---|---|
| Correctness | 3 | 4 |
| Completeness | 4 | 3 |
| Accuracy | 4 | 4 |

## Structure Rubric
| Criterion | A | B |
|-----------|---|---|
| Organization | 3 | 5 |
| Formatting | 4 | 4 |

## 總分
| | A | B |
|---|---|---|
| Content | {score} | {score} |
| Structure | {score} | {score} |
| **Total** | {total} | {total} |

## 判定：{A / B / TIE}

## 理由
[A/B 取勝的具體原因]

## 結構化輸出
- Blind A/B 結果寫入 `SkillCandidate.pareto_scores`（key = task_type，value = 分數差值）
- `memorize("skill:{name}:compare:v{N}vsv{M}", "v{M} 勝出。理由：{摘要}")`
```

### Comparator 呼叫方式

```
# 路徑：<loom_repo>/skills/meta-skill-engineer/agents/comparator.md
task: <讀入 skills/meta-skill-engineer/agents/comparator.md 的完整內容>
tools: ['read_file', 'list_dir']
context: {output_a_path, output_b_path, eval_prompt, expectations}
```

---

## 階段 6：Analyzer 因果分析

**目標：理解「為什麼」並產出具體改進建議**

### 觸發條件

- **Comparator 判定 A 或 B 勝出** → 啟動 Analyzer
- **Comparator 判定 TIE** → 跳過 Analyzer，直接走階段 7（可用 Grader 的 `mutation_suggestions` 當作改進方向）
- **完全沒做階段 5**（首次建立技能、沒有舊版可比對）→ 跳過 Analyzer，改進建議由 Grader 階段 4 的 `mutation_suggestions` 提供

### 呼叫方式

Analyzer 建議用**新的 `spawn_agent` session** 執行，原因是 Comparator 在 blind 模式下不知道誰是 A/B，而 Analyzer 必須揭盲——放在同一 session 會把 blind 污染掉。

```
# 路徑：<loom_repo>/skills/meta-skill-engineer/agents/analyzer.md
task: <讀入 skills/meta-skill-engineer/agents/analyzer.md 的完整內容>
tools: ['read_file', 'list_dir']
context: {
  winner: "A" | "B",                    # 揭盲後的勝出方
  skill_a_path: "...",                  # 兩個版本的 SKILL.md 路徑
  skill_b_path: "...",
  transcript_a_path: "...",             # 兩個版本的執行 transcript
  transcript_b_path: "...",
  comparator_report_path: "..."         # 階段 5 產出的報告
}
```

### Analyzer 流程

1. **揭曉結果**：告訴 Analyzer 誰贏了（揭盲）
2. **讀取雙方 SKILL.md**：找出結構差異
3. **讀取雙方 Transcript**：比較執行模式差異
4. **分析指令遵循度**：雙方是否都忠實執行了技能指令？
5. **找出具體缺陷**：Loser 哪裡做錯了？Winner 哪裡做對了？
6. **產出改進建議**：每個缺陷都有具體修復方向，不是模糊建議

### Analyzer 輸出格式

```markdown
# Analyzer Report — {skill-name}

## 勝出者：{A / B}

## 關鍵差異分析

### 差異 1：{描述}
- ** Winner 做法**：{具體描述}
- **Loser 做法**：{具體描述}
- **影響**：{這個差異對最終輸出的影響}

## 具體改進建議（對 Loser）

### 建議 1：{標題}
- **問題**：{描述}
- **修復方向**：[非常具體的修改指示]

### 建議 2：{標題}
- **問題**：{描述}
- **修復方向**：{具體指示}

## 結構化輸出
- 具體改進建議直接填入每個 `TaskDiagnostic.mutation_suggestions`（這正是 Analyzer 存在的意義）
- `memorize("skill:{name}:insight:r{N}", "{Insight 內容}")`
```

---

## 階段 7：SkillMutator 候選生成 + 生命週期

**目標：把 BatchDiagnostic 轉化為演化動力，走候選池生命週期**

### 候選生成（agent 工具）

使用 `generate_skill_candidate_from_batch` 工具，把 Grader 匯聚的結果直接餵給 `SkillMutator`：

```
generate_skill_candidate_from_batch(
  skill_name = "<parent>",
  pass_rate = 0.85,
  previous_pass_rate = 0.60,   # optional — 有舊版才填
  mutation_suggestions = [...], # 每個 TaskDiagnostic 的建議去重彙整
  instructions_violated = [...],# 同上
  failure_patterns = [...],     # 同上
  avg_quality_score = 3.8,      # 可選，預設 3.0
)
```

工具回傳 `candidate_id` 與 `fast_track` 標記，候選進入 `skill_candidates` 表，**不直接修改 SKILL.md**——生命週期決策交給 `promote_skill_candidate` / `rollback_skill`。

### Fast-track 規則

| 情境 | 候選標記 | 下一步 |
|------|---------|--------|
| `batch.improvement ≥ fast_track_threshold`（預設 20%，可在 `[mutation].fast_track_threshold` 調整） | `fast_track=True` | 直接 `promote_skill_candidate` 或 `loom skill promote`，跳過 shadow 階段 |
| 其他情況 | `fast_track=False` | 進入 shadow 模式，積累 N-wins 後 promote |

Fast-track 的前提是必須有 `previous_pass_rate`（有舊版比較基線）。

### Confidence 統一由 EMA 驅動

Stage 7 **不再**手動調整 confidence。批次 diagnostic 的 `avg_quality_score` 會在後續 turns 的 `TaskReflector` EMA 路徑中自然累積。

### Maturity Tag

Agent 內用 `set_skill_maturity` 工具（推薦）；CLI 用 `loom skill set-maturity`：

| 條件 | Agent 工具 | CLI 等價指令 |
|------|-----------|-------------|
| 連續 3 輪 pass rate ≥ 90% | `set_skill_maturity(skill_name, tag="mature")` | `loom skill set-maturity {name} mature` |
| pass rate < 30% 且輪次 ≥ 5 | `set_skill_maturity(skill_name, tag="needs_improvement")` | `loom skill set-maturity {name} needs_improvement` |
| 清除標記 | `set_skill_maturity(skill_name, tag="clear")` | `loom skill set-maturity {name} --clear` |

Maturity tag 儲存在 `SkillGenome.maturity_tag`，可在 `loom review` 中查看。

### 重跑驗證

候選 promote 後，重跑測試集確認改善效果。若新版 pass rate 低於舊版，執行 rollback：

```bash
loom skill rollback {name}
loom review {name}   # 確認版本已回退
```

---

## 使用範例

### 情境 A：建立全新技能

```
使用者：「我想建立一個幫我寫測試的技能」
Loom（使用本技能）：
  → 階段 1：意圖確認（5 個問題）
  → 階段 2：生成 SKILL.md
  → 階段 2.5：設計 precondition checks（識別風險面 → 寫 checks.py → 宣告 frontmatter）
  → 階段 3：建立 5 個測試案例（含 checks 單元測試）
  → 階段 4：Grader 評估
  → 階段 7：SkillGenome 寫入 + 重寫 → 完成
```

### 情境 B：改善現有技能

```
使用者：「systematic_code_analyst 最近的分析有點淺，想改善一下」
Loom（使用本技能）：
  → 階段 5：Comparator（v1 vs 草稿版 v2）
  → 階段 6：Analyzer（解釋為什麼 v2 更好）
  → 階段 7：SkillGenome 更新 + 正式部署 v2
```

### 情境 C：純評估

```
使用者：「評估一下 news-aggregator 技能的表現」
Loom（使用本技能）：
  → 階段 3：建立/確認測試集
  → 階段 4：Grader 評估
  → loom review news-aggregator（產出報告）
  → 根據結果決定是否進入重寫流程
```

---

## loom review 指令

```bash
loom review {skill-name}
```

一站式查看技能全貌：
- **SkillGenome 狀態**：version、confidence、maturity_tag、usage_count
- **Grader eval 歷史**：所有 `skill:{name}:eval:r*` 記錄（pass rate 時間線）
- **Analyzer insights**：`skill:{name}:insight:*` 記錄
- **候選池**：所有候選的狀態、fast_track 標記、mutation_strategy
- **版本歷史**：最近的 promote / rollback 記錄

---

## Skill Genome 整合提示

- 本技能本身也是一個技能，會被 SkillGenome 追蹤
- 觸發時機：`meta-skill-engineer` / `建立技能` / `評估技能` / `改善技能`
- 使用次數越多，本技能的 confidence 越高
- 本技能的 SkillGenome 更新由框架自動處理（工具執行成功率）

---

## 紀律提醒

- **不做無測試的假設**：「感覺更好」不是更好，要有數據
- **不跳階段**：「先給我用再說」是不可接受的——沒有測試就沒有進化
- **Comparator 永遠不能知道誰是 A/B** — 揭盲之前的偏見是最難發現的
- **Grader 要批判測試本身** — 通過了弱 assertion 比失敗更危險
- **每次 Grader 後必須產出 BatchDiagnostic 並寫 SemanticMemory** — 否則數據就散了，閉環就斷了
- **用 `run_bash` 的技能必須有 checks** — SKILL.md 的文字紀律是給 LLM 看的，precondition_checks 是給框架執行的；兩者缺一不可
- **checks 不可有副作用** — 純判斷、純回傳 bool，不寫檔、不改狀態、不發請求
