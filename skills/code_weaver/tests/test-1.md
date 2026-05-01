## 測試案例：1

### 情境
代碼理解（Code Comprehension）

### Prompt
```
分析 loom/platform/cli 這個模組的架構。
```

### 預期輸出（Expectations）
- 有結構快照（目錄結構、主要入口點）
- 有依賴拓撲（核心模組、第三方 library）
- 有觀察優點（具體指出 + 說原因）
- 有觀察疑慮（標記「確定」或「推測」）
- 有學習點（接手時先做什麼）
- 有適合/不適合場景
- 不是流水帳「這個檔案有什麼函數」而是說「解決什麼問題」

### 執行環境
- 工具集合：read_file, list_dir, run_bash
- 約束：不超過 15 個 read_file