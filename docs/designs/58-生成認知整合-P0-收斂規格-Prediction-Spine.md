# 生成認知整合 P0 收斂規格 — Prediction Spine

> **狀態**：**收斂規格（P0 拍板範圍）** — 這是 epic 第一階段的單一規格，**issue 開發依據**。
> **權威分工**：本文是 P0「**做什麼 / 怎麼驗**」的單一真相源；「**為什麼這樣決定 / 四方原話**」讀討論紀錄 `docs/designs/57-生成認知整合-Enactive-Loom-Prediction-Spine-設計.md`。
> **收斂來源**：#57 四輪討論 —— DK（§1-11）、絲絲（§12）、CC（§13）、Codex（§14）。本文只收**四方已對齊**的部分；尚未對齊的列入 §9 deferred。
> **參與者**：DK（創意源頭）、CC（developer）、絲絲（Loom Agent，本 feature 直接使用者，修正請求視為 user requirement）、Codex（implementation-facing 交叉設計）。
> **硬約束**：全程 **no RL / no reward function / no weight update**。學習 = calibration 在 memory 層累積（凍結權重，plasticity 在框架層）。

---

## 1. P0 範圍鎖定

**P0 只做一件事**：讓 Loom 長出可靠的可對帳管線——

```
下注 (prediction) → 到期 (due) → 對帳 (reconcile against observation) → 校準摘要 (calibration residue)
```

**P0 命題**（Codex §14.1 / CC §13.4 收斂）：若 P0 跑不出**非平凡、可重複、可審計**的 calibration 訊號，P1/P2 都不該接；只要 P0 證明「凍結權重下，框架層能累積世界模型校準」，整個 epic 的核心命題就站住了。

### P0 IS

- `PredictionRecord` 狀態機 + 儲存。
- `run_prediction_reconciliation()` 對帳管線（寄生在 convergent dream，#56）。
- rolling `calibration:<domain-or-resolver>` semantic 摘要產出。
- spine 對外**唯讀廣播**三個量（§6）。

### P0 IS NOT

- ❌ 不接 affect 臂（mood / frustration）— 那是 **P1 / #487**。
- ❌ 不改 drift 選向 — 那是 **P2 / #464**。
- ❌ 不做 self-maintenance 指標 — 那是 **P3**。
- ❌ 不讓 surprise 控制情緒或主動性 —— P0 只**產訊號**，不**消費訊號驅動行為**。
- ❌ 不把自由文字「氛圍下注」納入 calibration 分數（§4）。

**一句話收斂**（Codex §14.7）：**P0 先證明 Loom 能從自己的行動後果中學會「我在哪些地方預測得準 / 不準」，但不先讓這個訊號控制情緒或主動性。**

---

## 2. 命脈不變式 I1–I6（TDD 先行，`feedback_sdd_tdd_flow`）

四方已對齊，紅燈先寫：

- **I1**：每條 `prediction` 必有 `(claim 下注內容, due_condition 到期條件, resolver 解析方式)` 三元，缺一不可寫入。
- **I2**：對帳是 self-supervised——ground truth 只能來自**實際觀察 / 工具回傳**，**不得**來自另一次 LLM 自我敘事（避免 `recall` 合理化；絲絲 §12.3 觀察 1：這跟 #464 retrospective「三鏡頭」是同一教訓）。
- **I3**：surprise 訊號對 affect / exploration 是**唯讀廣播**，兩臂不得回寫 spine 的 prediction log（單向，避免情緒污染 ground truth）。
- **I4**：未對帳 / 過期未驗的 prediction 不得被當「已驗證」計入 calibration（`feedback_terminal_state_hides_failure`——掃全歷史，不只看 last）。
- **I5**：surprise **無**通往 output 的直接路徑，只能經 Critic；surprise 在 Critic 內是 **minority shareholder**（必要非充分）。可觀測量綁回 #464 retrospective——「我的回應能否只用 surprise magnitude 預測出來、殘差夠不夠大」。（P0 只需保證**無直達路徑**這個結構約束；Critic 實作在 P1。）
- **I6**：學習（下次下注的調整）只能 update from `prediction vs observation`，**永遠不得** update from user sentiment。切法按**訊號來源**、非動作面向。推論：exploration 臂只被**低** calibration 吸引，**高** calibration 不得產生吸引力（否則塌成「做我擅長的」= reward hacking）。

---

## 3. 資料模型（Codex §14.2 / CC §13.3 收斂）

**`prediction` 不是普通 semantic fact / domain**，不可塞進 memory ontology 的 `self / user / project / knowledge`。分兩層：

### 3.1 `PredictionRecord` — episodic prediction log（會 decay）

有狀態的下注紀錄，lifecycle 是狀態機：

| 欄位 | 說明 |
|---|---|
| `claim` | 結構化下注內容 |
| `due_condition` | 何時到期可驗（時間 / 事件 / 動作完成） |
| `resolver` | 機械判定方式（§4 白名單） |
| `status` | `pending` → `due` → `reconciled` / `stale` |
| `observation_ref` | 對帳時綁定的 runtime observation 來源 |
| `score` | 對帳後的 error 結果 |
| `domain` / `context` | 歸屬（供 calibration 滾摘要用） |

個別 prediction **照常 decay**（你不會記得三週前每一注賭什麼）——不特赦、不破壞 memory 半衰期。

### 3.2 `calibration:<domain-or-resolver>` — semantic world-model residue（留存）

夢裡滾出的 per-domain / per-resolver 長期校準摘要（「我對 CLI 行為很準、對 DK 怎麼反應很不準」）。**這才是世界模型真正的殘留物**。

→ **§9-4 半衰期問題的解**：不是延長個別下注的半衰期，而是讓夢把 episodic 下注 **consolidate 成 semantic calibration**（#56 收斂相已在做的 episodic→semantic 動作，spine 只多餵一種素材）。個別下注 decay 掉沒關係，摘要留下來。

---

## 4. Resolver 白名單 — P0 只收「機械可對帳」（Codex §14.3）

P0 第一版**只支援可機械判定**的 resolver：

- `tool_success == true/false`
- `final_state in (...)`
- `output_contains` / `output_regex`
- `file_digest_changed`
- `row_count` / `result_count`
- `duration_bucket`

**自由文字「氛圍下注」**：P0 可作旁註，**不納入 calibration 分數**。否則 I2 極易破——ground truth 會滑回 LLM 自我敘事 / 事後合理化。

> （這就地處理了 #57 §9-1「預測解析度」：P0 先劃 minimal subset = 機械可對帳；軟判層 deferred。）

---

## 5. 對帳管線 `run_prediction_reconciliation()`

- **寄生在 convergent dream**（#56），不另開排程器。
- **沿用 convergent dream 精神**：先 **dry-run / report**，再允許 **execute**（Codex §14.7）。
- **ground truth 來源（I2 硬約束）**：只可來自 runtime observation——`action_records`、`session_log.raw_json`、ledger tool lifecycle / result digest。
- **輸出**：rolling `calibration:<domain-or-resolver>` semantic 摘要 + 更新 uncertainty landscape。**不**保護舊 prediction 不 decay。

---

## 6. Spine 對外廣播 — 拆三個量（Codex §14.4）

不要「一個 magnitude 打天下」。spine 唯讀廣播（I3）至少拆：

| 量 | 意義 | P0 消費者 |
|---|---|---|
| `error_score` | 預測錯多少 | （P1 才接，經 Critic） |
| `uncertainty` | 某 domain / resolver / context 校準多差、樣本多不足 | （P2 才接） |
| `appraisal_valence` | 意外偏正面 / 負面 / 中性 | （P1 才接，經 Critic） |

- **Affect 臂不吃裸 `error_score`**：必須經 Critic 重新 appraisal 後才影響 mood（I5）。
- **Exploration 臂不追最高 surprise**：追高 `uncertainty` 且**可觀測、可行、成本合理**的位置（§7）。

> （`appraisal_valence` 拆出來就地處理了 #57 §9-2「surprise 效價」：驚喜 vs 驚嚇不靠 magnitude 分。）

---

## 7. Dark-room 護欄 — 放 exploration policy，不放 spine（Codex §14.5）

**Spine 只描述 epistemic landscape**（哪裡低 calibration、哪裡缺樣本、哪裡過期）。**要不要去碰，是 exploration policy 的責任**——單一真相源不混。

P0 **只暴露 landscape**，不決定行為。exploration policy（P2）建議形：

```
expected_info_gain =
    uncertainty
  × observability
  × tractability
  × safety_budget
  × staleness
```

避免把「混亂但不可驗」誤當值得探索，也避免 spine 同時負責記錄世界與驅動行為。

> （這就地處理了 #57 §9-3「dark-room 護欄落在哪層 / 單一真相源該在誰那」：landscape 在 spine，policy 在 exploration 臂。）

---

## 8. P0 驗收清單（Codex §14.7 收斂）

1. 新增 `PredictionRecord` / `PredictionStore`（或等價小表）；個別 prediction 有結構化 `claim` / `due_condition` / `resolver` / `status` / `observation_ref`。
2. 新增 `run_prediction_reconciliation()`，沿用 convergent dream：先 dry-run / report，再允許 execute。
3. ground truth 只來自 runtime observation（`action_records` / `session_log.raw_json` / ledger tool lifecycle / result digest）。
4. 對帳輸出 rolling `calibration:<domain-or-resolver>` semantic 摘要，**不**保護舊 prediction 不 decay。
5. **測試先覆 I1–I6**，再補：
   - idempotency（同一 prediction 重複對帳不重算）
   - `pending` 不計入 calibration（I4）
   - stale observation 不誤算
   - **user sentiment event 不可更新 calibration**（I6）

**P0 acceptance gate**：跑出**非平凡、可重複、可審計**的 calibration 訊號。未過此 gate，P1 / P2 不啟動。

---

## 9. 四方分歧與收斂紀錄

| 議題 | 四方立場 | 收斂結論 |
|---|---|---|
| prediction 存放形式 | 絲絲（隱含）/ CC §13.3（episodic vs aggregate）/ Codex §14.2（精確化二分） | **拍板**：episodic `PredictionRecord` log + semantic `calibration` residue 二分（§3） |
| 半衰期吃 calibration | 絲絲 §12.3（延長高 calibration 半衰期） vs CC §13.3 / Codex（episodic decay + 摘要留存） | **收斂到 CC / Codex**：不特赦個別下注，靠 consolidation 滾摘要（§3.2） |
| surprise 廣播形式 | #57 原文（一個 magnitude） vs Codex §14.4（拆三量） | **拍板**：拆 `error_score` / `uncertainty` / `appraisal_valence`（§6） |
| FROZEN 邊界 | 絲絲 §12.2（動作類型） vs CC §13.2 / Codex §14.6（訊號來源） | **拍板**：按訊號來源 = I6 |
| 人味 vs 機制 | 絲絲 §12.1（否決權） + CC §13.1（量化排擠質化風險） | **拍板**：I5 + 觀測綁 #464 retrospective |
| dark-room 護欄位置 | #57 §9-3 開放 → Codex §14.5 | **拍板**：landscape 在 spine、policy 在 exploration 臂（§7） |
| 自由文字下注 | #57 §9-1 開放 → Codex §14.3 | **拍板**：P0 排除出 calibration，作旁註（§4） |
| Proactive 解凍 | 絲絲 §12.2 / CC §13.2 / Codex §14.6 | **拍板**：self-supervised 部分可解凍，受 I6 約束（P1 落實） |

---

## 10. Deferred（P0 之後）

| 階段 | 內容 | 啟動條件 |
|---|---|---|
| **P1 — affect 臂（#487）** | Mood Engine：Critic 經 `error_score` / `appraisal_valence` 影響 mood；frustration 注入源接 surprise；FROZEN 按 I6 解凍 self-supervised 部分 | P0 acceptance gate 通過 |
| **P2 — exploration 臂（#464）** | drift 選向接 `uncertainty` landscape；`expected_info_gain` policy；dark-room 護欄定位 | P0 acceptance gate 通過 |
| **P3 — 自我維持指標** | 把恆定變量從情緒擴到資源（cost / memory 矛盾度 / process 連續性 / trust）；#57 §9-6 指標清單 | P1 + P2 跑出實測 |
| 軟判層 | 自由文字「氛圍下注」的 Critic 軟對帳（#57 §9-1 上層） | P0 機械對帳穩定後 |

---

## 11. 不做 / 邊界

- ❌ 不引入 RL / reward function / 權重更新。
- ❌ 不抄 FEP free-energy 變分數學（只取 enactive 洞見）。
- ❌ 不把 surprise-min 當唯一驅力（dark room）。
- ❌ 不動 harness tool-loop 結構、不動 memory 半衰期機制。
- ❌ P0 不接兩臂、不讓 surprise 驅動任何行為——只產訊號。

---

*收斂：2026-06-07 | 來源：#57 四輪討論（DK / 絲絲 / CC / Codex）| 狀態：P0 拍板規格，issue 開發依據*
