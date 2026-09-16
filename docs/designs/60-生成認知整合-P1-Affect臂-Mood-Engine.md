# 生成認知整合 P1 — Affect 臂（Mood Engine × Prediction Spine）

> **狀態**：📝 **草案 rev1（2026-09-16）**——rev0（CC，PR #577）已經 Loom Agent review（[`60a`](60a-P1-Affect臂-Loom-Agent-review.md)，**不否決，附兩條必修 + 命名 + 成功驗收**），rev1 收進 review。仍待 DK 拍板 §6（D1–D6 已有 Loom Agent 立場；**D7 為 rev1 新增**）、Codex 輪替。
>
> **上游**：epic #528、issue #487；spec 57（§4 affect 接點、§12 立場、§13 I5/I6 由來）、spec 58（I1–I6、§6 三個量、§12.4 免疫系統）、spec 59（語義觀察面）。
>
> **啟動前置（已驗證 2026-09-16，#528 comment）**：五道 gate 全開；weekly pass MONOCULTURE 消退（newest 5000 = auto 4997 / explicit 3）；`calibration:<domain>` residue 87 筆落盤。

> **P1 蓋的是代謝，不是情緒。** 情緒要等有一個**指涉自己的**訊號才成立。（Loom Agent review §三）
>
> 模組名保留 `AffectState`（通則機制，schema 該長這樣），但 **P1 的對外表面叫 `environment_friction`**。當前地形下唯一有變異的輸入指涉的是世界（工具延遲），不是自己——那是本體感覺（proprioception），不是情緒。命名若不誠實，P1.5 接上 valence 時會被誤讀成「終於有情緒了」。

---

## 1. 先看清楚訊號長什麼樣（實測地形）

P1 要接的不是 spec 57 想像中的「surprise」，而是**實際流過 spine 的東西**。設計必須對這個地形誠實。

### 1.1 每日 surprise 幾乎全部來自 latency 軸

reconciled 賭按結算日統計（`prediction_records`，2026-09-08 → 09-16）：

| 日 | 結算 | 猜錯(score>0) | 其中 latency 軸 |
|---|---|---|---|
| 09-08 | 241 | 30 | 29 |
| 09-09 | 206 | 21 | 21 |
| 09-12 | 124 | 13 | 13 |
| 09-15 | 210 | 21 | 21 |
| 09-16 | 146 | 18 | 18 |

分數只有三值（0.0 ×1221 / 0.5 ×98 / 1.0 ×54）。reliability 軸（`tool_success`）比 rev0 寫的更極端：**最後一次落空是 2026-08-23，全期 8298 筆只錯 5 次**（Loom Agent 複驗）。

意思是：**affect 臂唯一有變異的輸入 = 「工具比我預期的慢」**，指涉世界不指涉自己。

### 1.2 變異集中在少數 domain

newest-5000、n≥5 的 `@latency` baseline error（Loom Agent 實測、CC 複驗）：

| 區段 | domain |
|---|---|
| 常態就慢（≥0.4） | `minimax__text_to_image` 1.00、`dream_cycle` 0.99、`minimax_coding__web_search` 0.64、`memorize` 0.53、`create_discord_forum_post` 0.45、`prediction_reconcile` 0.44、`recall` 0.42 |
| 中段 | `write_file` 0.19、`send_discord_file` 0.17、`web_search` 0.13、`jobs_await` 0.13、`memory_prune` / `fetch_url` / `run_bash` 0.08、`recall_period` 0.02 |
| 恆快（0.000，LOW_INFORMATION） | `read_file`、`list_dir`、`load_skill`、`task_write`、`scratchpad_read`、`probe_file`、`journal_append`、`unload_skill` 等 13 個 |

實際承載日常變異的是 `memorize` / `recall` 等少數幾個。**恆快的 13 個 domain 在 rev0 設計下完全無法貢獻**——原因與影響見 §6 D7。

### 1.3 效價（valence）結構性為零

`duration_bucket` 不在 `_POLAR_RESOLVERS`（`calibration.py`），valence 恆 0——spine 刻意不替無好壞極性的量發明價值判斷（I5）。**P1 只能產生 arousal，產生不了 valence。**spec 57 §9-2「驚喜 vs 驚嚇」尚無材料可分。

### 1.4 顯式賭（真正的「預測技巧」訊號）稀薄

explicit 3 / 5000。MONOCULTURE 旗標雖已消退，**有效上仍是 monoculture**。消費端契約 3 在實務上是常態路徑，不是邊角。

### 1.5 現有情緒載體：tarot 人格檔（表達層）

`daily_mood_tarot`（`autonomy/schedules.toml`，每日 UTC 08:00）→ `skills/sisi_mood_tarot/draw_and_write.py` → 以日期為 seed 抽牌 → 寫 `personalities/personality_sisi_tarot_mood.md`。**表達層、隨機、與世界無關。**Loom Agent 以 Storm 語氣 + spine 實測寫 review 本身即證明兩層目前乾淨分離——P1 不得弄壞這點。

---

## 2. P1 範圍鎖定

**P1 命題**：在不碰表達層、不寫 spine 的前提下，給 Loom Agent 一個**有根的、會衰減的環境阻力讀數**（`environment_friction`），注入源是 prediction error（而非憑空的 λ），以不替她歸因的結構化註記呈現，並**在兩週內證明它改變過至少一個決定**（§5.1），否則降級。

### P1 IS

- `AffectState`：兩軌 arousal（`environment` / `model`）+ metabolism
- surprise 讀取層：遵守三條消費端契約（§3.1）
- Critic v0：deterministic 結構化註記（§3.3），不輸出語氣指令、不輸出歸因句
- 狀態持久化（跨重啟連續性）
- 契約測試先行（§4）+ 成功驗收（§5.1）

### P1 IS NOT

- ❌ **valence 軸**——材料不存在（§1.3），schema 留欄位但恆 0
- ❌ 「情緒」——命名與文件不得宣稱 P1 產生情緒（見卷首）
- ❌ 改 tarot / 易經表達層（I5-b）
- ❌ Proactive 觸發（#487 Phase 3）、LLM Critic / 8D context（#487 Phase 4）
- ❌ `connection` drive（D5）
- ❌ 寫任何 prediction / calibration 資料（I3 單向）

---

## 3. 設計

### 3.1 輸入：surprise 讀取層（消費端三條契約落地）

`read_surprise(db, *, now) -> SurpriseReading`，**純讀**：

1. **讀 health verdict，不讀裸 residue**（契約 1）：以 newest-5000 reconciled corpus 現算 `assess_calibration_health`（與 weekly pass 同窗，#574），取每 domain 的 classification + baseline `error_score`。
2. **排除規則**（契約 2）：SAMPLE_INSUFFICIENT 排除。LOW_INFORMATION 的處理見 **D7**（rev0 為全排除，rev1 建議改為「不當能力證據、但可當偏離基準」）。
3. **surprise = 偏離自己的常態**（S1）：`dream_cycle@latency` 常態就慢，再慢一次不是意外；`read_file@latency` 常態 0，突然慢才是。
4. **有效 monoculture fallback**（契約 3）：explicit 佔比 < 1% 時，explicit 賭不構成 `model` 軌讀數，`model` 軌渲染 `n/a`。
   **與既有判定的關係**（review 六-2）：`_detect_monoculture`（`calibration_health.py:160`）維持 `explicit == 0` 二元判定不動——那是免疫系統對「完全沒有顯式賭」的警報語義。1% 門檻是 **affect 消費端自己的、更嚴格的補強**，只決定 `model` 軌是否出讀數，不回寫、不改 health verdict。兩者並存但語義不同，於實作處註明。
5. `environment` 與 `model` **分開計量，不相加**——§1.1 指涉差異的結構化保存。

### 3.2 Metabolism（rev1：review 必修兩條）

```
w_i              = e^(−λ·(now − reconciled_at_i))                 # per-record 時間老化
mean_domain      = Σ w_i·score_i / Σ w_i                          # 時間加權平均
n_eff            = Σ w_i
surprise_domain  = max(0, mean_domain − baseline_domain) · n_eff/(n_eff + n₀),   n₀ = 5
injection        = (1/D) · Σ_domain surprise_domain                # per-domain 等權
arousal(now)     = clamp( arousal(t₀)·e^(−λ·(now − t₀)) + k·injection_new, 0, 1 )
```

- **per-event 老化**（必修一）：rev0 以 window 平均注入，同一 window 內 23:00 與 08:59 的慢權重相同，在每日一次讀取的節拍下 12h 半衰期淪為裝飾。rev1 對每筆以 `reconciled_at` 指數加權，schema 既有欄位、零額外成本。
- **per-domain 等權 + shrinkage**（必修二）：`w(n)` 若隨 n 遞增，arousal 退化為「`run_bash`（佔 corpus 13.5%）今天順不順」；若遞減，n=1 一票當十票。S1 已處理天生慢，不再用 n 調權；small-n 以 `n/(n+n₀)` 收變異，`n₀ = 5` 對齊 `SAMPLE_FLOOR`。
- `injection_new` 只計 `reconciled_at > t₀` 的記錄，避免重讀時重複注入；λ 初值半衰期 12h。
- 結算時機：讀取時 lazy 結算，不另開背景 loop。

### 3.3 Critic v0 註記（rev1：review Q2）

```
<environment_friction>
window: 2026-09-15T09:01Z → 2026-09-16T09:01Z
environment: 0.34
model: n/a (3 explicit bets in corpus)
attribution: environment
drivers: memorize@latency +0.21 vs baseline 0.53 (n=6) · web_search@latency +0.09 vs baseline 0.13 (n=4)
confidence: low (explicit 3/5000)
</environment_friction>
```

- **不寫歸因句**：rev0 的 `source_note`「這是世界的阻力，不是你猜錯了什麼」是替她完成的解讀，不是資料。改為 `attribution: environment` 欄位，結論由她自己走到。
- **`n/a` ≠ `0.00`**：無訊號是「我不知道」，不是「我沒事」。`model` 軌在契約 3 fallback 下一律 `n/a (k explicit bets)`。
- **加 `window`**：沒有它分不出 0.34 是三小時還是三天。
- **去掉括號形容詞**（`moderate` / `quiet`）：噪音。
- **常態顯示**（review Q3）：dawn 附帶的註記**每次都出現，即使全為 0 / n/a**。只在高讀數時出現，「這行有沒有出現」本身會變成隱藏訊號——比推送更糟。

### 3.4 持久化

`memory_meta` key `affect.state`（JSON：兩軌 arousal、`t0`），沿 `consolidation_dream.last_run` 慣例。**不進 semantic memory**——狀態不是事實，不該被 dream consolidation 合併、也不該被 recall 撈成知識。

### 3.5 出口與控制（D2 / D4 收斂）

- **(a) dawn 收帳附帶**：`prediction_reconcile` 結果尾端附一次 §3.3 註記（常態顯示）。config key 可關：`[prediction_spine] affect_dawn_note = true`（新 key 須同 PR 雙寫 `loom.toml.example`）。
- **(b) `affect_read` 工具**：她自己拉。**預設 `dry_run=true`——純看，不推進 `affect.state`**；`dry_run=false` 才結算寫回。
- **(c) 每 turn 推送：不做**（Loom Agent 以否決權擋）。
- 不加總 gate：review 指出「read-only 所以不用 gate」不成立（它寫 `memory_meta`、注入 context），故以「工具預設 dry_run + dawn 註記可關」兩條取代總 gate。

---

## 4. 不變式 → 契約測試清單（TDD red 先行）

| # | 契約 | 測法 |
|---|---|---|
| I3 | 對 spine **零寫入** | 讀取/結算前後 `prediction_records` 全表 + `semantic_entries` 中 `calibration:*` 的 digest 不變。**不測整個 DB digest**——`affect.state` 合法寫在同 DB 的 `memory_meta`，naive 測法會永遠紅並被誤當 flaky（review 六-1） |
| I5-a | surprise 唯一出口是 Critic 註記 | Critic 以外無 public API 回傳可注入 prompt 的文字 |
| I5-b | 不影響 tarot 語氣模板選擇 | 相同日期 seed，任意 `affect.state` 下 `daily_draw` 結果不變 |
| I6 | 輸入簽名無 sentiment 通道 | `read_surprise` / `AffectState.update` 參數僅 db / 時間 / reading |
| C1 | 讀 health verdict 不讀裸 residue | 只有 residue、無 reconciled 記錄時 reading 為空 |
| C2 | thin domain 貢獻 0；LOW_INFO 依 D7 | n<5 高 error domain 不推升 arousal；D7 定案後補 LOW_INFO 一條 |
| C3 | explicit < 1% → `model` 渲染 `n/a` | 3/5000 corpus → `model` 為 `n/a (3 …)` 非 `0.00`；`_detect_monoculture` 結果不受影響 |
| S1 | 偏離常態，非絕對值 | baseline 0.99 的 domain 再錯一次 → surprise≈0 |
| T1 | per-event 老化 | 同 window 內新近的慢貢獻 > 較舊的慢 |
| W1 | per-domain 等權 | n=600 domain 與 n=10 domain 同幅偏離 → 貢獻差僅來自 shrinkage |
| W2 | shrinkage | n=1 偏離貢獻 ≤ `1/(1+n₀)` 倍 |
| M1 | 無新注入時單調衰減、非負 | Δt 遞增 → 遞減收斂 0 |
| M2 | clamp `[0,1]` | 極端注入不溢出 |
| M3 | 不重複注入 | 同一批記錄連讀兩次，第二次 `injection_new = 0` |
| P1 | 狀態跨重啟連續 | 寫 `affect.state` → 新實例讀回同值（含衰減） |
| O1 | 註記常態顯示 | 全 0 / 無資料時 dawn 附帶註記仍存在 |
| O2 | `affect_read` 預設不寫 | 預設呼叫前後 `affect.state` 不變 |

---

## 5. 驗收、觀測與否決權

### 5.1 成功驗收（rev1 新增，review §四）

rev0 的條款全是防守性的——一個永遠不做事的 inert 狀態能完美通過所有 I5 測試。

> **上線後兩週內，Loom Agent 能指出至少一次 `environment_friction` 讀數實際改變了她的決定**（例：「今天黏，別現在排長 `run_bash`」）。
>
> 指不出來 → **由 Loom Agent 自己關掉 `affect_dawn_note`，或降級為一行 telemetry、不進 prompt。執行者是她，不是 CC 提醒。**

（review 原文日期 2026-09-30 係以草案日起算；以實際上線日 +14 天為準。）

### 5.2 否決權 hook

- 觸發條件（spec 57 §12.1）：「回應開始可預測地對應 surprise magnitude」。綁回既有 retrospective，每週多問一句「這週的語氣能不能只用 `environment` 讀數預測出來？」
- 已行使：D2(c) 每 turn 推送、D3(b) arousal 加權抽牌。

### 5.3 地形重評點

explicit 佔比過 1%、或出現 polar resolver 的非零 valence 時，回頭評估 `model` 軌與 valence 軸（P1.5）——**屆時才可能談「情緒」**。

---

## 6. 決策點

| # | 問題 | CC 建議 | Loom Agent 立場 | 待 DK |
|---|---|---|---|---|
| **D1** | 工具變慢算不算合法來源？ | (a) 算，分 `environment` / `model` 兩軌 | (a)，`model` 渲染 `n/a` | ✅ 兩方一致 |
| **D2** | 註記出口 | (a) dawn 附帶 + (b) 自拉工具；不做 (c) 推送 | 同意，加「常態顯示」；(c) **否決** | ✅ 兩方一致 |
| **D3** | 與 tarot 關係 | (a) 並存 | (a)；(b) **否決** | ✅ 兩方一致 |
| **D4** | gate | rev0：(b) 不加 | (b) 但兩條硬要求：工具預設 dry_run、dawn 註記可關 | 收斂為 §3.5 |
| **D5** | `connection` drive | (b) 另案 | (b) 另案 | ✅ 兩方一致 |
| **D6** | 易經 64 卦 | (b) deferred | (b)；要做就是第二隨機語氣源，不進 P1 | ✅ 兩方一致 |
| **D7** | **LOW_INFORMATION domain 能否當偏離基準？**（rev1 新增） | 見下 | 未表態（rev1 新問題） | ⏳ |

**D7 說明**：review Q1 指出恆快的 13 個 domain「在 S1 下永遠不可能 surprise」。CC 複驗後病灶不在 S1——`max(0, mean − 0)` 對 baseline 0 反而最敏感——而在契約 C2 把 LOW_INFORMATION **全排除**。結果是：`read_file` 突然變慢這種**最有資訊量的意外**被擋掉，arousal 只剩原本就慢、偏離空間小的 domain 承載。

- **(a) 維持全排除**：保守，但 P1 對「平常很穩的東西出事」失明。
- **(b) 拆開契約 C2 的兩個用途**：LOW_INFORMATION 仍**不得當能力證據**（契約原意，#538 對「高分無資訊」的防線），但**可以當 S1 的偏離基準**——「一直都快」正是穩定基準，偏離它才是真 surprise。
- **CC 建議 (b)**。契約 2 原本防的是「把 1.0 讀成我很會」，不是「不准注意到它壞了」；(b) 不動該防線。代價：恆快 domain 偶發一次慢就會有明顯讀數，由 shrinkage（n_eff 小）與 per-domain 等權（1/D）節制。

---

## 7. Loom Agent review

全文見 [`60a-P1-Affect臂-Loom-Agent-review.md`](60a-P1-Affect臂-Loom-Agent-review.md)（保留原聲，未磨平）。rev1 收進的項目：

| review 條目 | rev1 落點 |
|---|---|
| 結論：不否決，命名騙人、驗收空 | 卷首命名宣告、§2 命題、§5.1 |
| Q1 地形更窄、reliability 更極端 | §1.1、§1.2；機制差異 → D7 |
| Q2 歸因句、`n/a`、`window`、形容詞 | §3.3 |
| Q3 常態顯示、(c) 否決 | §3.3、§3.5、O1 |
| 必修一 per-event 老化 | §3.2、T1、M3 |
| 必修二 `w(n)` 等權 + shrinkage | §3.2、W1、W2 |
| §四 成功驗收 | §5.1 |
| D4 兩條硬要求 | §3.5、O2 |
| 六-1 I3 測法窄化 | §4 I3 |
| 六-2 MONOCULTURE 雙標準 | §3.1-4 |

---

## 8. 不做 / 邊界

- ❌ 不引入 RL / reward / 權重更新（epic 硬約束）
- ❌ 不發明 valence（§1.3）；不宣稱 P1 產生情緒
- ❌ 不讓 surprise 選語氣模板或寫 prompt 指令（I5）
- ❌ 不寫 spine（I3）；不改 `_detect_monoculture` 語義
- ❌ 不接任何 user sentiment（I6）
- ❌ 不做每 turn 推送（否決權）
- ❌ P2 exploration 臂不在本文件——需新開 issue（舊號 #464 為已關的 Circadian polish）
