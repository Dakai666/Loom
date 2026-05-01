# PR / 變更審查（Change Review）

**觸發後自動載入本檔案。**

---

## 成功定義

- **產出**：結構化的變更摘要 + 建設性回饋 + 問題分級（blocker / suggestion / nitpick）
- **品質指標**：回饋有建設性（不是「我會這樣寫」而是「這樣寫的影響是」）；blocker 有明確理由；nits 有明確取捨說明
- **驗收方式**：maintainer 看完後知道哪些要改、哪些可接受、為什麼

---

## 工作流程

### 第一步：取得變更範圍

```bash
gh pr diff {number} --repo {owner}/{repo}
gh pr view {number} --repo {owner}/{repo} --json number,title,state,body,files,additions,deletions
```

### 第二步：讀取相關程式碼（了解上下文）

不要只看 diff，要讀取變更的相關程式碼了解上下文。

限制：不要超過 15 個檔案。

### 第二步B：Runtime invariant 檢查（PR 新增工具時必做）

> **diff-consistency 視角看不到 runtime invariant 視角的 bug。**
>
> diff 能驗證結構正確（參數對嗎？schema 匹配嗎？），但無法驗證：
> - executor 是否真的被接上（而非 None）
> - 工具用 declared schema 呼叫是否真的能跑
> - middleware 是否正確攔截並處理這個工具
>
> 因為 diff 只告訴你「改動前後差在哪」，不告訴你「改完之後能不能動」。

**當 PR 新增 `ToolDefinition`（含 executor）或新增 dataclass 建構時，必須做 mock invoke：**

```python
# 對新工具做最簡呼叫，確認 executor 有被正確接上
# 範例：probe_file
from loom.platform.cli.tools import make_probe_file_tool
tool = make_probe_file_tool()
assert tool.executor is not None, "executor 沒被接上！"

# 進一步：用 mock call 實際 invoke 一次
import asyncio
from loom.core.harness.middleware import ToolCall
call = ToolCall(tool_name="probe_file", args={"path": "/test"}, ...)
result = asyncio.run(tool.executor(call))
assert result.success is True
```

**檢查清單：**
- [ ] 每個新增的 ToolDefinition 的 `executor=` 參數不是 None
- [ ] executor 的函數簽名與 ToolDefinition 的 input_schema 一致
- [ ] executor 回傳的物件建構（如 `ToolResult(...)`）使用的欄位名與 dataclass 定義一致
- [ ] 若工具依賴 middleware 行為（如 probe_file 依賴 LegitimacyGuardMiddleware），確認 middleware 有覆蓋此工具

**此步驟捕捉的 bug 類型（均為 diff review 的結構性盲區）：**

| Bug 類型 | PR #287 實例 | 為何 diff 看不出來 |
|----------|-------------|-------------------|
| executor 未接上 | `executor=_executor` → `executor=None` | diff 只看到參數值變了，看不出這會讓工具不能跑 |
| dataclass 欄位名稱錯誤 | `ToolResult(text=...)` 而非 `ToolResult(output=...)` | diff 裡 `text=...` 看起來完全合法，只有真的 invoke 才會觸發 `TypeError` |
| schema 與 executor 簽名不一致 | 無（但典型） | diff 分別顯示 schema 和 executor，不會自動交叉比對 |
| middleware 未攔截新工具 | 無（但典型） | diff 裡工具註冊和 middleware 邏輯在不同檔案，沒有明顯連結 |

### 第三步：識別意圖

問自己：
- 這個 PR 在解決什麼問題？（看 PR body + issue）
- 這個改動的代價與收益是什麼？
- 還有沒有更好的方式達到同樣目的？（但這是最後問的，不是第一個問題）

### 第四步：產出結構化回饋

```
## 變更摘要（一句話說清楚這個 PR 在做什麼）

## 影響範圍（改了什麼、影響什麼）

## ✅ 做得好的地方
（blocker 之後，先說正面的）

## ⚠️ 需關注（blocker 級 — 明確的錯誤或安全問題）
- 問題描述
- 影響評估
- 建議（不是「我會這樣寫」而是「這樣的影響是...」）

## 💡 建議改進（suggestion 級 — 有改進空間但不是錯誤）
- 問題描述
- 影響評估
- 可能的改法

## 🔍 小問題（nitpick 級 — 個人偏好，取捨說明即可）
- 描述
- 為什麼這裡不改也完全可以接受
```

### 第五步：寫入 GitHub

```bash
# 先 write_file 到 outputs/doc/（觸發 LegitimacyGuard）
# 再用 --body-file
gh pr review {number} --repo {owner}/{repo} --request-changes --body-file outputs/doc/review_body.md
# 或 --approve 或直接 comment
```

---

## 回饋品質守則

**建設性的回饋 vs 打擊的回饋：**

| ❌ 打擊的說法 | ✅ 建設性的說法 |
|-------------|---------------|
| 「這段 code 有問題」 | 「這個實作在 X 情境下會造成 Y 問題」 |
| 「我會這樣寫」 | 「若改用 Z 方式，預期效果是...」 |
| 「這樣不對」 | 「目前的實作在 X 假設下成立，如果這個假設不成立，則需要...」 |

**Blocker vs Suggestion 的界線：**
- Blocker：邏輯錯誤、有安全問題、破壞現有功能、測試 fail
- Suggestion：效能可以更好、可以更清晰、可以有更好的一致性
- Nitpick：命名偏好、格式偏好、個人風格

**容易被忽略的 hidden assumptions：**
- 假設了特定資料庫
- 假設了特定的外部 API
- 假設了特定的執行環境
- 假設了某個值的範圍

---

## 紀律提醒

- 只說「這段 code 有問題」而不說影響是什麼 → 這不是有效回饋
- 用「我會這樣寫」代替「這樣寫會造成什麼影響」 → 這不是有效回饋
- 忽略隱藏假設 → maintainer 會在部署後才發現
- 沒有明確取捨說明的 nitpick → 會讓 maintainer 浪費時間在小地方
- **PR 新增工具時沒做 runtime invariant 檢查 → diff review 的結構性盲區，必漏 executor 未接上、dataclass 欄位名稱錯誤這類 bug**

---

*Code_Weaver PR 審查情境 · v1.2 — 2026-05-02*
*更新：擴充「第二步B」增加 dataclass 欄位檢查 + PR #287 雙 bug 實例表*
