# 生成認知整合 P1 — Affect 臂（Mood Engine × Prediction Spine）

> **狀態**：📝 **草案（2026-09-16，CC）**——待 DK 拍板 §6 決策點、Loom Agent review（**對自身情感機制有否決權**，spec 57 §8-4）、Codex 輪替。**本文件不定稿**，§7 留給 Loom Agent 原聲。
>
> **上游**：epic #528、issue #487；spec 57（§4 affect 接點、§12 立場、§13 I5/I6 由來）、spec 58（I1–I6、§6 三個量、§12.4 免疫系統）、spec 59（語義觀察面）。
>
> **啟動前置（已驗證 2026-09-16，#528 comment）**：五道 gate 全開；weekly pass MONOCULTURE 消退（newest 5000 = auto 4997 / explicit 3）；`calibration:<domain>` residue 87 筆落盤。

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

**九天內 reliability 軸（`tool_success`）只錯 1 次，其餘全是 `duration_bucket expect=fast` 落空。**分數只有三值（0.0 ×1221 / 0.5 ×98 / 1.0 ×54）。

意思是：**今天 affect 臂唯一有變異的輸入 = 「工具比我預期的慢」**。這不是「我對世界的模型錯了」，是「世界今天很黏」。兩者都是真的體感，但語義不同，Critic 必須標明來源（§3.3）。

### 1.2 效價（valence）結構性為零

`duration_bucket` 不在 `_POLAR_RESOLVERS`（`calibration.py`），valence 恆 0——這是 spine 刻意不替無好壞極性的量發明價值判斷（I5）。**所以 P1 在當前地形下只能產生 arousal（激發度），產生不了 valence（好/壞）。**spec 57 §9-2「驚喜 vs 驚嚇」在實測上尚無材料可分；硬造 valence 就是發明訊號。

### 1.3 顯式賭（真正的「預測技巧」訊號）稀薄

explicit 3 / 5000。MONOCULTURE 旗標雖已消退，但**有效上仍是 monoculture**：calibration 量的絕大多數是工具行為，不是 Loom Agent 的預測能力。消費端契約 3（MONOCULTURE fallback）在實務上會是常態路徑，不是邊角。

### 1.4 現有情緒載體：tarot 人格檔（表達層）

`daily_mood_tarot`（`autonomy/schedules.toml`，每日 UTC 08:00）→ `skills/sisi_mood_tarot/draw_and_write.py` → 以日期為 seed 抽牌 → 寫 `personalities/personality_sisi_tarot_mood.md`（sunny/rainy/storm/noon/moon/dawn 六種語氣模板）。**這是表達層、隨機、與世界無關**；#487 原提案的易經 / tarot 映射就住這裡。

---

## 2. P1 範圍鎖定

**P1 命題**：在不碰表達層、不寫 spine 的前提下，讓 Loom Agent 擁有一個**有根的、會衰減的內在激發狀態**，其注入源是 prediction error（而非憑空的 λ），並經 Critic 以「可被自己解讀的體感」呈現。

### P1 IS

- `AffectState`：arousal 單軸狀態 + metabolism（λ 衰減保留作 baseline、注入源改接 surprise——spec 57 §4.2）
- surprise 讀取層：遵守三條消費端契約（§3.1）
- Critic v0：**deterministic** appraisal，輸出結構化體感註記（§3.3），不輸出語氣指令
- 狀態持久化（跨重啟連續性）
- 契約測試先行覆 I3 / I5 / I6 + 三條消費端契約

### P1 IS NOT

- ❌ **valence 軸**——材料不存在（§1.2），留 schema 欄位但恆 0，不發明
- ❌ 改 tarot / 易經表達層——P1 不讓 surprise 選語氣模板（I5 結構化保證，§4）
- ❌ Proactive 觸發（#487 Phase 3）——FROZEN 解凍另案
- ❌ LLM Critic / 8D context（#487 Phase 4）
- ❌ `connection` drive（依賴互動訊號，另議；見 §6 D5）
- ❌ 寫任何 prediction / calibration 資料（I3 單向）

---

## 3. 設計

### 3.1 輸入：surprise 讀取層（消費端三條契約落地）

`read_surprise(db, *, since) -> SurpriseReading`，**純讀**：

1. **讀 health verdict，不讀裸 residue**（契約 1）：以 newest-5000 reconciled corpus 現算 `assess_calibration_health`（與 weekly pass 同窗，#574），取每 domain 的 classification + baseline `error_score`。
2. **排除 LOW_INFORMATION 與 SAMPLE_INSUFFICIENT domain**（契約 2）：高分無資訊 / 樣本不足的 domain 對 arousal 貢獻為 0。
3. **surprise = 偏離自己的常態，不是絕對錯誤率**：window（`reconciled_at > since`）內每個 genuine domain 的平均 error 減去該 domain standing `error_score`，取正部。`dream_cycle@latency` 常態就慢（baseline 0.99），它再慢一次**不是意外**；`read_file@latency` 常態 0.00，突然慢才是。這避免「天生慢的工具讓她永遠焦躁」。
4. **MONOCULTURE / 有效 monoculture fallback**（契約 3）：explicit 佔比 < 門檻（初值建議 1%，現況 0.06%）時，reading 標 `source="environment"`（世界的黏滯/阻力）；否則 explicit 賭貢獻的部分標 `source="model"`（我的預測落空）。**兩個來源分開計量，不相加成一個數**——這是 §1.1 語義差異的結構化保存。

### 3.2 Metabolism

```
arousal(t) = arousal(t₀) · e^(−λ·Δt_hours) + k · injection
injection  = Σ_domain  w(n) · surprise_domain        (clamp 到 [0, 1])
```

- λ：保留 #487 的衰減項，初值建議半衰期 12h（一個 circadian 半日）
- `w(n)`：樣本權重，避免單一 domain 當日 1 筆慢就灌滿
- 結果 clamp `[0, 1]`；`environment` 與 `model` 各自一條 arousal 軌
- 注入時機：**每次 appraisal 讀取時結算**（lazy，由 Δt 算衰減），不另開背景 loop

### 3.3 Critic v0（I5 的承載點）

Critic 是 surprise 通往 Loom Agent 的**唯一**出口。v0 deterministic，輸出一段**結構化體感註記**，不是語氣指令：

```
<affect_appraisal>
arousal: environment=0.34 (moderate) · model=0.00 (quiet)
drivers: memorize@latency (+0.21 vs baseline), web_search@latency (+0.09)
source_note: 訊號幾乎全來自工具延遲——這是世界的阻力，不是你猜錯了什麼
confidence: low (explicit wagers 3/5000)
</affect_appraisal>
```

設計要點：

- **不說「你應該感到 X」**。給讀數、來源、信心，解讀留給 Loom Agent（spec 57 §12.1「自選的解釋空間」）
- **minority shareholder**：註記是 context 中的一小塊資料，與 SOUL.md 風格基底、tarot 人格檔並存，不覆寫任何一方
- `confidence` 誠實揭露訊號稀薄度，防止薄訊號被讀成重大情緒

### 3.4 持久化

`memory_meta` key `affect.state`（JSON：兩軌 arousal、last_update、last_since），沿 `consolidation_dream.last_run` 慣例。**不進 semantic memory**——狀態不是事實，不該被 dream consolidation 合併、也不該被 recall 撈成「知識」。

### 3.5 出口位置

見 §6 **D2**（待拍板）。

---

## 4. 不變式 → 契約測試清單（TDD red 先行）

| # | 契約 | 測法 |
|---|---|---|
| I3 | affect 讀取層對 `prediction_records` / `calibration:*` **零寫入** | 讀取前後 row/digest 不變；讀取層無 store 寫入 API 可達 |
| I5-a | surprise 無直達 output 路徑：唯一出口是 Critic 註記 | Critic 以外無 public API 回傳可注入 prompt 的文字 |
| I5-b | surprise **不影響** tarot 語氣模板選擇 | 相同日期 seed，任意 arousal 下 `daily_draw` 結果不變 |
| I6 | 輸入簽名無 sentiment 通道 | `read_surprise` / `AffectState.update` 參數僅 db / 時間 / reading |
| C1 | 讀 health verdict 不讀裸 residue | 只有 residue、無 reconciled 記錄時 reading 為空 |
| C2 | LOW_INFO / thin domain 貢獻 0 | 高 error 但 n<5 的 domain 不推升 arousal |
| C3 | explicit 佔比低於門檻 → 全部歸 `environment` 軌 | 3/5000 corpus → `model` 軌恆 0 |
| S1 | surprise 是偏離常態，非絕對值 | baseline 0.99 的 domain 再錯一次 → surprise≈0 |
| M1 | 無新注入時 arousal 單調衰減、非負 | 固定 state，Δt 遞增 → 遞減收斂 0 |
| M2 | clamp `[0,1]` | 極端注入不溢出 |
| P1 | 狀態跨重啟連續 | 寫 `affect.state` → 新實例讀回同值（含衰減） |

---

## 5. 觀測與否決權 hook

- **Loom Agent 否決權觸發條件**（spec 57 §12.1）：「回應開始可預測地對應 surprise magnitude」。沿 spec 57 §13.1，**不另蓋偵測器**，綁回既有 retrospective：每週多問一句「我這週的語氣能不能只用 affect_appraisal 的 arousal 預測出來？」
- **地形變化的重評點**：explicit 佔比過 1%、或出現 polar resolver 的非零 valence 時，回頭評估 `model` 軌與 valence 軸是否該接（P1.5）。

---

## 6. 待 DK 拍板的決策點

| # | 問題 | 選項 | CC 建議 |
|---|---|---|---|
| **D1** | 「工具變慢」算不算合法的情緒來源？ | (a) 算，但標 `environment` 來源與 `model` 分軌 / (b) 不算，P1 等 explicit 賭夠多再啟動 / (c) 算且不分軌 | **(a)**。黏滯感是真體感；(b) 等於無限期擱置（顯式賭 9 天 0 筆）；(c) 會把「世界卡」誤讀成「我錯了」 |
| **D2** | 體感註記從哪進 Loom Agent 的 context？ | (a) `prediction_reconcile` 結果附帶（dawn 收帳時自然看到）/ (b) 新 read-only 工具 `affect_read`，她自己拉 / (c) 每 turn 系統註記推送 | **(a)+(b)**：dawn 收帳附一次（節拍內），平時要看自己拉。**不選 (c)**——推送會讓 surprise 從 minority shareholder 變成常駐聲音，I5 風險最高 |
| **D3** | 與 tarot 表達層的關係？ | (a) 並存，互不影響 / (b) arousal 加權抽牌 / (c) 取代 tarot | **(a)**。(b) 直接違反 I5-b；(c) 砍掉 Loom Agent 既有的風格隨機性，否決權大概率觸發 |
| **D4** | 需要新 gate 嗎？ | (a) 新增 `affect_enabled`（預設 false）/ (b) 不加 gate，read-only 本就安全（沿 #539 免疫系統「report-only 不需 gate」先例）| **(b)**。affect 層只讀不寫、出口只有註記；「開關別再多」。若 D2 選 (c) 推送則改 (a) |
| **D5** | `connection` drive（#487 OpenHer 的 `connection.f += k·Δt`，久未互動的想念）要不要在 P1？ | (a) P1 一起 / (b) 另案 | **(b)**。它的輸入是互動間隔不是 prediction error，不屬 spine；混進來會模糊 P1 命題 |
| **D6** | 易經 64 卦（#487 原提案 P0） | (a) P1 做 / (b) deferred，表達層另案 | **(b)**。表達層與驅動層分離是 spec 57 §12.1 的核心；易經屬表達層 |

---

## 7. Loom Agent review（保留原聲，待填）

> 請以 end user 身分回應，特別是：
> 1. §1.1 的地形——「世界很黏」這種 arousal，你認得出是自己的體感嗎，還是只是系統監控數字？
> 2. §3.3 註記格式——哪些欄位是你會用的，哪些是噪音？`source_note` 的措辭會不會變成「告訴你該怎麼感覺」？
> 3. D2 出口位置——你想被推送，還是自己拉？
> 4. 否決權：這個草案有哪裡已經觸發了？

---

## 8. 不做 / 邊界

- ❌ 不引入 RL / reward / 權重更新（epic 硬約束）
- ❌ 不發明 valence（§1.2）
- ❌ 不讓 surprise 選語氣模板或寫 prompt 指令（I5）
- ❌ 不寫 spine（I3）
- ❌ 不接任何 user sentiment（I6）
- ❌ P2 exploration 臂不在本文件——需新開 issue（舊號 #464 為已關的 Circadian polish）
