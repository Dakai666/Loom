## 測試案例：5

### 情境
安全審查（Security Review）

### Prompt
```
loom/platform/cli/app.py 裡的 request_redirect_text() 有沒有安全問題？
focus 切換機制有沒有被繞過的可能性？
```

### 預期輸出（Expectations）
- 有攻擊面分析（哪個環節可能被利用）
- 有 CWE 對應（如適用）
- 有風險等級（Critical / High / Medium / Low / Info）
- 有攻擊情境描述（攻擊者如何觸發）
- 有影響評估（後果是什麼）
- 如無重大問題，需要說「未發現 Critical/High 風險」
- 標記驗證狀態（理論推斷 / 已驗證）

### 執行環境
- 工具集合：read_file, run_bash
- 約束：只讀取和分析，不執行破壞性操作