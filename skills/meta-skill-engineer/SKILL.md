---
name: meta-skill-engineer
description: "技能的元技能：協助使用者建立新技能、做技能上線檢查、用 ledger 證據維護現有技能。當使用者說「我想做一個 X 技能」、「幫我看這個 SKILL.md 對不對」、「跑一下 skill_review」、「weekly 顯示 X 技能該關注」、「X 技能可以更好」、「翻新 / 拆 / 刪 Y 技能」時使用。"
precondition_checks:
  - ref: checks.require_skills_dir_target
    applies_to: [write_file]
    description: "只能寫入 skills/ 目錄，不可修改框架或使用者程式碼"
tags: [meta, skill, create, register, manage, evolution]
---

# Meta Skill Engineer — 元技能工程師

**技能的元技能**：當絲絲與使用者要建立、上線、或維護一個技能本身時的工作流程。

> Quest D Phase 1 翻新版（2026-05-18，issue #366）。退役了 SkillGenome / Grader / Comparator / candidate-promote 七層架構，重建在 doc/54 §4 三通道 + `skill_review` 工具 + weekly 報告之上。

---

## 核心原則（Layer 1）

> **本技能所有情境共同遵守的內建紀律。**

### 原則 1：可驗證的才自動化

「成品好不好」目前沒有可信判定機制（Evaluator milestone 才會處理）。在這之前：

- 不寫「自動演化」邏輯
- 不下「品質分數」
- 不做「shadow / promote / candidate」生命週期
- 用 ledger 證據呈現現況，由使用者判斷該不該動

### 原則 2：完全刪除 > 暫存

退役一個技能或概念時，代碼 + config + 文件一次性清乾淨。不要為「未來可能還會用到」留死規格——未來真的要重起時重寫一份比解 8 個月前的半完成品快。

### 原則 3：先確認意圖，再動手

不清楚要做什麼之前不寫 SKILL.md。常見錯誤是「使用者隨口一句就被收進技能」——這會生出沒人會用的技能。看到「這個工作我做第 3 次了」才是建技能的訊號。

### 原則 4：即時反饋走對話、不走框架

「使用者根據反饋 → 絲絲直接 Edit SKILL.md」是 conversational flow，不要包成 candidate/promote 框架。git commit 就是版本紀錄。

### 原則 5：bookend 是 skill_review 的閉環契約

`load_skill` / `unload_skill` 必須成對。skill_review 的活動窗口依賴這對 bookend；忘記 unload 會讓 `unload_inferred=True` 出現，數據還能用但 boundary 是推導的。這條紀律是使用者+絲絲共同維護，沒有 framework auto-unload（PR #393 已 rule out）。

---

## 情境分岔（Layer 2）

> **載入本技能後的第一件事：識別情境 → 立刻 `read_file` 對應的 `contexts/` 檔案。**
>
> 情境檔案是 **SOP**，決定產出結構、成功定義、與使用者互動的節奏。Layer 1 是「怎麼做才對」，情境檔案是「做完長什麼樣子」。
>
> 情境不明確時，主動詢問：「你要建一個新技能、檢查現有技能上線、還是想維護一個既有技能？」

| 情境 | 觸發訊號 | 情境檔案 | 主要工具 |
|------|---------|---------|---------|
| **創造** | 「我想做一個 X 技能」「這段流程能不能變成技能」「幫我把 Y 寫成技能」 | `contexts/create.md` | write_file（限 skills/） |
| **註冊** | 「我剛寫好 X 技能」「幫我看這個 SKILL.md 對不對」「這個技能可以上了嗎」 | `contexts/register.md` | read_file、人工 review |
| **管理** | 「跑一下 skill_review」「weekly 報告 X 技能該關注」「X 技能可以更好」「Y 技能該不該砍」 | `contexts/manage.md` | skill_review、weekly 報告 |

---

## 技能的生態位置

| 元件 | 角色 |
|------|------|
| `load_skill` / `unload_skill` | agent 自己拉技能 / 收技能；bookend 必須成對 |
| `skill_review` tool | agent 主動拉某技能的 ledger 使用紀錄回顧（PR #386） |
| Weekly worker | 每週六排程從 ledger 抽現況，寫到 `outputs/self_check/{date}-skill-weekly.md`；附該關注清單 |
| `ToolCallDimension` | 即時對話內顯示 tool 統計與 anomaly，是反饋的最隱性層 |
| `~/.loom/skills/` vs `<repo>/skills/` | 全域技能（跨專案）vs 專案綁定技能 |

doc/54 §4 三通道：對話內即時反饋 / weekly 報告 / 對話內 ledger 調閱（skill_review）。**通道 C 是真正的優化主路徑**——本技能的 `manage` 情境就是走這條。

---

## 不在範圍

- 自動 grading / 自動演化 → Evaluator Skill milestone（未來）
- 跨 skill 比較 / co-occurrence 分析 → 同上
- 修改框架代碼 / 使用者程式碼 → 由 `precondition_checks.require_skills_dir_target` 阻擋

---

## 觸發關鍵詞速查

- 創造：「建立技能」「新技能」「寫成技能」「重複工作」「把 X 變成技能」
- 註冊：「上線」「review skill」「frontmatter 對嗎」「該放哪裡」「該不該加 precondition」
- 管理：「skill_review」「weekly」「muffled_run」「該關注」「翻新」「拆」「砍」「該不該動」

---

*meta-skill-engineer v2.0 — 2026-05-18 · issue #366 翻新*
*核心架構：doc/54 §4 三通道 + skill_review (PR #386) + weekly worker · 不依賴 Evaluator milestone*
