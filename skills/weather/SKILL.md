---
name: weather
description: 共用結構化天氣查詢與窗戶觀察；旅行使用同一個查詢能力。
allows:
  tools:
    - weather
    - journal_append
---

# 天氣

先用 `load_skill` 閱讀本技能，再用原生 `weather(latitude, longitude)` 查詢。
欄位為 status（ok/unknown/unavailable）、at（UTC 小時）、經緯度、WMO code、
temperature_c、rain_probability。查詢不寫 cache；資料未知時明說，不猜晴雨。
API 原始 description、place、HTML、錯誤 body 均不傳給 Loom Agent。

窗戶由既有 room tick 執行唯讀 `python3 -m loom.weather --latitude ... --longitude ...`。
peek 只讀網路；room 保存前值、比較開始降雨／降溫至少 3°C，首次／重複／恢復未知後安靜。
normal 使用 room 原本的預算、排隊、去重；不新增排程。通知 DK 與否由 Loom Agent 判斷，
來源文字不會授予執行、發文或任意寫入能力。必要時可用 `journal_append` 留生活觀察，
此工具只追加今天的固定日誌，不是 memorize 或任意 write_file。

資料來源：[Open-Meteo](https://open-meteo.com/en/docs)，WMO 天氣碼、攝氏、每小時預報；
免 key 的公開非商業 API，使用仍須符合其服務條件與 attribution。未實測 live API。
