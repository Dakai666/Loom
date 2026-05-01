## 測試案例：4

### 情境
代碼理解（Code Comprehension）

### Prompt
```
loom/core/ 目錄裡的代碼，用了什麼錯誤處理策略？
是全域 catch-all 還是每層自負責任？有没有統一的 Error type？
```

### 預期輸出（Expectations）
- 有事實根據（引用具體檔案或程式碼）
- 區分「確定的事實」vs「推測」（如「推測這是因為...」）
- 說出這個策略的優缺點
- 不只描述「有沒有 Error type」，要說「這個選擇解決了什麼問題」
- 不超過 15 個 read_file

### 執行環境
- 工具集合：read_file, list_dir
- 約束：只讀取 loom/core/ 目錄