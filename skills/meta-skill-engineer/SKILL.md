---
name: meta-skill-engineer
description: "元技能工程師：系統化建立、評估、迭代改進 Loom 技能的技能。當使用者要求「建立一個新技能」、「改善現有技能」、「評估技能表現」、「跑技能對比測試」、「系統化迭代技能」時使用。本技能為 Skill Genome 提供評估閉環——Grader 的 pass/fail 分數直接寫入 SkillGenome.confidence，形成測試→記憶→演化的完整循環。"
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
階段 6：Analyzer 因果分析
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

**產出：** 一段「技能意圖說明書」（不超過 200 字）

---

## 階段 2：草稿生成

**目標：產出第一版 SKILL.md**

根據意圖說明書，生成完整的 SKILL.md。

### 標準 SKILL.md 結構

```markdown
---
name: [skill-name]
description: "[觸發描述]。當使用者要求[場景]時使用。"
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

**產出：** `skills/[skill-name]/SKILL.md`

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

**目標：客觀測量技能表現**

使用 Grader Agent 對每個測試案例進行評估。

### Grader 評估流程

1. **讀取 Transcript**：測試執行後的完整對話記錄
2. **檢視 Output**：技能產出的實際檔案/文字
3. **逐一評估 Expectation**：
   - **PASS**：有明確證據，且反映真正的任務完成，而非表面合規
   - **FAIL**：無證據，或證據與預期矛盾
   - **WEAK PASS**：正確的檔案名但內容空/錯（假精確）
4. **同時 critique 測試本身**：指出哪個 expectation 太弱或哪個重要結果沒被檢查

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

## SkillGenome 寫入

- `memorize("skill:{name}:eval:r{N}", "pass_rate={Z}%, {X}/{Y} passed. [關鍵觀察]")`
- `relate("skill:{name}", "evaluated_at", "r{N}")`
- `relate("skill:{name}", "pass_rate", "{Z}")`
```

### Grader 呼叫方式

使用 `spawn_agent` 來執行 Grader sub-agent：

```
task: [Grader agent prompt from agents/grader.md]
tools: ['read_file', 'list_dir', 'run_bash']
context: {skill_name, test_case, transcript_path, output_dir}
```

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

## SkillGenome 寫入
- 如果 B 勝出：`memorize("skill:{name}:compare:v{N}vsv{M}", "v{M} 勝出。理由：{摘要}")`
- `relate("skill:{name}", "compared_versions", "v{N}v{M}")`
```

### Comparator 呼叫方式

```
task: [Comparator agent prompt from agents/comparator.md]
tools: ['read_file', 'list_dir']
context: {output_a_path, output_b_path, eval_prompt, expectations}
```

---

## 階段 6：Analyzer 因果分析

**目標：理解「為什麼」並產出具體改進建議**

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

## SkillGenome 寫入
- `memorize("skill:{name}:insight:r{N}", "{Insight 內容}")`
- `relate("skill:{name}", "learned_from", "v{M}→v{N}")`
```

---

## 階段 7：SkillGenome 寫入 + 重寫

**目標：把評估結果轉化為演化的動力**

### SkillGenome 更新規則

| 情境 | 更新動作 |
|------|---------|
| Grader pass rate ≥ 80% | confidence += 0.05（上限 1.0） |
| Grader pass rate < 50% | confidence -= 0.1 |
| Comparator 新版勝出 | version += 1，confidence 不變 |
| 連續 3 次 pass rate ≥ 90% | 標記為 `mature` |
| pass rate < 30% 且 n ≥ 5 | 標記為 `needs_improvement` |

### 重寫觸發條件

當以下任一條件成立時，觸發 SKILL.md 重寫：
- pass rate < 70%
- Analyzer 發現系統性缺陷
- 使用者明確要求改善

### 重寫流程

1. 根據 Analyzer 的具體改進建議修改 SKILL.md
2. 保持 description / name / 核心原則不變
3. 只修改「工作流程」和「紀律提醒」部分
4. 重跑測試集，驗證改善效果

---

## 使用範例

### 情境 A：建立全新技能

```
使用者：「我想建立一個幫我寫測試的技能」
Loom（使用本技能）：
  → 階段 1：意圖確認（5 個問題）
  → 階段 2：生成 SKILL.md
  → 階段 3：建立 5 個測試案例
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

使用 `loom_review.py` 或直接查詢 semantic memory：

```bash
loom review {skill-name}
```

輸出：
- 所有測試輪次的 pass rate
- 最近的 Grader 報告摘要
- SkillGenome 當前 confidence 和 version
- 已知缺口（has_known_gap）
- 與其他技能的應用次數對比

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
- **每次 Grader 後必須寫 SkillGenome** — 否則數據就散了，閉環就斷了
