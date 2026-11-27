---
name: memory-housekeeping
description: "每週記憶庫維護與語料庫週報：先普查、再預覽與清理衰減記憶，最後寫維護報告。"
allows:
  write:
    - outputs/self_check
---

# 記憶庫維護與語料庫週報

用於 memory_housekeeping 排程。這是有順序、有寫入效果的維護活動；不是唯讀健康檢查。一般工具沿用自身效果、入口宣告與 permission_mode，不另外列入 allows.tools。

1. 先呼叫 `memory_health(view="corpus")`。此工具會儲存當日 census snapshot；必須在 prune 前量，才能保留這一週的樣子與上週差值。
2. 呼叫 `memory_prune(dry_run=true)` 預覽。若有符合條件的記憶，依既有排程授權呼叫 `memory_prune(dry_run=false)`。這會將衰減的 active 記憶封存，並可能真正刪除已封存且仍衰減的記憶；不是單純讀取。不要為改善統計另外改記憶、memorize 或調整 threshold。
3. 用 `write_file` 寫 `outputs/self_check/<today>-memory-corpus.md`。記 census 一行摘要、來源佔比與 machine:hand、重複版本群（≥3）與最大群、從未被存取比例，以及可取得的上週差值與 prune 回傳 examined/pruned/retained。目前 memory_prune 回傳文字沒有 archived/deleted 分拆；未提供就明說無法分拆，不編造數字或另查私人資料。

日期以本次 chime 的 fired_at（Asia/Taipei）理解；若處理延遲，明示它是觸發日期。不要另跑 date、shell 維護指令或夢的 CLI。工具失敗時記錄已完成步驟與錯誤，不宣稱全部維護完成，不為補報告而重新執行刪除。

`allows.write` 只給原生報告寫入目錄，不是 DB 權限或 OS 隔離。memory_health/prune 目前註冊為 SAFE，即使前者存快照、後者會刪資料；本片保留既有分類與工具內部邏輯，沒有用這份技能替 SAFE 分類背書。
