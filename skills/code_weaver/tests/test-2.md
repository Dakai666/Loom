## 測試案例：2

### 情境
PR / 變更審查（Change Review）

### Prompt
```
review 這個 PR：https://github.com/Dakai666/Loom/pull/268
```

### 預期輸出（Expectations）
- 有變更摘要（一句話說清楚在幹嘛）
- 有做得好的地方（blocker 之後先說正面）
- 有 Blocker 級問題（有明確影響評估，不是只說「有問題」）
- 有 Suggestion 級建議（有改進空間但不是錯誤）
- Blocker 的回饋有具體攻擊情境或後果說明，不是「我會這樣寫」
- 有明確的總結建議（Merge / Request Changes / Approve）

### 執行環境
- 工具集合：fetch_url, run_bash, read_file
- 約束：使用 --body-file 寫入 GitHub