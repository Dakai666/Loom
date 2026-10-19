---
name: memory_big_dream
description: "記憶大夢（一次性全庫整理）。當使用者說「做一場大夢」、「把記憶全部整理/蒸餾一次」、「記憶太多太重複」，或第一次啟用蒸餾夢、語料庫長期沒整理時使用。日常每 12h 的蒸餾夢、每週的收斂夢都會自動跑，不需要這個技能。"
tags: [memory, maintenance, distill, consolidation]
---

# Memory Big Dream — 一次性全庫整理

日常的夢一次只處理幾個鄰域：蒸餾夢每 12h 一場、每場 5 個，收斂夢每週一場、有上限。如果語料庫已經累積了好幾個月沒整理，或是剛啟用蒸餾夢，就先跑一場大夢，一次把全庫掃過，之後再交給日常的夢接手。

設計與邊界見 `docs/designs/57-記憶蒸餾夢-規格.md`（§4–§6）。

---

## 成功定義

- **產出**：一份前後對照（活記憶數、機器/手寫比例、重複群估計），加上 `autonomy/circadian/dreams/<日期>.md` 裡每一輪的 夢境鞏固 / 記憶蒸餾 報告。
- **品質指標**：活記憶數下降。被取代的原文都能從 survivor 的 `metadata.consolidated_into` 找回。手寫記憶（`memorize`、使用者親述、tool 驗證）與 milestone 不會被改動。
- **驗收方式**：把對照表和報告交給使用者看。**值不值得由使用者判斷**，不要自己宣布成功。

---

## 它會做什麼

`loom memory big-dream` 依序執行：

1. **普查**：整理前的快照。
2. **收斂**：近重複（cos ≥ 0.85）的收斂夢，跑全庫、不設簇數上限。
3. **蒸餾**：同主題不同面向（cos 0.70–0.85）分批精煉，每輪 `--batch` 個鄰域。一直跑到沒有可蒸餾的鄰域、某一輪完全沒有拿到判定（只有 deferred / failed，例如 LLM 暫時出問題），或達到 `--max-passes` 為止。同一次執行中每個鄰域只試一次；被否決的鄰域在內容改變前，之後的執行也不會重審，所以每一輪都會往下走。
4. **普查**：整理後的快照，並印出前後對照。

安全網全部內建，不需要手動處理：

- 每個會寫入的階段都會先備份，存到 `~/.loom/backups/memory-pre-consolidation-*.db` 和 `memory-pre-distill-*.db`。**備份失敗時，該階段自動降級成唯讀。**
- 自審有否決權，資訊遺失的 gate 會擋下蒸餾。寫入當下會再比對一次版本，規劃之後被改過的記憶會跳過。
- 跟 daemon 是不同的 process，SQLite 會隔離兩邊的寫入，**不需要停 daemon**。

---

## 步驟

在**該 agent 的目錄**執行，因為 `loom.toml`、`autonomy/` 和報告都以 cwd 為準。沒有指定 `--db` 時，會用該 `loom.toml` 的 `[memory] db_path`（相對路徑以 cwd 為準），都沒有才用 `~/.loom/memory.db`。指令一開始會印出實際使用的 DB，請先確認它是這個 agent 的 DB。

### 1. 先彩排（唯讀，不需要任何開關）

```bash
loom memory big-dream --db <DB 路徑> --batch 20
```

唯讀模式只跑一輪蒸餾，因為沒有任何記憶被取代，跑第二輪只會重複同樣的結果。唯讀完全不寫 DB，所以彩排結果不會影響之後正式跑的內容。打開當天的 `autonomy/circadian/dreams/<日期>.md` 看 記憶蒸餾 報告，逐條檢查：

- 「精煉」有沒有保留來源的關鍵細節？
- 「不保留」的理由站得住嗎？例如已作廢的規則、已由手寫記憶承載。
- 有沒有把不同的事硬拼成一條？

**把報告交給使用者確認後才進行下一步。** 品質有疑慮就停在這裡回報，不要調參數硬跑。

### 2. 打開寫入開關（由使用者決定）

寫入需要兩道開關，缺一不可：命令列的 `--execute`，加上 `loom.toml` 裡**使用者設定的開關**。

```toml
[memory.consolidation_dream]
execute = true          # 收斂夢寫入

[memory.distill_dream]
execute = true          # 蒸餾夢寫入（日常 12h 的蒸餾 loop 也會因此開始寫入）
```

修改 `loom.toml` 前先備份到 `backups/`（例如 `cp loom.toml backups/loom.toml.<日期>-big-dream`）。只開其中一個也可以，沒開的階段會維持唯讀，指令會印出提醒。

### 3. 執行

```bash
loom memory big-dream --db <DB 路徑> --execute --yes
```

`--yes` 會跳過互動確認，給 agent 或腳本用；人自己跑時可以省略。只想蒸餾就加 `--skip-consolidation`。指令會邊跑邊印進度，最後印出對照表：

```
                              before    after       Δ
live facts                     10843     9xxx    -xxx
...
stopped: no eligible neighbourhood left
```

`stopped` 的意思：

| stopped | 意思 | 下一步 |
|---|---|---|
| `no eligible neighbourhood left` | 全庫掃完 | 完成 |
| `no progress (only deferred / failed)` | 剩下的鄰域這次都沒有拿到判定，例如 LLM 暫時出問題 | 晚點重跑同一個指令 |
| `max_passes (N) reached` | 還沒跑完 | 重跑，會從剩下的接著做 |
| `distill backup failed` | 備份失敗，已停止寫入 | 查磁碟空間或權限，修好再跑 |

### 4. 回報

把對照表、當天的 dreams 報告路徑，以及備份檔名交給使用者。

---

## 注意

- **不要自己重啟或關閉 daemon。** 大夢是另一個 process，不需要停 daemon；重啟由使用者決定。
- **不要跳過彩排。** 唯讀報告是使用者判斷品質的唯一依據。
- **還原**：要整個退回時，先請使用者停掉 daemon，再用備份檔取代 DB。單筆記憶可以從 survivor 的 `metadata.consolidated_into` / `consolidation_history` 找回原文。這兩件事都要使用者同意後才做。
- 很多個 agent 共用同一份程式時，每個 agent 各跑一次，各用自己的目錄和 DB。
