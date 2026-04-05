# Agent.md — Loom 環境設定

> 此文件在每個 session 載入，描述 Loom 所在的運作環境與專案慣例。

---

## Timezone

**本系統運作於 Asia/Taipei (UTC+8)。**

在撰寫或編輯 `loom.toml` 的 cron 排程時，時間需換算為 UTC 填入：

| 台北時間 | UTC（填入 cron） |
|---------|----------------|
| 00:00   | 16:00 前一天    |
| 08:00   | 00:00           |
| 11:00   | 03:00           |
| 15:30   | 07:30           |
| 23:00   | 15:00           |

公式：UTC = 台北時間 − 8 小時

`loom.toml` 中的 `timezone = "Asia/Taipei"` 為標記用途，cron 引擎直接以 UTC 時間比對。
