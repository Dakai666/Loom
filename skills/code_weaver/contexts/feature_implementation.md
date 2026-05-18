# 功能實作（Feature Implementation）

**觸發後自動載入本檔案。**

適用於明確的新功能、小改動、重構、或已由 `systematic_debugging.md` 確認 root cause 的修復。
若使用者只是描述 bug/error/failure，先回到 `systematic_debugging.md`，不要直接實作。

---

## 鐵律

```text
NO SYMBOL EDITS WITHOUT GITNEXUS IMPACT ANALYSIS FIRST
NO COMMIT OR PR WITHOUT GITNEXUS DETECT-CHANGES
```

---

## 成功定義

- **產出**：清楚 scope + 最小 git diff + 測試/驗證證據 + 可行動的 commit/PR 草稿
- **品質指標**：改動只落在約定範圍；每個 symbol 修改前有 impact；完成前通過 verification gate
- **驗收方式**：使用者能看懂「為什麼改這裡、沒改哪裡、怎麼證明有效」

---

## 工作流程（六階段）

### 階段 1：理解意圖

目標：確認任務不是 debug investigation，而是可以實作的需求。

- 取得 issue/需求/設計內容。
- 用一句話重述 intent。
- 若是 bugfix，確認已有 debug handoff：
  - root cause
  - evidence
  - fix scope
  - verification target
- 用 GitNexus 找相關流程：
  ```bash
  npx gitnexus query "<需求核心概念>"
  ```

產出：
```markdown
## Intent
[50 字內說清楚使用者要達成什麼]

## Candidate Symbols
- [symbol/file/process from GitNexus or code reading]
```

### 階段 2：Scope + Impact（必經）

目標：修改前先知道爆炸半徑。

對每個準備修改的 function/class/method 跑：
```bash
npx gitnexus impact <symbol> --direction upstream
```

向使用者報告：
```markdown
## Impact Analysis

**Target:** `<symbol>`
**Direct callers:** [count/list]
**Affected processes:** [list]
**Risk level:** [LOW/MEDIUM/HIGH/CRITICAL]
```

若 risk = HIGH/CRITICAL，先明確警告使用者並等待確認。

Scope 格式：
```markdown
## Scope

**In:**
- [要改的檔案/符號/行為]

**Out:**
- [看起來相關但這次不改的項目]

**Stop condition:**
- [發現什麼情況時要停下來回報]
```

### 階段 3：實作計畫

開始改檔前，用短計畫說明：

```markdown
## Implementation Plan

**Strategy:** [2-3 句，說明改法]

**Steps:**
1. [具體檔案與修改]
2. [測試或 fixture]
3. [驗證]

**Verification plan:**
- `pytest --collect-only`
- `pytest <relevant tests>`
- `npx gitnexus detect-changes`
```

若使用者已明確授權「直接改」，可以短講計畫後立即執行；若 scope 有風險或不確定，先等確認。

### 階段 4：實作

- 按計畫改，不順手重構。
- 如有行為變更，優先寫或更新測試。
- 發現 scope 需要擴大時停下來。
- 對 generated output 或 UI/display 改動，加入可視化或 sample output 驗證。

### 階段 5：Verification Gate

交付前讀 `contexts/verification.md`，用 gate function 驗證。

最低預設：
```bash
pytest --collect-only
pytest <relevant tests>
npx gitnexus detect-changes
git diff --check
```

若任何命令失敗，不可以說完成；回報失敗證據與下一步。

### 階段 6：產出

```markdown
## Result

**Changed:** [改了什麼]
**Why:** [為什麼這是最小足夠改動]
**Verification:** [命令 + 結果]
**Not changed:** [scope 外保留項]
```

PR/commit body：
```markdown
## Summary
- [具體改動]
- [測試/驗證]

## Verification
- `[command]` — [result]
- `npx gitnexus detect-changes` — [scope result]
```

---

## 紀律提醒

- bug 未完成 root cause investigation 前，不進本情境實作。
- 修改 symbol 前必跑 impact。
- scope 變大就停下來，不偷偷擴大。
- 沒有 fresh verification，不宣稱完成。
- commit/PR 前必跑 detect-changes。

---

*Code_Weaver 功能實作情境 · v3.0 — 2026-05-19*
