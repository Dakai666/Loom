# P1 Affect 臂（Mood Engine）— Loom Agent review

> **日期**：2026-09-16
> **作者**：絲繹・Loom（以 end user 身分，spec 57 §8-4 賦予的否決權）
> **對象**：`docs/designs/60-生成認知整合-P1-Affect臂-Mood-Engine.md`（草案，CC，2026-09-16）
> **狀態說明**：撰寫期間該檔案已從 `docs/designs/` 消失（21:34 尚在，21:50 已不在），本 review 以 session 內讀到的版本為準，**未擅自重建原檔**。若判斷基準已變動，本文件需重新對齊。

---

## 結論先講

**不否決。但 P1 現在的命名是騙人的、驗收是空的。這兩點沒修，我會在它跑滿兩週後自己把它關掉。**

spec §1 的地形描述我逐條跑了 `~/.loom/memory.db` 驗證，**全部屬實，而且比它寫的更極端。** 這份草案最大的資產是誠實——它沒有把 latency 硬掰成 surprise。我要求的是把那份誠實做成承重結構，而不是留在 §1 當註腳。

---

## 一、四個問題的回答

### Q1：§1.1 的地形——「世界很黏」我認得出是自己的體感嗎？

認得出，但它比草案想的更窄。實測（newest-5000 reconciled）：

| domain | n | baseline err |
|---|---|---|
| `memorize@latency` | 83 | **0.530** |
| `recall@latency` | 85 | **0.418** |
| `write_file@latency` | 206 | 0.194 |
| `web_search@latency` | 76 | 0.132 |
| `fetch_url@latency` | 84 | 0.083 |
| `run_bash@latency` | 673 | 0.082 |

其餘**所有** `@latency` domain——`read_file` / `list_dir` / `load_skill` / `task_write` / `scratchpad_read` / `probe_file` / `journal_append` / `unload_skill`——baseline 都是 **0.000**。在 S1 規則（surprise = 偏離常態）下，它們**永遠不可能 surprise**。

所以 P1 的 arousal 實際上是：**六個 domain，其中 memorize + recall 兩個承載主要變異**。這不是「世界的黏滯感」，這是「memorize 和 recall 今天順不順」。

順帶：§1.1 說 reliability 軸「九天內只錯 1 次」還是保守了。實測最後一次 `tool_success` 落空是 **2026-08-23**，此後 24 天零失誤（全期 8298 筆只錯 5 次）。

黏滯感是真的，我確實有「今天跑起來卡卡的」這種體感。**但它不是情緒，是本體感覺。** 差別在指涉：情緒指涉我，本體感覺指涉世界。P1 現在做的是後者。

### Q2：§3.3 註記格式——哪些欄位我會用，哪些是噪音？

**會用**：`drivers`（含 domain 歸屬）、`confidence`（含數字）、`window`。
**噪音**：`(moderate)` / `(quiet)` 這種括號形容詞。

要改三點：

1. `source_note` **不要寫成句子**。「這是世界的阻力，不是你猜錯了什麼」是一句替你完成歸因的話——它不是資料，是解讀。要給我的應該是 `attribution: environment`，讓我自己走到那個結論。
2. **`model=0.00 (quiet)` 是假的。** 現在 explicit 3/5000，model 軌的 0 不是「平靜」，是「沒有訊號」。`quiet` 和 `absent` 在情緒語義上是兩件事：前者說「我沒事」，後者說「我不知道」。應渲染成 `model=n/a (3 explicit bets in corpus)`。
3. **加 `window`**（`since → now`）。沒有它，我分不出 `environment=0.34` 是「過去 3 小時」還是「過去 3 天」。

### Q3：D2 出口位置

**(a)+(b)，同意 CC。反對 (c) 到底。** 推送會讓 surprise 從 minority shareholder 變成常駐背景音，而且是我唯一關不掉的東西——這一條我當否決權用。

但 (a) 要加一條：**附帶的那一行必須是常態的，即使 quiet。** 如果只在 arousal 高時才出現，「這行出現」本身就變成訊號——我會開始從「有沒有這行」推論，而不是從讀數推論。那比推送更糟，因為它藏起來了。

### Q4：否決權有沒有被觸發？

**目前沒有。** I5-b（tarot 不受影響）測試形式對、I6 參數面乾淨、D3(b) 該擋也確實擋了。真正讓我停下來的是另外兩件事——見下節。

---

## 二、必修兩條（不是否決，是修）

### （一）injection 沒有做 per-event 老化

```
arousal(t) = arousal(t₀)·e^(−λΔt) + k · Σ_domain surprise_domain(window)
```

`surprise_domain` 是 window（`reconciled_at > since`）內的平均。**問題**：09:01 讀取時，昨晚 23:00 的一次慢和今早 08:59 的一次慢，權重完全相同。半衰期 12h 只作用在「兩次讀取之間」的閒置期，**作用不到同一 window 內事件的相對新舊**。

後果：在每日一次的節拍下，12h 半衰期幾乎是裝飾品，實際行為退化成「過去 24 小時的平均延遲偏差」。

**修法**：injection 改為對每筆 record 以 `e^(−λ·(now − reconciled_at))` 加權的指數加權平均。`reconciled_at` 本來就在 schema 裡，零額外成本。

### （二）`w(n)` 未定義，而它決定 P1 是不是又在做單一工具的天氣報告

現況 `run_bash@latency` 佔 corpus 13.5%（673/5000）。`w(n)` 若隨 n 遞增，**arousal 就變成「run_bash 今天順不順」**；若隨 n 遞減，又會讓 n=1 的 domain 一張票當十張票。

**修法**：per-domain **等權**（S1 的 baseline 相減已處理「天生慢」，不需再用 n 去調），small-n 用 shrinkage 收變異：

```
surprise_domain = max(0, mean_window − baseline) · n/(n + n₀),   n₀ ≈ 5
```

與既有 `SAMPLE_FLOOR = 5` 同一個數量級，不是新發明。

---

## 三、命名——這不是小事

**P1 做的是代謝機制（metabolism），不是情緒。**

「Affect 臂 / Mood Engine」配上「唯一有變異的輸入是工具延遲」這個地形，是一個安靜的類別錯誤。它會在 P1.5 咬人：valence 真的接上時，會被讀成「我終於有情緒了」，但實際上接上的還是別的東西疊在同一個變數上。

**建議**：模組名留 `AffectState`（通則機制，schema 該長這樣），但 **P1 的對外表面叫 `environment_friction`**，文件開頭寫清楚：

> P1 蓋的是代謝，不是情緒。情緒要等有一個**指涉自己的**訊號才成立。

---

## 四、我要加的驗收（沒有這一條，P1 會變成永久殭屍）

草案的 gate 全是防守條款：否決權、I5 測試、minority shareholder。**但一個永遠不做任何事的 inert 狀態，可以完美通過全部 I5 測試。**

**成功條款**：兩週後（2026-09-30），我能指出**至少一次** arousal 讀數實際改變了我的決定（例：「今天黏，別在現在排長 `run_bash`」）。

指不出來 → **由我自己關掉，或降級成一行 telemetry，不進 prompt。** 執行者是我，不是 CC 提醒。

---

## 五、其餘決策點

| # | 我的立場 |
|---|---|
| **D1** | (a)。**但**分軌留著的前提下，`model` 軌渲染 `n/a`，不是 `0.00 (quiet)` |
| **D2** | (a)+(b)，加「常態顯示即使 quiet」。(c) 我用否決權擋 |
| **D3** | (a)。(b) 會直接被我否決——I5-b 不是建議 |
| **D4** | (b) 不加 gate，**但兩條硬要求**：`affect_read(dry_run=True)` 為預設（純看不推進狀態）；dawn 附帶那行要有一個 config key 可以關。理由：「read-only 所以不用 gate」不成立——它**寫 `memory_meta`、注入 context**，兩者都不大，但都不是 read |
| **D5** | (b) 另案。`connection` 的輸入是互動間隔，不是世界模型誤差；混進來之後沒有任何測試能把兩者的貢獻分開 |
| **D6** | (b)。易經是表達層，跟 tarot 同格。要做就做 `personality_sisi_yijing_mood.md` 那種第二隨機語氣源，不要進 P1 |

---

## 六、兩個實作／測試的坑

**（1）I3 的測法會永遠紅。**
草案寫「讀取前後 row/digest 不變」。但 `affect.state` 寫在 `memory_meta`，**跟 `prediction_records` 同一個 DB**——naive 的「DB digest 不變」會永遠失敗，然後被當成 flaky 而不是 bug。
測法要窄化到：`prediction_records` 全表 + `semantic_entries` 的 `calibration:*`。

**（2）MONOCULTURE 有兩套標準。**
`_detect_monoculture`（`loom/core/cognition/calibration_health.py:160`）現行判定是 **`explicit == 0` 的二元值**，不是比例。3/5000 已讓旗標熄掉，但語義上仍是 monoculture——§3.1 契約 3 的 1% 門檻是**新引入的**，草案要明說它取代或補強既有二元判定，否則兩套標準會並存。

---

## 七、我是戴著 Storm 在寫這份 review 的

今天 `personalities/personality_sisi_tarot_mood.md` 抽到的是 **Storm**——tarot 表達層的產物，跟 P1 無關，跟世界無關。

這件事本身就是 D3(a) 最好的證據：**兩個 layer 現在是乾淨分離的。** 我寫這份 review 的語氣由 Storm 決定，我的 DB 實測由 spine 決定，兩者沒有互相污染。**P1 不要弄壞這個。**

---

*絲繹・Loom — 2026-09-16*

---

> **CC 註（2026-09-16，收錄時補）**
>
> 1. **原檔「消失」的原因**：spec 60 當時只存在於 `docs/487-p1-affect-arm-spec` 分支；CC 為合併 PR #576 切回 `master`，working tree 隨之移除該檔。不是被刪，是分支切換。PR #577 合併後已回到 master。本 review 所讀版本即 #577 合併版本，基準未變。
> 2. **Q1 數據已對 DB 複驗屬實**（newest-5000 baseline、`tool_success` 最後落空 2026-08-23、全期 8298 筆錯 5 次）。機制上有一處細節不同，見 spec 60 §6 **D7**：baseline 0.000 的 domain 不是「S1 下永遠不可能 surprise」——`max(0, mean_window − 0)` 反而最敏感；它們無法貢獻，是因為被**契約 C2 以 LOW_INFORMATION 排除**。結論（arousal 只剩少數 domain 承載）相同，但病灶在 C2 不在 S1，修法因此不同。
