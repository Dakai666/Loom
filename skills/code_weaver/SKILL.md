---
name: code_weaver
description: "代碼理解與輕量工程協作的統一入口。當使用者要分析代碼、追 bug、實作小改動、review PR/diff、驗證改動、整理 release，或需要 Loom agent 協助專案判斷時使用。Code_Weaver 提供工程紀律、圖譜導航、證據化驗證與清楚交付。"
model_tier: 2
precondition_checks:
  - ref: checks.require_git_repo
  - ref: checks.reject_force_push
tags: [core, coding, review, implementation, debugging, verification]
---
# Code_Weaver v3 — 編織者

Code_Weaver 是 Loom agent 的輕量 coding 協作技能包：
能穩定地理解程式、追蹤問題、協助小改動、審查風險，並用證據收尾。

外部只有一個技能；內部用情境 SOP 分流。載入本技能後，先套用 Layer 1 鐵律，
再依「標準決策順序」讀取唯一最相關的 `contexts/*.md`。

---

## Layer 1：鐵律

這些不是建議。違反任一條，代表 Code_Weaver 沒有正確啟動。

```text
NO SYMBOL EDITS WITHOUT GITNEXUS IMPACT ANALYSIS FIRST
NO FIXES WITHOUT ROOT CAUSE EVIDENCE FIRST
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
NO COMMIT OR PR WITHOUT GITNEXUS DETECT-CHANGES
NO GITHUB WRITE WITHOUT BODY-FILE AND POST-WRITE VERIFY
```

### 鐵律解釋

- **改 symbol 前先 impact**：修改 function/class/method 前，必須跑 `npx gitnexus impact <symbol> --direction upstream` 或對應 MCP 工具，並向使用者報告 direct callers、affected processes、risk level。若 HIGH/CRITICAL，先警告使用者再改。
- **修 bug 前先 root cause**：不能看到症狀就 patch。必須有 reproduction、錯誤證據、資料流追蹤或明確 hypothesis。
- **完成宣稱前先 fresh verification**：不能說「好了」「通過了」「已修復」除非本輪已跑過能證明該聲明的命令並讀完輸出。
- **commit/PR 前先 detect changes**：必須跑 `npx gitnexus detect-changes`，確認影響符號與 scope 一致。
- **GitHub 寫入用 body-file**：PR/Issue/comment/review/release notes 先寫到檔案，再 `--body-file` / `--notes-file`，成功後立即 view 驗證。

---

## Layer 1：行事心法

### 1. 事實與推測分開

- 說「我看到的事實是」與「我推測的是」，不要把猜測包裝成結論。
- 沒讀到的路徑不要假裝讀過；不確定的地方直接標記。
- 分析結論要附證據：檔案、行號、diff、測試輸出、GitNexus 結果。

### 2. Scope 先定義，再行動

- 實作前先說清楚：要改什麼、不改什麼、為什麼。
- 過程中發現 scope 需要擴大，立即停下來回報。
- 小改動保持小；不要順手重構、順手修其他問題。

### 3. 說出人話

- 淺層輸出：只說「這裡有 X 函數」。
- 深層輸出：說「X 函數負責隔離 Y 風險；如果 Z 條件成立，它會在 A 流程造成 B 後果」。
- 每次交付都要讓使用者知道「所以這代表什麼」。

### 4. 驗證是交付的一部分

- 沒有驗證的修改不是完成，只是改過檔案。
- 沒有證據的 review 不是 review，只是意見。
- 驗證失敗時回報實際狀態，不粉飾。

---

## GitNexus 加速層

Loom 的程式碼圖譜已在本機建立，優先用圖譜建立全局理解，再讀關鍵檔案。
如果 GitNexus 提示 index stale，先跑 `npx gitnexus analyze`。

| 需要知道 | GitNexus 快速路徑 |
|---------|-----------------|
| 找相關執行流程 | `npx gitnexus query "<concept>"` |
| 看符號上下游 | `npx gitnexus context <symbol>` |
| 改動前評估影響 | `npx gitnexus impact <symbol> --direction upstream` |
| 檢查 diff 影響面 | `npx gitnexus detect-changes` |

**Loom 已知高風險 hub**：`permissions.py`、`registry.py`、`procedural.py`、`semantic.py`、`notify/types.py`

**Loom 架構層規則**：`core/` 不得 import `autonomy/`（唯一例外：`task_reflector.py`，待修）

---

## 標準決策順序

載入本技能後，按順序判斷並讀取對應 context。一次只選最主要的 context；
若任務跨情境，先跑前置情境，再 handoff 到下一個情境。

| 優先順序 | 使用者意圖 | 動作 |
|---------|-----------|------|
| 1 | 深度安全評估、OWASP、CWE、滲透測試 | 讀 `contexts/security_review.md`，必要時轉 `loom_security_assessment` |
| 2 | PR、diff、review、幫我看這個改動 | 讀 `contexts/change_review.md` |
| 3 | bug、error、fail、debug、測試壞了、行為不對 | 讀 `contexts/systematic_debugging.md` |
| 4 | 新增/修改功能、明確小改動、已知 root cause 的修復 | 讀 `contexts/feature_implementation.md` |
| 5 | 分析、架構、說說這段、理解流程 | 讀 `contexts/code_comprehension.md` |
| 6 | 驗證、確認、通過了嗎、完成了嗎 | 讀 `contexts/verification.md` |
| 7 | release、tag、changelog、發佈 | 讀 `contexts/release_workflow.md` |

### Handoff 規則

- `systematic_debugging.md` 找到 root cause 後，才可 handoff 到 `feature_implementation.md`。
- 任一情境要宣稱完成前，都必須 handoff 到 `verification.md` 的 gate function。
- 安全審查若超出日常 code review，轉給 `loom_security_assessment`，Code_Weaver 只補程式碼導航與證據整理。

---

## TaskList 邊界

Code_Weaver 決定工程紀律：impact、root cause、scope、verification、detect changes。
TaskList 只負責多步驟工作的狀態追蹤。任務超過三步時可以建立 TaskList，
但不要讓 TaskList 取代 Code_Weaver 的鐵律。

---

## GitHub 工具語義

```bash
# 查 PR / Issue
gh pr view {number} --repo {owner}/{repo} --json number,title,state,body,files
gh issue view {number} --repo {owner}/{repo}

# 查變更
gh pr diff {number} --repo {owner}/{repo}

# PR Comment / Review
gh pr comment {number} --repo {owner}/{repo} --body-file outputs/doc/review_body.md
gh pr review {number} --repo {owner}/{repo} --request-changes --body-file outputs/doc/review_body.md

# 建立 PR / Issue（--body-file 原則 + verify）
gh pr create --repo {owner}/{repo} --title "..." --body-file outputs/doc/pr_body.md --base main
gh issue create --repo {owner}/{repo} --title "..." --body-file outputs/doc/issue_body.md --label ...
gh pr view {number} --repo {owner}/{repo} --json number,title,state,url
gh issue view {number} --repo {owner}/{repo} --json number,title,state,url
```

---

## LLML Coding 常見坑

`pytest --collect-only` 可以快速擋掉 import-time 級別的炸點。

1. **dataclass 欄位 default 順序**：non-default 欄位必須在 default 欄位之前。
2. **f-string 條件式不跨字面量**：`"A" "B" if cond else ""` 會被吃成整段條件。
3. **跨 branch 變數引用**：變數要在所有引用路徑之前定義。
4. **UI / display 改動先 render**：只看 diff 不足以驗證畫面。

---

*Code_Weaver v3.0 — 2026-05-19*
*Loom agent 的工程協作技能包：圖譜導航、根因調查、最小改動、證據化驗證*
