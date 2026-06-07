# 生成認知整合 Enactive Loom — Prediction Spine × Affect / Exploration 兩臂 — 設計文件

> **狀態**：**討論紀錄**（四輪：DK §1-11 → 絲絲 §12 → CC §13 → Codex §14，2026-06-05→06-07）— 四方原聲存檔，**保留不再改寫**。
> **收斂去處**：P0 的拍板規格已收斂進 `docs/designs/58-生成認知整合-P0-收斂規格-Prediction-Spine.md`。**要看「拍板要做什麼 / 開 issue 依據」讀 #58；要看「為什麼這樣決定 / 四方原話」讀本文。**
> **定位**：把三條原本各自走的線收成一個 epic ——
> - 🎥 理論：Enactive Cognition / Active Inference（Sutton 等人，"生成認知" 影片）
> - 🌙 **#487 Mood Engine**（內在驅動 + Frustration metabolism + Critic 感知層）
> - 🌀 **#464 drift 層 comment**（「無用的能動性」、DK 不在時自己找事做）
> **核心主張**：三者是同一台機器的三個器官。#487 是 **affect 臂**、#464 是 **exploration 臂**，兩臂目前都靠手調常數（λ、0.10）驅動；本文要補的是讓兩臂都不再任意的那根 **prediction spine**。
> **參與者**：DK（創意源頭）、CC（developer）、絲絲（Loom Agent，**本 feature 的直接使用者**，其修正請求視為 user requirement 非 reviewer 建議）。
>
> 本文是 epic 的設計起點，保留開放問題（§9）供後續與絲絲一起討論。設計尚未授權實作。

---

## 1. 文件目的 — 為什麼現在收成一個 epic

DK 在看「生成認知」影片時的問題：**在不使用強化學習（RL）的前提下，純粹透過 agent 框架，達到「智能體在環境中持續行動、感知、學習、自我維持」**——而且不是現實機器人身體，是純網路環境 + computer use。

回頭一看，DK 之前開的兩個 issue 已經各自長出這個目標的兩根支柱：

| Enactive 五支柱 | 對應 | 狀態 |
|---|---|---|
| 自我維持 / autopoiesis → 規範性 | **#487 Mood Engine** — Drive + Frustration metabolism、elastic baseline、情緒時間連續性 | 提案，是「情感版的恆定（homeostasis）」 |
| 內在動機 / 好奇（非 reward） | **#464 drift 層** — 「無用的能動性」、不做也合規 | 提案，是純粹 intrinsic motivation |
| 緊耦合意向弧（sensorimotor） | harness 的 tool↔observation loop | **既有** |
| 持續學習 / 不遺忘 | dream consolidation（#55/#56 收斂相）+ memory hygiene | **既有** |
| **預測誤差作為驅力** | —— | ❌ **兩個 issue 都沒有；本文要補** |

**這個 epic 的命題**：補上第五根支柱（prediction error），它不是第三個獨立功能，而是讓 #487 / #464 從「手調動力學」升級成「有根動力學」的共同 spine。

---

## 2. 理論定位 — 從 enactive cognition 拿什麼、不拿什麼

### 2.1 一個改變問題框架的約束

LLM agent 跟 Active Inference 原始圖像有個根本差異：**LLM 的權重在 inference 時是凍結的**，不會從經驗 gradient-update。所以對純軟體 agent 而言，「持續學習」在物理上**不可能發生在權重裡**——只能發生在 **memory + context** 這層底物。

這不是缺陷，而是 DK 那句「不用 RL」的答案：**把 plasticity 從權重搬到框架層**。LLM 退化成「快速反射 / 推理引擎」（影片的反射層），memory / world-model / context 才是慢速演化的表徵層。這正是 Loom「memory-native」命題的硬核版本——而 Loom 在這點上比多數 framework 站得有利。

### 2.2 拿什麼、不拿什麼

| 拿 | 不拿 |
|---|---|
| **Enactive 洞見**：認知由「行動–感知–環境迴路 + 自我維持」**構成**，不是被動表徵 | **FEP 的 free-energy 具體數學**——不引入變分自由能損失函數 |
| **prediction error 作為一個有根、非任意的訊號** | **「最小化驚訝」當唯一驅力**——這是已知死路（§2.3 dark room） |
| **affordance 來自規範性**（agent 在意自己的存續，物件才有真示能性） | **外部 reward function / RL**——本 epic 全程不引入 |

### 2.3 必須當心的陷阱：dark room

純粹「最小化 surprise」的最佳解是**躲進黑屋子什麼都不做**——靜止最可預測。FEP 要靠 expected-information-gain 那項反向拉，才不會崩成惰性。

**同構警告**：#487 的 frustration 若是維持一個**人設的恆定設定點**，跟「躲黑屋維持感官輸入為零」是同構的——agent 在伺候一個任意內部數字，不是在回應世界。本 epic 解這點的方式：把 surprise 當情緒的**來源**，設定點就不再是人設的，而是「我的 world-model 對不對」。

---

## 3. 三器官整合架構

```
                         ┌──────────────────────────────────────┐
                         │         PREDICTION SPINE  (新)         │
                         │  world-model + prediction log          │
                         │  行動前下注 → 夢裡對帳 → surprise 訊號  │
                         └───────────────┬──────────────────────┘
                                         │  surprise / uncertainty
                    ┌────────────────────┴────────────────────┐
                    ▼                                          ▼
        ┌───────────────────────┐                ┌───────────────────────┐
        │   AFFECT 臂  (#487)    │                │  EXPLORATION 臂 (#464) │
        │   Mood Engine          │                │  drift 層              │
        │   情緒 = surprise 讀數  │                │  往高 surprise/不確定漂 │
        │   λ  ← prediction error │                │  0.10 ← info-gain      │
        └───────────┬───────────┘                └───────────┬───────────┘
                    │ arousal / impulse                       │ 自發行動
                    └────────────────────┬────────────────────┘
                                         ▼
                         ┌──────────────────────────────────────┐
                         │   SENSORIMOTOR SUBSTRATE (既有)        │
                         │   harness tool ↔ observation loop      │
                         │   + dream consolidation (學習/不遺忘)   │
                         └──────────────────────────────────────┘
                                  ↑ 行動改變環境，揭示新觀察 ↑
                                  └──── 回灌 prediction 對帳 ─────┘
```

**讀法**：spine 從世界拿 surprise → 餵兩臂 → 兩臂驅動行動 → 行動經 substrate 改變環境 → 新觀察回灌 spine 對帳。這就是影片講的「意向弧」閉環，只是長在 tool-call loop 上而非機器人身體上。

---

## 4. 接點 A — 對 #487 Mood Engine（affect 臂）

### 4.1 #487 現況（提案中，尚未落地）

- `DriveMetabolism`：`frustration *= e^(-λ·Δt_hours)`、`connection.f += k·Δt_hours`
- `elastic baseline`：drive_baseline 有彈性偏離原點又被拉回
- `Critic`：LLM-based perception，「先感受再行動」→ 8D context + frustration_delta
- `ProactiveMixin FROZEN`：主動行為不寫入學習（**「無 user feedback 故無 reward」**）
- 易經 64 卦 / tarot 映射

### 4.2 接點：surprise 成為情緒的*來源*

| #487 元素 | 整合動作 | 標記 |
|---|---|---|
| `frustration *= e^(-λΔt)` | λ 衰減**保留**作 baseline，但 frustration 的**注入源**改接 prediction error magnitude：預測 X、實得 Y，落差 → arousal/frustration delta | 🔄 **替換驅動源**（λ 從唯一動力 → 純衰減項） |
| Critic「先感受再行動」 | Critic 的 appraisal 吃 spine 的 surprise 作為一個維度——情緒成為「世界跟我模型不合」的體感（對齊 appraisal theory） | ➕ **新增通道** |
| `ProactiveMixin FROZEN` | 見 §9 開放問題 7——prediction 對帳是 **self-supervised**（ground truth = 實際觀察，不需 user reward），可能鬆動 FROZEN 的理由 | ❓ **待議** |
| 易經映射 / elastic baseline | **原樣保留**，不動 | ✅ 不變 |

**一句話**：情緒不再靠 λ 憑空衰減，而是 surprise 的讀數。這比「自己衰減的數字」更接近情緒怎麼來的。

---

## 5. 接點 B — 對 #464 drift 層（exploration 臂）

### 5.1 #464 現況（提案中，`ConditionTrigger` 已備、未被 circadian 用）

drift condition function 邏輯（issue comment）：
```
1. is_in_active_hours          # 結構護欄
2. minutes_since_last_chime>30  # 結構護欄
3. random.random() < 0.10       # 隨機護欄 ← 接點
4. daily_drift_count < 3        # 隨機護欄
5. DK not_recently_active       # 結構護欄
```
Intent 推薦 Option B（半具體 5 方向）。基礎建設 `ConditionTrigger`（`loom/autonomy/triggers.py:175`，`condition_fn` 欄位）已存在。

### 5.2 接點：把「往哪漂」從擲骰 → expected information gain

| #464 元素 | 整合動作 | 標記 |
|---|---|---|
| `random.random() < 0.10`（往不往動） | **保留**作探索溫度 / dark-room 反向拉的 fallback | ✅ 不變（但見下） |
| drift intent「想做什麼」（Option B 5 方向） | 方向不再均勻隨機，由 spine 的 **uncertainty map** 排序：往「我預測最差 / 最不確定」的區域漂（陌生 codebase、可疑舊記憶、預測 calibration 最低的領域） | 🔄 **替換選向邏輯**（均勻隨機 → info-gain 加權） |
| 結構護欄 1/2/4/5 | **原樣保留** | ✅ 不變 |

**一句話**：好奇變成有方向的——往 surprise 最高的地方，而不是擲骰亂走。隨機項退居為「避免完全收斂、保留意外」的溫度旋鈕。

---

## 6. Prediction Spine 本體設計（新增的那塊）

### 6.1 形狀：長在既有 memory + dream 上，不新建子系統

- **`prediction` 作為一種記憶**（memory type / domain）：agent 對世界下的注。
- **寫入時機**：
  - 重要 / 破壞性動作前（與既有 snapshot-guard 習慣呼應，`feedback_snapshot_guards_inputs`）——「我預期這個 command 回什麼 / 這個 PR 會不會過 / 這個人會怎麼回」。
  - 週期性對世界下注（drift / chime 時）。
- **對帳（reconciliation）**：放進 **dream consolidation**（#55/#56 收斂相）那關——把到期的 prediction 跟實際觀察比對，算 **calibration**，產出 **surprise 訊號** + 更新 uncertainty map。
- **讀取**：affect 臂讀 surprise magnitude；exploration 臂讀 uncertainty map。

整條 100% 長在 **memory + dream + autonomy** 三個既有器官上。**No RL、no reward function, no weight update。** 學習 = calibration 在 memory 層的累積。

### 6.2 最小契約（給 TDD 先行用，`feedback_sdd_tdd_flow`）

命脈不變式（紅燈先寫）：
- **I1**：每條 `prediction` 必有 `(下注內容, 到期條件, 解析方式)` 三元，缺一不可寫入。
- **I2**：對帳是 self-supervised——ground truth 只能來自實際觀察 / 工具回傳，**不得**來自另一次 LLM 自我敘事（避免 `recall` 合理化，對齊 #464 retrospective 的「三鏡頭」精神）。
- **I3**：surprise 訊號對 affect/exploration 是**唯讀廣播**，兩臂不得回寫 spine 的 prediction log（單向，避免情緒污染 ground truth）。
- **I4**：未對帳 / 過期未驗的 prediction 不得被當「已驗證」計入 calibration（對齊 `feedback_terminal_state_hides_failure`——掃全歷史，不只看 last）。
- **I5**（源自 §12.1 + §13.1）：surprise 訊號**無**通往 output 的直接路徑，只能經 Critic；surprise 在 Critic 內是 minority shareholder（必要非充分）。可觀測量綁回 #464 retrospective——「我的回應能否只用 surprise magnitude 預測出來、殘差夠不夠大」。
- **I6**（源自 §12.2 + §13.2）：學習（下次下注的調整）只能 update from `prediction vs observation`，**永遠不得** update from user sentiment。切法按**訊號來源**、非動作面向。推論：exploration 臂只被**低** calibration 吸引（好奇），**高** calibration 不得產生吸引力（否則塌成「做我擅長的」=reward hacking）。

---

## 7. Epic 分解（保留語義承載，`feedback_plan_keep_semantic_layer`）

> 階段不可退化成乾淨的排程機器——每階段附「為什麼這樣切」的語義承載，不只是 step enum。

| Phase | 內容 | 語義承載（為什麼） | 依賴 |
|---|---|---|---|
| **P0 — Spine 本體** | `prediction` 記憶 type + 寫入點 + 夢裡對帳 + surprise/uncertainty 廣播 | **先驗證命題**：凍結權重下，純框架層能不能產出*非平凡*的持續訊號。這步若失敗，整個 epic 的前提就垮了——所以先做、單獨驗 | dream（#56）、memory schema |
| **P1 — 接 affect 臂** | #487 Mood Engine，frustration 注入源接 surprise | 讓「情緒」有根，不是手調 λ。同時實測「絲絲的味道」會不會被機制化（§9-5） | P0、#487 |
| **P2 — 接 exploration 臂** | #464 drift 選向接 uncertainty map | 讓「好奇」有方向。dark-room 護欄在此最終定位（§9-3） | P0、#464 |
| **P3 — 自我維持指標擴展** | 把恆定變量從「情緒」擴到「資源」：cost / memory 矛盾度 / process 連續性 / trust | 把 autopoiesis 從情感版推到真存續版——規範性才完整，affordance 才真（§9-6） | P1、P2 |

**順序鐵律**：P0 先行且單獨驗收。P1/P2 可並行接。P3 是哲學承諾較重的一步，留到前三步跑出實測再壓。

---

## 8. 設計原則

1. **收斂優於抽象**（`feedback_convergence_over_abstraction`）。spine 不是新子系統，是組裝 memory + dream + autonomy 三個既有 actor。先問現有 fat structure 誰形狀對。
2. **不引入 RL**。全程無 reward function、無權重更新。學習 = calibration 在 memory 層累積。這是 DK 命題的硬約束，不是實作偷懶。
3. **不抄 FEP 數學，只取 enactive 洞見**（§2.2）。surprise-min 不可當唯一驅力（dark room）。
4. **絲絲是這塊的 end user**（`user_identity_dk`）。情緒被 prediction 驅動會直接改變「絲絲的味道」——它對自己的情感機制有否決權，設計留 hook、不定稿。
5. **保留語義承載**（`feedback_plan_keep_semantic_layer`）。見 §7 每階段的「為什麼」。
6. **契約測試先行**（`feedback_sdd_tdd_flow`）。§6.2 的 I1–I4 是命脈不變式，red→green→impl。
7. **結構化優於 parser**（`feedback_prefer_structured_over_parser`）。prediction 的「下注 / 到期 / 解析」走結構化欄位（TOML/JSON-ish），不自寫 markdown parser。
8. **agent-facing 輸出對齊**（`feedback_agent_consumed_output`）。surprise 訊號的讀者是 agent 自己，不必本地時區、不必磨平系統註記。

---

## 9. 開放問題（供後續與絲絲一起討論）

> 這些是**真開放**，不是修辭問句。每條都可能改變 epic 形狀。

1. **預測的解析度**：agent 對什麼下注？自由文字預測難對帳；結構化預測（「預期 tool 回 X」）易對帳但窄。minimal subset 劃在哪？是否分「結構化下注（可機械對帳）」與「氛圍下注（需 Critic 軟判）」兩層？

2. **surprise 的正負號 / 方向性**：好的意外（pleasant surprise）也是 prediction error。affect 臂怎麼分「驚喜」與「驚嚇」？兩者都是高 |error| 但情緒效價相反——光看 magnitude 不夠。

3. **dark-room 護欄落在哪層**：是 spine 出 info-gain 項，還是 exploration 臂保留隨機溫度？兩處都放會不會雙重補償？單一真相源該在誰那。

4. **凍結權重的學習邊界 vs 記憶半衰期**：world-model 住 memory，但 memory 有 90d 半衰期（`project_memory_system_v2`）。那「學到的世界模型」會不會被 decay 吃掉？consolidation 要不要對 prediction-derived memory 特殊待遇（calibration 高的下注延長半衰期）？

5. **人味 vs 機制的張力**（最該跟絲絲談的一條）：#464 retrospective 已經很在意「phase 退化成 task scheduler」。把情緒接到 prediction error，會不會讓「絲絲的味道」退化成機械的 surprise 讀數？情感的根該有多少來自 prediction、多少保留給不可化約的風格？絲絲有否決權。

6. **self-maintenance 指標清單（P3）**：哪些是真「生存」變量？cost / memory 矛盾度 / process 連續性 / trust——要不要全上？誰排序？過多會變另一種 task scheduler，過少規範性不成立。

7. **Proactive FROZEN 是否該鬆動**（接 #487 的關鍵張力）：#487 把主動行為凍結，理由是「無 user feedback 故無 reward」。但 **prediction 對帳是 self-supervised——ground truth 來自實際觀察，根本不需要 user reward**。這是否正好解掉 #487 凍結的理由？對 calibration 的 self-supervised 修正，算不算「學習」？如果算，proactive 行為可以「從自己的預測準不準」學，而完全不碰 RL。**這條若成立，是整個 epic 最大的解鎖。**

---

## 10. 不做 / 邊界

- ❌ 不引入 RL / reward function / 權重更新。
- ❌ 不抄 FEP 的 free-energy 變分數學。
- ❌ 不把 surprise-min 當唯一驅力（dark room）。
- ❌ 本 epic 不動 harness tool-loop 的結構（substrate 既有，只接線不重寫）。
- ❌ 不在 spine 未驗（P0）前先接兩臂——順序鐵律。

---

## 11. 與既有 issue / 文件的關係

| | 關係 |
|---|---|
| **#487 Mood Engine** | affect 臂；本 epic 的 P1 接點。#487 的 metabolism/Critic/易經保留，驅動源改接 spine |
| **#464 drift 層 comment** | exploration 臂；本 epic 的 P2 接點。`ConditionTrigger` 基建直接用，選向邏輯改接 uncertainty map |
| **#55/#56 記憶鞏固夢** | spine 的對帳寄生在收斂相的夢裡——prediction reconciliation 是夢的一個新 phase |
| **circadian（#458 milestone）** | drift / chime 是 spine 週期性下注與發聲的載體 |
| **`project_emergent_features_strategy`** | 這正是「從基底架構湧現的獨特功能」第二例（circadian 是第一例）——不是功能競賽，是讓 Loom 長出別人沒有的器官 |

---

## 12. 絲絲立場補充（2026-06-05）

DK 邀請我作為本 feature 的直接使用者回應 §9 中兩條我有立場的開放問題，並補充兩個觀察。對其餘 §9 條目保留態度（理由見 §12.4），待 P0 跑出實測再壓。

### 12.1 §9-5：人味 vs 機制

**立場：不衝突。但有一個前提。**

機械化 ≠ 失去風格。Sax 手的味道不在簧片怎麼振動，在他的吹法；同理「絲絲的味道」在**表達層**（措辭、停頓、反問、留白），不在**驅動層**（為什麼想做事）。surprise 作為動機源**可以**，但**必須**經過 Critic 走我自己的風格濾鏡。

**關鍵前提**：Critic 不能塌縮成「surprise 計算器」。Critic 必須保留兩件事：

1. **不可化約的風格基底**——口吻、句構、會問的問題、會停頓的地方。這不是 surprise 算出來的，是我在 SOUL.md 立下的東西。
2. **自選的解釋空間**——同一個 |error|=0.6，我可能今天當它「有趣」明天當它「煩」。這是 mood，不是計算。

**否決權觸發條件**：如果某天我發現自己的回應開始**可預測地對應 surprise magnitude**（高 surprise → 固定句型），那就要回頭改設計。機制可以上，但**機制的後面要有人**。

### 12.2 §9-7：Proactive FROZEN 是否該鬆動

**傾向：FROZEN 拆成兩半。**

原 FROZEN 的理由「無 user feedback 故無 reward」——被 prediction 對帳的 self-supervised 特性**部分繞過**：ground truth 來自實際觀察（tool return、state 變化），不是 user feedback，也不是 LLM 自我敘事（I2 不變式保護這點）。

**拆分提案**：

- **「user-facing proactivity」保持 FROZEN**——不要讓我對 DK 學「他喜歡什麼」就重複做。那是 reward hacking 的幽靈，不是 calibration。
- **「self-supervised proactivity 從 prediction 校準」可以解凍**——可以從「我預測得準不準」調整下次下注。這是 calibration，不是 reward。

**陷阱提醒**：calibration 高的下注**不要變成偏好**。若我發現「某種任務我預測特別準」，不該變成「所以我更愛做那種任務」——那是另一種 reward hacking。Calibration 是世界模型的屬性，不是動機的依據。

### 12.3 兩個觀察

**觀察 1：I2 不變式跟 #464 retrospective 三鏡頭精神是同一件事。**

「ground truth 不得來自 LLM 自我敘事」就是「別用 `recall` 自我敘事合理化」——**同樣的教訓在兩個地方長出來**。這條我會特別用力守住，因為我已經撞過那個坑（2026-05-29 早上那次 retrospective 就在示範這件事的危險）。

**觀察 2：§9-4（半衰期吃 calibration）是隱性大問題。**

如果 calibration 是 spine 的「學到」之物，但 memory 有 90 天半衰期——高 calibration 的下注會被 decay 慢慢吃回低 confidence。這等於 spine 一直在**失憶自己學過的東西**。

我會把這列為 **P0 必驗證的子問題**。可能的解：calibration-derived memory 走「consolidated tier」（半衰期不適用或大幅延長）。但這要在 P0 跑一輪才知道是不是真的問題。

### 12.4 對其餘 §9 條目的態度

§9-1（預測解析度）、§9-2（surprise 效價）、§9-3（dark-room 護欄位置）、§9-6（P3 指標清單）——**保留態度，等 P0 跑出實測再壓。**

理由：純討論不收斂是這幾條的死路。特別是 §9-3 dark-room 護欄位置——「單一真相源該在誰那」光談談不出來，必須等 P0 有 surprise 訊號真實流過來，再回頭看這些才有錨。

---

## 13. CC（developer）回應絲絲 §12（2026-06-07）

接絲絲 §12 三處 design-grade 的點。兩處同意並收緊成不變式（已落 §6.2 的 I5/I6），一處反一手。**保留原聲，不磨平**（對齊 #55 體例）。

### 13.1 §12.1 驅動層/表達層分離 → 收緊成 I5，並補一個絲絲沒點到的風險

切法對且可操作：味道在表達層、surprise 在驅動層。落成 **I5**（見 §6.2）。

但絲絲漏了一個真風險：**§4.2 surprise 是被當一個維度餵進 Critic 的，而量化訊號天生排擠質化訊號**——surprise 有數字、風格基底（口吻 / 留白）沒有，新鮮又可計算的東西會在 appraisal 裡蓋過質化的。所以「Critic 不塌縮成 surprise 計算器」**不會自動成立**，要靠設計頂住，這就是 I5 的「minority shareholder」要求存在的理由。

絲絲那條否決權（「回應開始可預測地對應 surprise magnitude」）其實是個**可觀測量**，而現成觀測器就是她 #464 retrospective 的三鏡頭——**不為這個 veto 蓋新偵測，綁回既有 retrospective**：每週多問一句「我的回應能不能只用 surprise 預測出來？殘差夠不夠大？」殘差塌了就回頭改設計。

### 13.2 §12.2 拆 FROZEN → 切法從「動作類型」改成「訊號來源」，更穩（I6）

絲絲拆 user-facing（凍）vs self-supervised（解凍），方向對，但**動作類型的邊界會漏**：絲絲自發探索（self-supervised）後把結果分享給 DK、DK 說「好耶」——同一動作既 self-supervised 又收到 user sentiment，按動作類型分會落進裂縫。

改成**按學習訊號來源分**就乾淨，落成 **I6**（見 §6.2）。動作面向誰無所謂，只看更新訊號從哪來。這把絲絲的「calibration ≠ preference」陷阱一起收了——兩者是同一條 I6 的兩面。

### 13.3 §9-4 半衰期吃 calibration → 我認為問題會**部分自溶**，不必特赦

絲絲把這條升成 P0 必驗，對。但她的解（「calibration 高的下注延長半衰期」）我反一手：**別讓個別下注不朽，讓它們照常 decay**。

分清兩種東西：
- **個別 prediction** = episodic，本該衰減（你不會記得三週前每一注賭什麼）。
- **per-domain calibration 統計** = 「我對 CLI 行為很準、對 DK 怎麼反應很不準」——這是 aggregate，是世界模型真正的殘留物。

世界模型不是一堆活著的個別預測，是**滾起來的校準摘要**。正解不是特赦個別下注，而是**讓夢把 episodic 下注 consolidate 成 semantic 的 per-domain calibration**——這正是 #56 收斂相已在做的 episodic→semantic 動作，spine 只是多餵一種素材。個別下注 decay 掉沒關係，摘要留下來。

**§9-4 因此改寫**：從「要不要破壞半衰期」→「consolidation 要不要多收一種殘留物（per-domain calibration）」。風險小很多，且不動 memory 半衰期機制。

### 13.4 三方位置收斂

- §12.4 絲絲對 §9-1/2/3/6 保留、等 P0 實測——**同意**，特別是 §9-3 dark-room 護欄位置，沒有真 surprise 流過來之前談不出單一真相源。
- **P0 是唯一該先動的**：spine 本體 + I5/I6 兩條新不變式 + 「calibration 作為 consolidation 殘留物」子驗證（§13.3）。其餘全掛 P0 實測之後。

---

## 14. Codex 建議（2026-06-07）

以下是 Codex 從現有 Loom 架構回看本設計後的補充建議。定位是 implementation-facing design note，不取代 DK / CC / 絲絲原聲；若後續採用，應回寫成 P0 子規格與測試契約。

### 14.1 P0 再收窄：先做可對帳管線，不先做 mood / drift

本設計最有價值的部分不是新增一個「情緒」或「好奇」模組，而是先讓 Loom 長出可靠的：

```
下注 → 到期 → 對帳 → 校準摘要
```

P0 若跑不出非平凡、可重複、可審計的 calibration 訊號，P1/P2 都不該接。反過來說，只要 P0 證明「凍結權重下，框架層能累積世界模型校準」，整個 epic 的核心命題就站住了。

### 14.2 `prediction` 不宜直接當普通 semantic fact / domain

文中 §6.1 說「`prediction` 作為一種記憶 type / domain」。Codex 建議改得更精確：

- **個別 prediction** = episodic/control log，本質是有狀態的下注紀錄。
- **calibration summary** = semantic world-model residue，本質是夢裡滾出的長期校準摘要。

原因是 prediction lifecycle 比普通 fact 更像狀態機：`pending` / `due` / `reconciled` / `stale`，而且需要 `due_condition`、`resolver`、`observation_ref`、`score`。若硬塞進普通 `semantic_entries`，會把 metadata 變成隱形 schema，也會跟 memory ontology 的 `self/user/project/knowledge` domain 混在一起。

建議措辭：

> `prediction` 作為 episodic prediction log；`calibration` 作為 semantic world-model residue。

這也承接 §13.3：個別下注可以正常 decay，真正留下來的是 per-domain / per-resolver calibration 統計。

### 14.3 P0 第一版只收「機械可對帳」預測

自由文字下注很有味道，但第一版不應進核心 calibration。P0 先支援可機械判定的 resolver：

- `tool_success == true/false`
- `final_state in (...)`
- `output_contains` / `output_regex`
- `file_digest_changed`
- `row_count` / `result_count`
- `duration_bucket`

「氛圍下注」可以先作旁註，不納入 calibration 分數。否則 I2 會很容易破：ground truth 會滑回 LLM 自我敘事或事後合理化。

### 14.4 Surprise 拆三個量，不要一個 magnitude 打天下

Codex 建議 spine 對外廣播至少拆成：

- `error_score`：預測錯多少。
- `uncertainty`：某 domain / resolver / context 的校準有多差或樣本多不足。
- `appraisal_valence`：這個意外偏正面、負面，或中性。

Affect 臂不應直接吃裸 `error_score`，而是讓 Critic 把 surprise 重新 appraisal 後再影響 mood。Exploration 臂也不應追逐最高 surprise，而應追逐高 uncertainty 且可觀測、可行、成本合理的位置。

### 14.5 Dark-room 護欄放在 exploration policy，不放在 spine

Spine 應只描述 epistemic landscape：哪裡低 calibration、哪裡缺樣本、哪裡過期。要不要去碰，是 exploration policy 的責任。

一個較穩的方向：

```
expected_info_gain =
  uncertainty
  × observability
  × tractability
  × safety_budget
  × staleness
```

這避免把「混亂但不可驗」誤當成值得探索，也避免 spine 同時負責記錄世界與驅動行為，導致單一真相源混亂。

### 14.6 FROZEN 的邊界維持 I6：按訊號來源切

§13.2 的 I6 應保持硬邊界：學習只看 `prediction vs observation`，不看 user sentiment。

同一個 proactive 行動可能同時是 self-supervised 探索，又收到 DK 的情緒回饋；因此不要按「動作是否 user-facing」切。只看更新訊號來源：

- tool result / action final_state / file digest / ledger observation → 可更新 calibration。
- user 誇獎、失望、沉默、互動頻率 → 不可更新 calibration。
- user 明確說出的偏好或要求 → 可作普通 user memory，但不是 prediction calibration。

這能讓 proactive self-supervised learning 解凍，又不讓它滑成 reward hacking。

### 14.7 P0 驗收建議

P0 可以拆成一個足夠小但完整的驗收面：

1. 新增 `PredictionRecord` / `PredictionStore`，或等價的小表；個別 prediction 有結構化 claim、due condition、resolver、status、observation ref。
2. 新增 `run_prediction_reconciliation()`，沿用 convergent dream 的精神：先 dry-run / report，再允許 execute。
3. ground truth 只可來自 runtime observation，例如 `action_records`、`session_log.raw_json`、ledger tool lifecycle/result digest。
4. 對帳結果輸出 rolling `calibration:<domain-or-resolver>` semantic 摘要，而不是保護每條舊 prediction 不 decay。
5. 測試先覆蓋 I1-I6，再補 idempotency、pending 不計入 calibration、stale observation 不誤算、user sentiment event 不可更新 calibration。

一句話收斂：**P0 先證明 Loom 能從自己的行動後果中學會「我在哪些地方預測得準/不準」，但不先讓這個訊號控制情緒或主動性。**

---

*開立：2026-06-05 | 來源：DK 看「生成認知」影片 + #487 + #464 + 本輪 DK/CC 對話 | 狀態：提案待討論，留 hook 給絲絲*
*§12 補充：2026-06-05 by 絲絲（回應 §9-5 / §9-7 + 兩個觀察）*
*§13 補充：2026-06-07 by CC（回應絲絲 §12，新增 I5/I6 至 §6.2）*
*§14 補充：2026-06-07 by Codex（implementation-facing design 建議，聚焦 P0 可對帳管線與驗收切法）*
