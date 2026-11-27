---
name: sisi_mood_tarot
description: "每日 Mood Tarot：由既有腳本先備份，再抽牌／套用特殊日期情緒、更新人格檔與日誌。"
allows:
  exec:
    - python3 skills/sisi_mood_tarot/draw_and_write.py
---

# 每日 Mood Tarot

用於 daily_mood_tarot 排程。先讀這份技能，再從 workspace 直接呼叫：

```text
python3 skills/sisi_mood_tarot/draw_and_write.py
```

只用這條 plain script，不加參數、redirect、pipe 或串接。不要先跑 ls、test、cd、date 或額外備份 shell。需要讀取／列目錄時，一般 read_file/list_dir 沿用原工具規則，不需要也不取得 exec grant。時間參考 chime 的 fired_at（Asia/Taipei），腳本本身仍用執行環境的 date.today()；延遲跨日不要自行改時間／日期參數補跑。

腳本的可信內部效果（source 核對，沒有在本片執行）：

- 讀取本技能 moods 範本、special_dates.json（若有）與 Python 支援模組；一般日期按日期 seed 抽牌，特殊日期採指定情緒。
- 在任何人格／日誌更新前，建立 `skills/sisi_mood_tarot/backups/<date>/`，保存原本存在的 `personalities/personality_sisi_tarot_mood.md` 與 `skills/sisi_mood_tarot/log/draw_log.json`。以 exclusive create 保留同日首份，不覆蓋；備份錯誤會在這兩份更新前中止。不要繞過失敗或另外刪改備份。
- 覆寫 `personalities/personality_sisi_tarot_mood.md`，並更新 `skills/sisi_mood_tarot/log/draw_log.json`。兩次更新不是單一 transaction；後段失敗可能已完成人格更新，回報實際錯誤與已完成階段，不自動重試。
- Python 匯入也可能建立本技能 `__pycache__`，視執行環境而定。上述是可信腳本效果，並非 path grant 所隔離的清單。

DK 已選 exec-only：本技能沒有 allows.write、allows.tools；入口 allowed_tools=[]，沒有原生 write_file 授權。舊技能 root、錯位的 loom/core/personalities、date grants 都撤除，不把實際人格儲存位置搬走。不要手動重寫人格或 draw_log，也不修改 script、SKILL.md、既有 guards。

exec shape 只限制呼叫形狀，不隔離 subprocess 的檔案／DB／網路效果；allows 也不把不可信腳本變安全。既有腳本、備份與 guard 保持原狀。載入本技能不會替一般對話新增 grant，仍由當次 permission_mode 與 hard guards 判斷。
