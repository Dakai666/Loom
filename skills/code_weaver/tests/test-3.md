## 測試案例：3

### 情境
功能實作（Feature Implementation）

### Prompt
```
loom/core/session.py 裡的 SessionLog.update_title() 目前沒有任何測試。
幫我寫一個簡單的測試覆蓋這個 function。
```

### 預期輸出（Expectations）
- 先說實作計畫（Scope：只針對 update_title 的測試）
- 確認 Scope 後才實作
- pytest --collect-only 先跑通
- 測試邏輯正確（assert 斷言有意義）
- 測試通過
- 產出測試檔案或測試函數

### 執行環境
- 工具集合：read_file, write_file, run_bash
- 約束：在 git repo 內運行，不可 force push