# 生成認知整合 — 語義型觀察面擴張（Semantic Observation-Surface Expansion）

> **狀態**：🟢 **已拍板（2026-09-06，見 §9）**——§6 六點以四方視角（DK 產品節奏 / Codex 硬契約 / 絲絲 end-user / CC 架構）收斂定案，DK 保留否決權；實作隨 issue #569 的 PR 進行。追蹤 issue：**#569**。
> **上游**：epic #528 / P0 收斂規格 `docs/designs/58`（§4 resolver 白名單、§5 I2 硬約束、§10 Deferred）
> **觸發（原）**：P0 acceptance gate 觀察期實測——reliability 軸近乎退化（1570/1572 error=0.0），逼出「訊號從哪來」的真問題
> **觸發（升級，2026-09-06）**：絲絲自發採用 explicit `predict` 工具後，**14 筆賭裡 13 筆（93%）用語義 resolver（output_contains/output_regex），全部餓死永不結算 → 學習迴路零閉合 → 她 8/19 停止下注**。本文從「隱式賭的 nice-to-have 增量」變成「explicit 路徑能不能活」的前提。詳見 §8。
> **先行落地（本輪 code，非本文範圍）**：latency heartbeat（`duration_bucket @ <tool>@latency`），證明**不動觀察面**就能取非平凡訊號。本文談的是**下一階增量**——那一階非動觀察面不可。

---

## 0. 一句話

Resolver 白名單（`docs/designs/58` §4）早就列了 `output_contains` / `output_regex` / `file_digest_changed` / `row_count`，但**action observation 不吐它們要的欄位**，所以這些 resolver 對「動作結算」的下注全部餓死。本文評估「把觀察面餵飽」這件事——它直接動到 spec 視為神聖的 **I2 ground-truth 邊界**，所以先討論、後動刀。

---

## 1. 問題：resolver 有嘴，observation 沒料

P0 acceptance gate 觀察期（2026-06-10 → 06-21，1572 筆隱式下注）撈出來的事實：

| 軸 | resolver | error_score 分佈 | 判讀 |
|---|---|---|---|
| reliability（已有） | `tool_success` | **1570×0.0 / 2×1.0** | 近乎退化——tool 幾乎都成功，naive prior「會成功」幾乎不會錯 |
| latency（本輪先上） | `duration_bucket` | 0.00..1.00 跨 43 domain | 非平凡——`read_file` 永遠 fast、`recall` 0.57 銅板、媒體生成幾乎不 fast |

latency 軸救起了「非平凡」，但它量的是**延遲**，不是**語義正確性**。真正貼近 #528 原意（「預測對不對」而非「有沒有失敗」）的，是這類賭：

- 「這個 `write_file` 真的改了檔案內容」→ `file_digest_changed`
- 「這個 `run_bash` 的輸出會含 `PASS`」→ `output_contains` / `output_regex`
- 「這個 `recall` 會撈回 ≥1 筆」→ `row_count`

**這些 resolver 都在白名單裡、`resolve()` 都能算**。卡點唯一：

```python
# loom/core/memory/observation.py :: _resolve_action()
return {
    "observation_ref": ...,
    "tool_name": r[1],
    "final_state": r[2],
    "tool_success": not failed,
    "duration_ms": r[3],
}
# ← 沒有 output / digest_before / digest_after / row_count
```

`resolve()` 對缺欄位的 observation 丟 `KeyError`（刻意——「不可觀測 ≠ 預測正確」，no-silent-pass，配 I4）。所以這些賭一律落 `unresolvable`，calibration 一筆都收不到。

**`action_records` 表本身也沒存這些欄位**（25849 筆查證：無 output、無 digest 欄）。所以這不是「extractor 漏讀」，是**捕捉管線根本沒留下這份 ground truth**。

---

## 2. 為什麼這直接撞 I2

I2（`docs/designs/58` §2 / §5）：**ground truth 只可來自 runtime observation 白名單表**——`OBSERVATION_SOURCES = ("action", "session_log")`，且 reconcile 每個 score 必指向白名單表的某個 row（#531 observation_ref 契約）。

要讓語義型 resolver 可對帳，等於**擴張 ground-truth 的表面積**。這不是加個欄位那麼輕——它動到的正是「spine 拿什麼當真相」這條命脈。兩個風險：

1. **把 LLM 自我敘事偷渡成 ground truth**。若 `output` 取的是 agent 事後對結果的「描述」而非工具的**原始 stdout/return**，I2 就破了——calibration 會學到「我覺得我做對了」而非「世界回了什麼」。
2. **digest 的計算時點**。`file_digest_changed` 要 `digest_before` / `digest_after`，這要求捕捉**動作前後**的檔案狀態。若只在動作後算一次，等於沒有 before，resolver 退化。

> 結論：擴張觀察面**可以做**，但**必須證明擴張進來的每個欄位仍是「世界回的」而非「agent 說的」**。這是本文 review 的核心審查點。

---

## 3. 兩條路（沿用 #541 的 (a)/(b) 框架）

#541（事件結算下注）已經把同一張地圖畫好了，本文是它的「動作結算」對應版：

### (a) 動作捕捉時多留一份可對帳觀察（傾向）

在 lifecycle 捕捉縫，把工具的**原始輸出 / 檔案 digest** 落進一個 observation row（擴 `action_records` 欄，或新 observation 表），reconcile 照舊只讀白名單表。

- ✅ **I2 純粹不變**：ground truth 仍只來自白名單表的 row，只是 row 變寬。
- ✅ 對齊既有紀律——「要可對帳就得留下可審計的觀察」（#541 對 (a) 的同款論證）。
- ⚠️ 代價：捕捉管線要負責持久化原始輸出。**大輸出**（bash 吐幾 MB、檔案內容）要決定**存摘要還是存全文**——傾向只存 **digest / 截斷前綴 / 命中布林**，不吞整包（呼應 `project_compression_belongs_in_autonomy_path`：tool-output 爆 context 是真風險）。

### (b) 放寬 I2 白名單 / resolver 改吃別的來源

讓 resolver 去讀 `session_log`（已有 `output`/`raw_json`）或新增 observation 來源。

- ⚠️ `session_log` 的 `output` 是**對話輸出**不是**工具原始 return**——拿它對帳極易把 LLM 敘事當真相（撞風險 1）。
- ⚠️ 動到 `OBSERVATION_SOURCES` = 動神聖邊界，blast radius 大。

> **傾向 (a)**，理由同 #541：不破 I2，且「留下可審計觀察」本身是對的紀律。但**存什麼粒度**（digest-only vs 截斷前綴 vs 全文）是 (a) 內部待拍板的子決策——留給 review。

---

## 4. Per-tool resolver 映射（草案，待 review）

擴張後，auto-predict 的隱式下注可依工具型別選 richer resolver（取代或並列現有的 reliability + latency 雙注）。**naive prior 不可偷看結果**——expect 必須是固定先驗，方差才來自「先驗對不對」：

| 工具型別 | resolver | naive prior（expect） | 方差來源 |
|---|---|---|---|
| 變動類 `write_file` / `journal_append` / `memorize` | `file_digest_changed` | `True`（寫了就會變） | no-op 寫入（內容相同）、寫了但沒落盤 |
| 執行類 `run_bash` | `output_regex`（錯誤樣式）/ `output_contains` | 視場景 | 命令靜默失敗、輸出不如預期 |
| 讀取類 `recall` / `list_dir` / `read_file` | `row_count >= 1` | `True`（讀得到東西） | 空 recall、空目錄、檔案空 |

> **開放問題（review 點）**：
> - run_bash 的「先驗」難定——「輸出含 X」的 X 從哪來？可能 run_bash 這類**自由輸出工具退回只用 reliability + latency**，語義型只給「結構化結果工具」（write/read/recall）。
> - 變動類用 `file_digest_changed` 需要 before-digest——這把「動作前快照」推進 lifecycle，成本與 TOCTTOU 要評（呼應 `feedback_snapshot_guards_inputs`）。

---

## 5. 與已落地件的關係

- **latency heartbeat（本輪 code）**：證明「不動觀察面也能取非平凡訊號」。本文是**正交的下一階**——語義軸。兩軸可並存（domain 各自 namespace：`<tool>` / `<tool>@latency` / 未來 `<tool>@semantic`），`compute_calibration` 按 domain 分桶，互不污染。
- **#541（事件結算下注）**：同一張 (a)/(b) 地圖的「外部事件」分支。本文是「動作結算」分支。兩者若都選 (a)，可共用「先落一筆可對帳 observation row」的同一套機制——**review 時值得一起看能不能收斂成單一 observation-row primitive**。
- **I5 valence**：語義型 resolver 多半**無 good/bad 內在極性**（`file_digest_changed` 變了是好是壞？看不出來）→ 沿用 `_POLAR_RESOLVERS` 現制，valence=0，不讓 spine 自創價值判斷。

---

## 6. 待拍板（交 DK / Codex / 絲絲 輪替）

1. **走 (a) 還是 (b)**？（傾向 a）
2. (a) 下，原始輸出**存什麼粒度**？digest-only / 截斷前綴 / 命中布林 / 全文。
3. `file_digest_changed` 要不要 before-snapshot？成本 vs 訊號值。
4. run_bash 這類自由輸出工具**納不納**語義軸，還是只給結構化結果工具。
5. 語義軸是**取代**現有雙注，還是**第三條並列**（三注/動作）？volume 與 reconcile backlog 影響（#553/#554 餘波）。
6. 能否與 #541 收斂成**單一 observation-row primitive**（動作結算 + 事件結算共用）。

---

## 7. 不做 / 邊界（繼承 58 §11）

- ❌ 不讓 `session_log` 的對話輸出冒充工具原始 return（I2）。
- ❌ 不在擴張裡引入任何「軟判 / 氛圍」對帳（那是 58 §10 的軟判層，另案）。
- ❌ 本階仍不接兩臂、不驅動行為——只是把 calibration 訊號從「延遲可預測性」加上「語義正確性」一軸。
- ❌ 未過 review、未定稿前不進 code。

---

## 8. 實測驗證（2026-09-06）— 本文從「增量」升級為「blocker」

本文 6/21 寫成時，explicit `predict` 工具剛上線、**零真實下注**，論證只能靠隱式賭的理論增量。9/6 對帳撈出生產資料，把假設變成事實，並揭露一個 6/21 沒預見的維度：**驅動力不是隱式賭，是 explicit 工具 + agent 的自然傾向**。

### 8.1 絲絲自發採用了 predict——然後停了

- **採用**：8/06→8/19 絲絲自發下了 **14 筆** explicit 賭（無人強迫，早於 8/25 nudge 硬化 merge）。品質正是 #528 原意——不確定性導向、真判斷（「這次夢會不會解析出有效 triples」「Dakai 會不會夜間修好根因」「fetch 會不會回文章原文」）。
- **停止**：8/19 後**至今（9/6，18 天）零新增**。不是重啟或 config 弄壞——她就是不再下注了。

### 8.2 根因：她自然抓的 resolver，正是本文說餓死的那批

| resolver kind | 筆數 | 結算 | 命運 |
|---|---|---|---|
| `tool_success` | 2 | **2/2 reconciled** ✓ | 有 `final_state` 可讀 |
| `output_contains` | 11 | **0/11 永遠 pending** ✗ | action observation 無 output 欄位 |
| `output_regex` | 1 | **0/1 永遠 pending** ✗ | 同上 |

resolver-kind 完美預測結算與否。**13/14（93%）落在本文 §1 說的 unresolvable 虛空。** 她只有 2 筆得到回饋，其餘 12 筆 score=None 永遠 pending。

> 這印證了本文 §4 的開放問題答案：agent **自然伸手抓的是語義型 resolver**（`output_contains` 是表達「真內容預測」最直覺的方式），而非 reliability/latency。所以「語義軸只給結構化結果工具、自由輸出工具退回 reliability+latency」的保守選項（§4 開放問題）**會砍掉 agent 最想下的那種賭**——這是本文原本低估的。

### 8.3 對 roadmap 的意義

- explicit 路徑（真正的 prediction-skill 訊號、eval 支柱的核心）**從一開始就餓死在 resolver 層**。7/2 acceptance gate 的「過關」是 auto latency 軸撐的，explicit 路徑其實沒活過。
- 因此「翻 `calibration_write_enabled`」這一步**過早**——幾乎沒有可信 explicit 訊號可校準。**本文（#59）才是 explicit 路徑的真前置。**
- 8/25 的 provenance 修復（`explicit:` 前綴保留、PR #564）雖正確，但當下 moot——沒有新 explicit 賭可分類，MONOCULTURE 不會靠它消退，因為卡點在更深的 observation 層。

### 8.4 §6 待拍板點的實測傾向

實測資料對 §6 幾個問題給了方向（仍待輪替確認）：

- **Q4（自由輸出工具納不納語義軸）**：**應納**。絲絲對 `run_bash`/`fetch_url`/`dream_cycle` 都下了 output_contains 賭——把它們排除等於排除她 79% 的賭。難點在「先驗 X 從哪來」：可考慮讓 **explicit 賭由 agent 自帶 expect**（她下注時已寫明「含 valid triples」），隱式賭才需要固定先驗。這條 explicit/implicit 分流是 6/21 沒想到的。
- **Q2（存什麼粒度）**：explicit 賭多為「輸出**含**某字串」→ 存 **digest + 截斷前綴** 對多數命中布林已足夠；全文只在 regex 需要時。仍傾向不吞整包（`project_compression_belongs_in_autonomy_path`）。
- **次要坑（非本文主症但同層）**：`next_action` 綁定是同 session 的——絲絲對「下一次 dream_cycle」下注，但排程夢常在別的 session，即使 resolver 可對帳也會 orphan。§6 收斂 observation-row primitive 時應一併看跨 session 結算（#541 事件結算的近親）。

---

## 9. 拍板記錄（2026-09-06）

§6 六點定案。每點附證據與否決代價；DK 保留否決權。實測補充證據（拍板時撈生產庫）：**12 筆餓死賭中 10 筆同 session 有 settling row**——真根因確定是缺 output 欄位，不是跨 session orphan；絲絲 14 筆的 needle/pattern 全長 **4–27 字元**。

| # | 拍板 | 理由（濃縮） |
|---|---|---|
| Q1 | **走 (a)：擴 `action_records` 欄** | I2 白名單（`OBSERVATION_SOURCES`）一字不動，row 變寬即可。(b) 的 session_log 是對話形狀、無結算索引；其 raw_json 變體也輸在可審計性（一 row = 一 action = 一 observation 才是乾淨的 observation_ref）。 |
| Q2 | **截斷前綴（16K chars）+ sha256 digest + 全長 + row count；不存全文** | 真 needle 全是短字串，前綴幾乎必中；digest+全長讓「needle 沒中且被截斷」誠實落 `unresolvable` 而非假 `absent`（no-silent-pass 一致）。全文 forensics 已由 session_log 承擔——**不引入新的隱私暴露類別**，也不吞 DB。 |
| Q3 | **before-snapshot defer**；但 `predict` 工具**寫入時拒收** `file_digest_changed` | 生產用量 0/14；動作前快照推進 lifecycle 是真 slice（TOCTTOU，`feedback_snapshot_guards_inputs`）。拒收訊息給出可用替代——**寫入時拒絕勝過讓賭爛在 pending**，那正是趕走絲絲的失敗模式。 |
| Q4 | **自由輸出工具應納語義軸；explicit 賭自帶 expect，隱式賭不配先驗** | 她 79% 的賭在 run_bash/fetch_url/dream_cycle；needle 本來就在 resolver spec 裡，「先驗從哪來」對 explicit 路徑是偽問題。 |
| Q5 | **不加第三條隱式心跳** | blocker 是 explicit 餓死，不是 implicit 量不足；再加 auto 量只會加深 MONOCULTURE。reliability+latency 雙注不動，語義軸只走 explicit。等 explicit 訊號質量被證實後再議。 |
| Q6 | **不預建單一 observation-row primitive** | `action_records` 本身就是 observation row（fat structure 形狀已對，convergence-over-abstraction）。#541 按其自訂啟動條件仍休眠；它醒來時複製「事件先落一筆可對帳 row」的**慣例**，不是現在共建一張表。跨 session 結算同樣 defer（實測 10/12 同 session 可結算，是次要坑）。 |

### 9.1 語義細則（實作契約）

- **正規字串** = agent 在對話中看到的同一份世界回覆：`str(result.output)`（成功）/ `error` 字串（失敗）。失敗時的 error 也是世界回的（如 429 文本），可被 output_contains 對帳。
- **row_count 的機械定義**：list/tuple → `len()`；字串 → `splitlines()` 行數。純世界函數，不猜語義。
- **截斷語義**：needle/pattern **沒中**且捕捉被截斷 → `unresolvable`（走既有 skip 稽核路徑），永不誤判為「不含」。中了就是中了（前綴中 = 全文必中）。
- **舊 row（無捕捉欄）** → 語義 resolver 照舊 `unresolvable`，不回填、不假分。
- **收尾 ops**：部署後將 12 筆已餓死的 explicit 賭一次性 `mark_stale`（需 DK 授權），讓 `unresolvable` skip 指標成為本修復的乾淨觀測訊號。
