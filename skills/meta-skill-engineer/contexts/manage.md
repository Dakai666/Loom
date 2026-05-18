# 管理（Manage / Evolve Existing Skills）

**觸發後自動載入本檔案。**

用 `skill_review` + weekly 報告觀察既有技能的真實使用模式，做出 keep / rewrite / split / delete 的判斷。這是 doc/54 §4.3 的**通道 C**——Loom 演化的真正主路徑。

---

## 成功定義

- **產出**：基於 ledger 證據而非感覺做出決策；若決定動 → Edit SKILL.md / contexts / checks.py + git commit
- **品質指標**：每個決策都能引用具體 episodes / load 數 / unload_inferred 比例 / feedback 密度作依據
- **驗收方式**：使用者讀完絲絲的觀察能說「對，我也是這樣感覺，那就這樣動」，而不是「等等，這從哪裡來的？」

---

## 工作流程

### 第一步：拉資料

```
skill_review(
    skill_id="<目標技能>",
    days=7,                          # 預設 7 天；長時間趨勢可拉 30
    max_episodes=30,
    max_events_per_episode=30,
)
```

或對應地對照 weekly 報告：`outputs/self_check/<latest>-skill-weekly.md`。

### 第二步：解讀關鍵訊號

| 訊號 | 在 digest 裡叫什麼 | 解讀 |
|------|-------------------|------|
| **載入頻率** | `load_count` | 0 → 久未使用；1-2 → 偶用；高頻 → 主力技能 |
| **Bookend 完整性** | `unload_count` vs `load_count`、`unload_inferred` 比例 | inferred 多 = 絲絲沒在收尾，weekly 的 `muffled_run` 標籤要打折扣 |
| **反饋密度** | `events_after_load` 裡的 `memory_op` 數 | 低 → 「悶頭跑」訊號（可能是技能設計問題、或情境沒被注意） |
| **異常結尾** | `episodes` 裡的 `turn_outcome` 分布 | `error` / `abandoned` 占比高 → 技能可能在卡關 |
| **跨 session 廣度** | `sessions` 列表長度 | 1 → 單一場景；多 → 通用度高 |
| **失敗 tool 模式** | `events_after_load` 裡 `tool_lifecycle:END` 帶 `error` 的 | 反覆失敗的同一 tool → 技能該不該調整工作流？ |

**重要**：`unload_inferred=True` 的 episode 仍是有效證據，只是 boundary 是推導的。`no_unload_in_window` 比例高 → 找絲絲談 bookend 習慣，但不要把那批 episode 從分析裡丟掉。

### 第三步：對照 weekly 報告的「該關注清單」

weekly 報告會 surface 五種觀察：

| Reason | 意思 | 常見動作方向 |
|--------|------|------------|
| `muffled_run` | 高頻載入 + 零反饋 | 看是否 `unload_inferred` 多（修 bookend）；否則檢查工作流是否設計失靈 |
| `undigested_feedback` | 反饋多 + SKILL.md 久未動 | 反饋已收進 memory 但沒回灌到技能主體 → 該 Edit |
| `error_heavy` | 異常結尾比例高 | 工作流可能有結構性 bug，跑一次 skill_review 看具體在哪步爆 |
| `stale` | 30 天未載入 | 該不該存在？或定義太窄被冷落？ |
| `exists_but_unused` | 從未被載入 | 17/18 純文案問題：description 觸發不到、或職責不清楚 |

清單**只列觀察**、不附建議動作——使用者跟絲絲對話後再決定。

### 第四步：對話討論

把訊號講給使用者聽，**對著 evidence 講**：

```
✓「<skill> 過去 7 天載入 5 次，4 次 unload_inferred=True，
   feedback_events 0 個。觀察是 bookend 沒做完整、所以 muffled_run
   其實是假訊號。要不要先看 episode 2 的 tool 序列確認？」

✗「<skill> 好像沒人用，要不要砍掉？」  ← 沒 evidence
```

聽使用者補充情境（「對，那次我是中途切去做別的事」），共識後才動。

### 第五步：分類決策

| 動作 | 觸發條件 | 怎麼做 |
|------|---------|--------|
| **Keep** | 訊號正常或解釋清楚 | 不動，下次 weekly 再看 |
| **Rewrite** | description 不清 / 工作流過時 / Tier 設錯 / contexts 該補 | `read_file` 該 skill → Edit SKILL.md → 跑 register checklist |
| **Split** | 一個技能塞了兩個情境（Layer 1 心法不同） | 拆 contexts/<ctx>.md，或拆成新技能（看心法是否一致） |
| **Delete** | 久未使用 + 沒未來用途 + 沒人在乎 | 退役清單：SKILL.md → checks.py → 對應 session.py 引用（如有） → contexts/。完全刪除，別暫存（doc/54 §2 原則） |

### 第六步：Edit + git commit

每次動 SKILL.md 都要 commit。commit message 要**引用 evidence**：

```
refactor(skill): rewrite <name> description — load_count=12 over 7d
but trigger pattern misses「<具體 prompt>」(skill_review 2026-05-18)
```

或：

```
chore(skill): retire <name> — exists_but_unused for 60 days,
description overlaps with <other_skill>; future Evaluator may revisit
```

git history = 演化軌跡。不要 candidate / promote。

---

## 邊界

- ❌ **不下品質分數** —「成品好不好」沒有可信判定。surface 觀察、由人判斷
- ❌ **不自動 promote / 自動演化** —— Evaluator milestone 才會處理
- ❌ **不跨 skill 比較**（除了明顯職責重疊的 surface）—— 同上
- ❌ **不靠感覺** —— 沒引用具體 episode / load_count / outcome 分布的話，回頭去拉 skill_review

---

## 對話腳本參考

絲絲被觸發「跑一下 skill_review」時的開場：

```
1. 「好，先拉 <skill> 過去 7 天的 ledger。」
   → skill_review(...)
2. 「我看到 ... 個訊號值得討論：[列 2-3 條 evidence-bound 觀察]。」
3. 「你要先聊哪一條？」
4. （等使用者選 → 深入該條 → 共識動作 → Edit/commit）
```

避免：

- 一次把所有 episode 全 dump 出來（會淹沒對話）
- 還沒問就直接 Edit SKILL.md（沒 alignment 改了等於白改）
- 沒看具體 episode 就建議「砍掉」（先看才能避免誤殺）

---

## 禁用事項

- ❌ 用 `skill_review` 跑完後不引用具體 episode 編號就下結論
- ❌ 把 `unload_inferred=True` 的 episode 整批丟掉
- ❌ 看到 `exists_but_unused` 就建議刪 — 先問「這個技能本來預期什麼時候用？」
- ❌ 改完 SKILL.md 沒 commit
- ❌ 為了「乾淨」整批退役老技能 — 退役要逐個對話確認

---

## 觸發訊號回顧

「跑一下 skill_review」「weekly 顯示 X 技能該關注」「X 技能可以更好」「Y 技能該不該砍」「翻新 Z 技能」「拆 X」「為什麼 X muffled_run」
