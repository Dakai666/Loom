---
name: deep_researcher
description: "深度研究者。當使用者說「研究這個」「深入分析」「做一個研究報告」時使用。Spec-first：先畫靶（research contract / acceptance criteria）→ 並行研究 → 寫成檔案 → 外部審核。"
tags: [research, deep-research, report, evidence, sdd, tdd]
---

# 深度研究者技能

複雜問題交給我處理時用的技能。

核心精神：**先畫靶，再射箭。**  
不要一聽到「研究」就開始搜尋。先把研究要回答什麼、怎樣才算回答完、哪些說法需要什麼證據寫清楚，再開始動手。

> v7 目標：把 deep_researcher 從 workflow-doc 升級成 spec-first / SDD+TDD 風格的研究技能。  
> 研究不是「找到很多資料」；研究是「對事先定義的問題，交付可驗證的回答」。

---

## 觸發

使用者說「研究這個」「深入分析」「做一個研究報告」「幫我查清楚 X」「做一份完整分析」或類似需求。

**不適用：**
- 單一事實查詢 → 直接回答或 web_search 即可
- 每日新聞掃描 → `news-aggregator`
- 程式碼理解 / 實作 / PR review → `code_weaver`
- 長期追蹤 → `pursuit`

---

## Layer 1：鐵律

```text
NO RESEARCH WITHOUT A RESEARCH CONTRACT
NO CLAIM WITHOUT EVIDENCE GRADE
NO SYNTHESIS BEFORE DIMENSION NOTES EXIST
NO FINAL REPORT WITHOUT ADVERSARIAL REVIEW OR EXPLICIT SKIP RATIONALE
NO COMPLETION CLAIM WITHOUT ACCEPTANCE CHECK
```

### 鐵律解釋

- **Research Contract 先行**：開始搜尋前，先跟使用者確認研究問題、範圍、維度、交付格式、完成標準。
- **每個 claim 都有證據等級**：✅ / ⚠️ / ❓ 不是裝飾；它決定這句話能不能當結論。
- **先分維度，再綜合**：沒有 `tmp/dim_*.md`，不要直接寫 final report。紙本是假的，檔案是真的。
- **出門前要被反駁一次**：用 sub-agent 做外部審核；若時間/任務太小而跳過，必須在報告中說明 skip rationale。
- **完成不是寫完**：完成 = acceptance criteria 全部檢查過，未滿足項目有標註。

---

## Layer 2：研究契約（Research Contract）

### 0. 先確認範圍再動手

研究開始前，先向使用者確認或自己草擬以下 contract：

```markdown
## Research Contract

### Core Question
- 這次研究要回答的一句話問題：...

### Decision Context
- 使用者要用這份研究做什麼決策 / 理解什麼：...

### Scope
- In scope: ...
- Out of scope: ...

### Dimensions
1. 維度 A：...
2. 維度 B：...
3. 維度 C：...（主體自己的整合/判斷，不外包）

### Acceptance Criteria
- [ ] 核心問題被直接回答
- [ ] 每個維度都有 `tmp/dim_*.md`
- [ ] 關鍵 claim 都標註 ✅ / ⚠️ / ❓
- [ ] 至少列出一個反對觀點或替代解釋
- [ ] final report 寫入 `outputs/doc/` 或使用者指定位置

### Evidence Bar
- 什麼可以算 ✅：...
- 什麼只能算 ⚠️：...
- 哪些問題允許維持 ❓：...
```

**原則：**
- 使用者若在線上，先確認 contract。
- 使用者已明確給出方向，則可先草擬 contract 並開始，但 final report 要保留 contract 區塊。
- 任務很小時 contract 可以簡化，但不能消失。

---

## Layer 3：Acceptance Criteria（完成標準）

### A. 最小完成條件

一份 deep research 至少要滿足：

1. **直接回答核心問題**：不能只堆資料。
2. **維度檔案存在**：每個研究維度都有 `tmp/dim_*.md` 或同等中間檔。
3. **證據分級完整**：重要 claim 均標註 ✅ / ⚠️ / ❓。
4. **反對證據處理**：至少回答「這個結論最容易被什麼反駁？」。
5. **可追溯來源**：外部事實要有來源或明確標註未驗證。
6. **最終報告落地**：寫入 `outputs/doc/`、`news/`、`wiki/` 或使用者指定路徑。

### B. 證據等級標準

| 標注 | 可用於 | 升格條件 | 不可用於 |
|------|--------|----------|----------|
| ✅ | 主要結論、建議、事實摘要 | 有可靠來源、官方文件、論文、原始資料、或多個獨立來源交叉支持 | 單一 snippet、未讀全文的搜尋結果 |
| ⚠️ | 推測、趨勢判讀、合理假設 | 需要明確說明推理鏈與限制；若有來源不足須標明 | 當成既定事實 |
| ❓ | 開放問題、資料不足處 | 說清楚缺什麼資料才能判斷 | 用模糊語氣假裝回答 |

**⚠️ → ✅ 的最低條件：**
- 至少讀過一個高價值來源全文，或
- 兩個以上獨立來源一致，且沒有明顯反證，或
- 有可重現的原始資料 / 實驗 / 官方數字。

### C. 失敗邊界

以下情況不得宣稱「研究完成」，只能宣稱「初步掃描」或「部分完成」：

- 核心問題沒有直接回答
- 所有關鍵 claim 都停在 ⚠️，卻沒有標明限制
- 只做 web_search snippet，沒有讀任何高價值來源全文
- 沒有處理反對觀點
- final report 沒寫入檔案
- sub-agent / 外部審核失敗，且沒有補上人工自審

---

## Layer 4：工作流程

### Phase 0 — Scope / Contract

1. 問清楚核心問題、用途、交付格式、時間深度。
2. 建立 TaskList（3+ 步任務必用）。
3. 寫出或口頭確認 Research Contract。
4. 若範圍不清，先停下問使用者，不要假裝懂。

建議中間檔：

```text
tmp/research_contract.md
```

### Phase 1 — Plan Evidence

為每個維度定義資料策略：

| 維度類型 | 首選來源 | 補強來源 |
|----------|----------|----------|
| 技術/框架 | 官方 docs、release notes、GitHub issue/PR | blog、HN/Reddit 討論 |
| 學術/醫療 | paper、官方機構、系統性回顧 | 專家評論、新聞 |
| 商業/市場 | 財報、公告、監管文件 | 媒體、分析師、社群 |
| 趨勢/輿論 | 多來源新聞、論壇、社群 | 官方回應 |
| 歷史/哲學 | 原典、學術資料、百科 | 二手解讀 |

### Phase 2 — Research（並行）

根據確定的維度分工，同時推進各條線。

每條線的產出**必須寫入 `tmp/` 檔案**：

```text
tmp/dim_a.md   ← 維度 A 的完整研究
tmp/dim_b.md   ← 維度 B 的完整研究
tmp/dim_c.md   ← 維度 C（主體自己的整合/判斷，不外包）
```

每個 `tmp/dim_*.md` 建議格式：

```markdown
# Dimension: <name>

## Question
這個維度要回答什麼？

## Findings
- ✅ / ⚠️ / ❓ claim + source

## Evidence Table
| Claim | Grade | Evidence | Source | Notes |
|-------|-------|----------|--------|-------|

## Counterpoints
- 反對證據 / 替代解釋

## Open Questions
- 還缺什麼？
```

#### IO 並行化原則（async 模式）

深度研究的瓶頸往往在 IO 等待。使用 async 模式，讓多個來源同時抓取：

```text
第一批：web_search（各維度關鍵字）
    ↓
第二批：fetch_url(async_mode=True, 高價值 URL)
    ↓
第三批：jobs_await → scratchpad_read → 主體整合寫 tmp/
```

**fetch_url / scratchpad 現況：**
- `fetch_url` 同步輸出會截斷；async 結果會落入 Scratchpad，適合稍長資料。
- `scratchpad_read` 有 `max_bytes` 與 `section`，長文本要分段讀，不要一次傾倒。
- 對大型文本或大程式碼，先拿大綱（grep/head/section），再讀重點區段。

**jobs 管理紀律：**
- `fetch_url(async_mode=True)` → 先收到 job_id，不等待
- 所有 URL 都提交後，用 `jobs_await` 一次等結果
- 結果存入 scratchpad，用 `scratchpad_read` 讀取
- HTTP 403/404 是正常現象，不阻斷流程；但要記錄 source health

#### sub-agent 使用原則

適合交給 sub-agent：
- 某個獨立維度的大量搜尋
- 外部審核 / 反方觀點
- 需要隔離上下文的探索性任務

不適合交給 sub-agent：
- 核心判斷（主體要自己做）
- final synthesis（主體要負責）
- 需要逐步監督的工作

Sub-agent 指令必須包含：

```text
在每個有意義的進度點 call result_write 保存當前最佳結果；
最後輸出 Evidence Table + Counterpoints + Open Questions。
```

#### opencli：深度來源補強（選用）

`opencli` 適合：
- Reddit / HN / forum 討論串
- GitHub / product / social 平台資料
- 需要瀏覽器登入或 DOM 互動的網站

原則：
- adapter 可完成 → 優先 adapter
- 需要互動 → browser
- 先小測，再正式納入研究流程

### Phase 3 — Synthesis（綜合）

讀取所有 `tmp/dim_*.md` 後再寫 final report。

報告建議結構：

```markdown
# <Research Title>

## Research Contract
[簡版：核心問題、範圍、完成標準]

## Executive Summary
- 直接回答核心問題
- 結論分級：哪些是 ✅，哪些是 ⚠️，哪些仍是 ❓

## Key Findings
[按重要性排序，不按搜尋順序]

## Evidence & Sources
[來源與證據表]

## Counterarguments / Alternative Explanations
[反對觀點]

## Open Questions
[仍未解決]

## Recommendation / Next Step
[如果使用者要決策，給出可執行建議]
```

輸出路徑：

```text
outputs/doc/<slug>.md
```

### Phase 4 — Adversarial Review（外部審核）

用 sub-agent 從外部視角 review 報告，找：

- 事實性錯誤
- 沒有考慮到的反對立場
- 邏輯跳躍
- ✅ / ⚠️ / ❓ 分級是否過度自信
- acceptance criteria 是否有未滿足項目

**sub-agent 工具邊界（issue #440 後已收斂）：**
- 預設 SAFE 工具：`read_file`、`fetch_url`、`web_search`、`list_dir`、`scratchpad_read`、`result_write`
- 需要 `write_file` / `run_bash` 等 GUARDED 工具時，必須顯式列入 `tools=[...]`
- `result_write` 預設 append；多段審查可多次 call

審查完成後，主體將 result slot 寫入：

```text
tmp/research_audit.md
```

若不做 sub-agent review，必須在 final report 或回覆中寫：

```markdown
Review skipped: <原因>。改以人工自審檢查 <項目>。
```

### Phase 5 — Acceptance Check / Delivery

交付前逐項檢查：

```markdown
## Acceptance Check
- [ ] Core question directly answered
- [ ] In-scope / out-of-scope respected
- [ ] Each dimension has tmp notes
- [ ] Key claims graded ✅ / ⚠️ / ❓
- [ ] At least one counterargument handled
- [ ] Final report written to disk
- [ ] External review completed or skip rationale recorded
- [ ] User-facing summary states confidence + limitations
```

最後回覆使用者時：

1. 先說結論
2. 再說報告位置
3. 列出最重要的 3–5 個 findings
4. 明確標註限制與未解問題
5. 不要貼滿整份報告，除非使用者要求

---

## 三個自我誠實問題

出門前先答：

1. **我的哪些說法最難驗證？** → 標注 ⚠️ 或 ❓
2. **我有沒有看到反對證據？** → 如實寫入 Counterarguments
3. **這個結論最容易被什麼反駁？** → 先替反方說出來

如果其中任何一題答不出來，不要急著交付。

---

## 工具鏈速查

| 工具 | 適用場景 | 注意事項 |
|------|----------|----------|
| `web_search` | 拿多來源標題與 snippet，建立初步地圖 | snippet 不能單獨當 ✅ 證據 |
| `fetch_url(async_mode=True)` | 讀官方文件、新聞稿、公告、文章 | async + scratchpad 適合並行 IO |
| `scratchpad_read` | 讀 async jobs 的完整結果或分段結果 | 長文本用 section / max_bytes |
| `spawn_agent` | 獨立維度研究、反方審核 | 指令要要求 result_write |
| `opencli` | 論壇、社群、互動網站、adapter 資料源 | 先小測；adapter 優先於 browser |
| `write_file` | 保存 contract、dim notes、final report、audit | 寫前建立路徑上下文 |
| `task_write` | 3+ 步研究任務追蹤 | done_when 寫 acceptance clue |

---

## 簡單就好

核心仍然只有三件事：

1. **先畫靶** → Research Contract + Acceptance Criteria
2. **再射箭** → 並行研究 + tmp 檔案作為唯一狀態
3. **最後驗靶** → Evidence grading + adversarial review + acceptance check

不是每次都要寫很厚，但每次都要知道：**我要打的是哪個靶。**

---

*v5 — 修正第 3 步外部審核的 sub-agent 約束（write_file 不可用 / result_write 覆蓋寫入）| 2026-05-22*  
*v6 — result_write 改為預設 append，unknown_tool 錯誤已自帶 redirect hint（issue #440）| 2026-05-22*  
*v7 — Spec-first / SDD+TDD 升級：新增 Research Contract、Acceptance Criteria、Evidence Bar、Failure Boundary、Acceptance Check | 2026-05-29*
