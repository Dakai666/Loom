# 註冊（Register / Onboard a Skill）

**觸發後自動載入本檔案。**

技能上線前的檢查清單。不是寫完 SKILL.md 就完事，要確認 LLM 觸發訊號清楚、precondition 不過嚴、放對位置。

---

## 成功定義

- **產出**：通過下方檢查清單的 SKILL.md（必要時 + checks.py + contexts/）+ git commit
- **品質指標**：載入該技能後，沒有原作者陪同也能完成一次該工作
- **驗收方式**：另一個 session 拿 SKILL.md 跑一次該技能、產出符合「成功定義」

---

## 上線檢查清單

### 1. Frontmatter — `description` 是否能觸發

LLM 看的就是 `available_skills` 裡這段 description。不夠具體 → 觸發不到 → 技能變死資產。

- [ ] 開頭一句話**動詞先行**講做什麼（「分析」「建立」「審查」...）
- [ ] 附「當使用者說 ... 時使用」並列出**至少 3 個**真實 prompt 樣式
- [ ] prompt 樣式是使用者**實際說話的方式**，不是文案

**反例**：「協助處理技能相關事務」← LLM 看不懂該不該觸發
**正例**：「技能的元技能：協助使用者建立新技能、做技能上線檢查、用 ledger 證據維護現有技能。當使用者說「我想做一個 X 技能」、「跑一下 skill_review」...時使用」

### 2. Frontmatter — `tags`

影響 surface 機制（available_skills 過濾）。

- [ ] 至少 2-3 個 tag，包含**領域**（coding / memory / audio）+ **動作類型**（review / create / search）
- [ ] 沒有 typo / 多餘空白
- [ ] 不要塞 marketing 詞（「powerful」「fast」），那不是 tag

### 3. Frontmatter — `precondition_checks`

過嚴會擋住正當工作；過鬆則丟掉護欄。

- [ ] 每個 check 有對應的 `checks.py` 函式且能 import
- [ ] `applies_to` 列出該 check 真的要擋的 tool（不要 `[*]` 萬用）
- [ ] description 一句話講「這條為什麼存在」
- [ ] 跑一次 mental test：能想到 ≥1 個合法使用會被擋住嗎？如果是，放寬

### 4. Frontmatter — `model_tier`（可選）

- [ ] 確定推理重才設 `model_tier: 2`（如 code_weaver、security_review）
- [ ] 純文字處理 / 模板填空 / 短查詢 → 不要設，預設 tier 1 即可
- [ ] tier 2 是 sticky 的，誤設會吃 token 預算

### 5. 工作流程 — SKILL.md 主體

- [ ] **成功定義**段落明確（產出 / 品質指標 / 驗收方式三欄）
- [ ] 工作流程是**可執行步驟**，不是抽象原則
- [ ] **不在範圍**段落明確切割邊界（這個技能**不**做什麼）
- [ ] 有「**觸發關鍵詞**」段落（給 LLM 反向確認情境的線索）

### 6. 多情境技能 — `contexts/` 拆分

只在多情境分岔技能適用。

- [ ] SKILL.md 有 dispatch 表（情境 / 觸發訊號 / 情境檔案 / 主要工具）
- [ ] 每個 `contexts/<ctx>.md` 開頭明說「觸發後自動載入本檔案」
- [ ] 每個 context 檔案結構一致（成功定義 / 工作流程 / 禁用事項 / 觸發訊號）
- [ ] dispatch 表的觸發訊號跟 context 檔案內的觸發訊號**一致**（不要兩處寫不同）

### 7. 部署位置

- [ ] 跨專案都用得到 → `~/.loom/skills/<name>/`
- [ ] 依賴特定 repo 結構 → `<repo>/skills/<name>/`
- [ ] 不確定 → 先放專案 `skills/`，需要再搬

### 8. Bookend 紀律提醒

- [ ] SKILL.md 沒有引導 agent「忘記 unload_skill」（如「使用完畢繼續其他工作即可」這種寫法 → 改成「使用完畢呼叫 unload_skill 收尾」）
- [ ] 工作流程裡如果會 `load_skill` 別的技能，也要記得對應的 `unload_skill`

### 9. 與其他技能的關係

- [ ] SKILL.md 末段或主體裡有一段「與其他技能的區別」表格
- [ ] 不跟既有技能職責重疊到無法區分（若重疊 → 該合併，不該並存）

### 10. Git Commit

- [ ] commit message：`feat(skill): add <name> — <one-line reason>`
- [ ] body 提及「該技能解決什麼重複工作 / 何時觸發」
- [ ] 同時 commit `skills/<name>/SKILL.md` + `checks.py` + `contexts/`（不要拆多 commit）

---

## 跑一次 dry-run（強烈建議）

在 new session 拿這個技能跑一次：

1. 給一個典型觸發 prompt
2. 看絲絲會不會自己 `load_skill <name>`（若否 → description 觸發訊號不夠強）
3. 跑完一次 → 看產出是否符合 SKILL.md 寫的成功定義
4. 不符合 → 回頭改 SKILL.md，**不要靠記憶補**

---

## 禁用事項

- ❌ 寫完就 commit，沒跑過一次驗證
- ❌ description 寫成文案而非觸發訊號集合
- ❌ precondition 「順手加幾條」沒思考阻擋面
- ❌ 多情境技能 SKILL.md 跟 contexts/ 寫不一致的觸發訊號

---

## 觸發訊號回顧

「我剛寫好 X 技能」「幫我看這個 SKILL.md 對不對」「這個技能可以上了嗎」「該不該加 precondition」「這個 description 夠清楚嗎」
