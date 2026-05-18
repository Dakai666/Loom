# 創造（Create a New Skill）

**觸發後自動載入本檔案。**

從一段重複出現的工作中萃取出新技能。本檔案是 SOP，不是參考資料。

---

## 成功定義

- **產出**：`skills/<name>/SKILL.md` 至少完成，必要時加 `checks.py` / `contexts/`；git commit
- **品質指標**：description 能讓 LLM 自己判斷觸發；觸發訊號描述具體、能對應到使用者實際說話的方式；workflow 寫完使用者讀完能複現
- **驗收方式**：載入該技能後，沒有原作者在場也能依 SKILL.md 完成一次該工作

---

## 觸發前先確認

```
1. 這個工作真的出現過 ≥ 3 次嗎？
   - YES → 繼續
   - NO  → 「我注意到你是第一次提這個，要不要先做幾次再說？」
           ledger 可查：使用 skill_review 看相似 tool_lifecycle 模式

2. 已經存在的技能能不能涵蓋？
   - 翻 skills/ 目錄看現有 inventory
   - 翻 ~/.loom/skills/（全域技能）
   - 若可，加個 context 或擴 description 比建新技能成本低很多

3. 要建的是「新技能」還是「現有技能的新情境（contexts/）」？
   - 工作流跟現有技能的 Layer 1 心法一致 → 加 context
   - 心法不同 → 才建新技能
```

---

## 工作流程

### 第一步：找出重複工作的核心動作

從對話 / ledger 抽出：

- 觸發訊號是什麼？使用者通常怎麼開頭？（具體的 prompt 樣式）
- 工作步驟有哪些？哪些是「不變的核心」、哪些是「依情境變動的細節」？
- 產出長什麼樣子？格式、字數量、結構
- 哪些動作是「執行錯了會有真實成本」的？需要 `precondition_checks` 嗎？

**不要拿單一範例就動工**——回顧過去的對話樣本（≥3 次），看共通骨架。

### 第二步：草擬 frontmatter

```yaml
---
name: <lowercase-hyphenated 或 snake_case，看現有 inventory 風格>
description: "一句話講這個技能做什麼。當使用者說「<具體訊號 1>」、「<具體訊號 2>」、「<具體訊號 3>」時使用。"
# 可選欄位：
# model_tier: 2          # 推理重的技能可升 tier；不確定就不寫
# precondition_checks:   # 寫 / 改危險的目標時用
#   - ref: checks.<func_name>
#     applies_to: [write_file, run_bash]
# tags: [...]            # 影響 surface 機制（available_skills 過濾）
---
```

**description 寫得不好就觸發不到**：

- ❌「處理代碼相關工作」→ 太籠統，LLM 判斷不出來
- ✓「代碼理解 / 實作 / PR 審查 / 安全審查 / 發佈的統一入口。當使用者說「analyze code」、「review PR」、「修 bug」、「實作」時使用。」

### 第三步：決定要不要拆 `contexts/`

- **單一工作流** → SKILL.md 一檔搞定（看 review、init 這類簡單技能）
- **多情境分岔**（每種情境的 SOP 真的不同）→ SKILL.md 當 dispatch + `contexts/<ctx>.md` 拆細節（看 code_weaver、meta-skill-engineer 本身）

判斷標準：如果你發現自己在 SKILL.md 裡寫「如果情境是 A 則... 否則如果是 B 則...」三次以上，該拆 contexts/。

### 第四步：決定 `precondition_checks`

寫 / 改 / 跑 bash 的技能才需要。寫法：

```python
# skills/<name>/checks.py
async def <check_name>(call) -> bool:
    """單一布林條件；False = 阻擋"""
    ...
```

常見模式：

- `require_skills_dir_target`：限制 write_file 只能寫進 skills/
- `require_git_repo`：要求在 git repo 內
- `reject_force_push`：擋 `git push --force`

**過嚴會擋住正當工作**——只擋「執行錯了會有真實成本」的動作，不要擋「LLM 通常會做對的動作」。

### 第五步：決定該放 `~/.loom/skills/` 還是專案 `skills/`

- **`~/.loom/skills/`**（全域）：跨專案都用得到，跟特定 repo 結構無關。如 `pdf`、`audio_transcriber`、`pet-cat`
- **`<repo>/skills/`**（專案綁定）：依賴專案的 architecture / convention / 工具鏈。如 Loom 的 `code_weaver`、`meta-skill-engineer`

不確定時：**先放專案 skills/**。要全域化再搬，比反過來容易。

### 第六步：寫 SKILL.md 主體

最小可用 SKILL.md 結構（單情境版）：

```markdown
---
<frontmatter>
---

# <Skill Name>

<一句話說明 + 為什麼存在>

---

## 成功定義
- 產出：...
- 品質指標：...
- 驗收方式：...

## 工作流程
1. ...
2. ...

## 不在範圍
- ...

## 觸發關鍵詞
- ...

---

*<name> v0.1 — <date>*
```

多情境版（dispatch + contexts/）參考 `skills/code_weaver/SKILL.md` 或 `skills/meta-skill-engineer/SKILL.md` 的結構。

### 第七步：上線檢查與 commit

完成草稿後 → 切到 `contexts/register.md` 跑檢查清單 → git commit。

commit message 風格：`feat(skill): add <name> — <one-line reason>`，body 寫該技能解決什麼重複工作、何時觸發。

---

## 禁用事項

- ❌ 寫沒被觀察到的「自動演化」邏輯（mutate / shadow / promote）
- ❌ description 寫得文謅謅但不對應實際 prompt 模式
- ❌ 把 LLM 本來就會做對的事情包成技能（會稀釋 available_skills 訊號）
- ❌ 沒有 ≥3 次重複出現就硬建技能
- ❌ 寫死「特定使用者特定情境」的工作流——技能要可以被別人使用
