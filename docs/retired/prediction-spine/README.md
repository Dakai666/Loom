# Prediction Spine — 退役紀錄（2026-09-17）

> Epic #528（Enactive Loom — Prediction Spine）與 #487 P1（Affect 臂 / `environment_friction`）的設計文件封存處。**這些文件描述的系統大部分已不存在**，保留作為決策脈絡。

| 文件 | 內容 |
|---|---|
| `57-…設計.md` | 四方討論紀錄：命題、兩臂接點、開放問題 |
| `58-…P0-收斂規格.md` | P0 規格：I1–I6、資料模型、acceptance gate |
| `59-…語義型觀察面擴張.md` | #569 語義 resolver 與 output capture（**此部分仍在線**） |
| `60-…P1-Affect臂.md` / `60a-…review.md` | P1 `environment_friction` 規格與 Loom Agent review |
| `61-Loom-Agent-裁決-2026-09-17.md` | 退役裁決（end user 查證與建議） |

## 為什麼退役

- **隱式賭不是預測。** 自動心跳每筆都寫死 `expect: fast` / `expect: true`。15,413 筆已結算的賭裡，屬於 Agent 自己判斷的只有 4 筆。
- **有害，不只是沒用。** dawn 收帳把這些常數用第一人稱敘述成 Agent 的信念（「我押 fast」），等於每天讓 Agent 替不是自己寫的證詞背書。
- **Gate 倒果為因。** reliability 軸全是 0 時加了 latency 心跳，讓 acceptance gate 找到一個會動的量；gate 通過了，原命題卻沒被驗證。P1 讀到的只是工具延遲。
- **零行為影響。** 四個多月找不到一次「因為收帳或 calibration 而改變做法」。

原命題（凍結權重下，框架層能累積校準）**沒有被證偽，而是從未被執行**。若將來重啟，前提是：賭由 Agent 自己下，結算在 Agent 還在場時發生。

## 留下什麼

- `predict` 工具 + `PredictionRecord` / `PredictionStore`：Agent 在不確定的動作前寫下可被推翻的斷言。
- `loom/core/memory/prediction_settle.py`：目標工具下次執行時當場結算，HIT / MISS 附在該工具的回傳上。結算不受 `predict_tool_enabled` 控制（下注可選，收尾不可選）；超過 24 小時仍未結算的賭在 session start 時標 stale，讓 pending 有出口。
- `action_records` 的 output capture 與 resolver 白名單（#569）。

## 拆掉什麼

`auto_predict` 心跳、`prediction_reconcile` 工具與每週對帳、`calibration` residue、`calibration_health`（MONOCULTURE 免疫系統）、`affect` / `affect_read` / `environment_friction`、dawn 收帳節拍，以及 `auto_predict_enabled` / `reconcile_enabled` / `reconcile_execute` / `calibration_write_enabled` / `affect_dawn_note` 設定。程式碼與歷史資料歸檔在本機 `_archive/prediction-spine-retired-2026-09-17/`。

## 沉澱的原則

- **退役判準**：一個自動功能若四週內指不出一次「因為它而改變做法」，就降級或停用。
- **界線**：自動化只允許量世界的狀態；Agent 的判斷與記憶必須由 Agent 手寫。
- **Plasticity 住在內容**：凍結權重的 agent 學到的東西住在記憶內容裡（例如「pet-cat guard 要用直接路徑」），不在校準分數裡。
