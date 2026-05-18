# PR / 變更審查（Change Review）

**觸發後自動載入本檔案。**

適用於 review PR、diff、commit range、patch。此情境採 code-review 姿態：
先列 findings，再補摘要；目標是幫 maintainer 判斷風險，不是展示個人偏好。

---

## 鐵律

```text
NO REVIEW FINDINGS WITHOUT EVIDENCE
NO GITHUB WRITE WITHOUT BODY-FILE AND POST-WRITE VERIFY
```

---

## 成功定義

- **產出**：具體 findings + 風險分級 + 可驗證影響 + 最終 verdict
- **品質指標**：每個 blocker 都有檔案/行號/情境；suggestion 說明取捨；nit 不阻塞
- **驗收方式**：maintainer 看完知道哪些必修、哪些可選、為什麼

---

## 工作流程

### 第一步：取得變更範圍

```bash
gh pr view {number} --repo {owner}/{repo} --json number,title,state,body,files,additions,deletions
gh pr diff {number} --repo {owner}/{repo}
```

對本地 diff 或 PR checkout 後執行：
```bash
npx gitnexus detect-changes
```

對 detect_changes 回傳的高風險符號執行：
```bash
npx gitnexus impact <symbol> --direction upstream
```

記錄：
```markdown
## Review Scope

**Changed symbols:** [...]
**Affected processes:** [...]
**Risk level:** [...]
```

### 第二步：讀上下文，不只看 diff

- 根據 detect_changes 與 diff 選 3-5 個關鍵檔案精讀。
- 找到 PR 意圖：它想解決什麼問題？
- 對照現有 working pattern：這個改動是否違反既有約定？
- 檢查架構層規則：尤其 `core/` 不得新增依賴 `autonomy/`。

### 第二步B：Runtime invariant 檢查（新增工具/建構時必做）

diff 能看出形狀，但看不出 runtime 是否真的接上。當 PR 新增或改動以下項目時，
必須做最小 mock invoke 或 import sanity：

- `ToolDefinition` / executor
- dataclass 建構
- middleware 攔截規則
- plugin/tool registry
- config schema 與 runtime loader

最小檢查範例：
```python
from loom.platform.cli.tools import make_probe_file_tool

tool = make_probe_file_tool()
assert tool.executor is not None
```

檢查清單：
- [ ] 新增 ToolDefinition 的 `executor=` 不是 None
- [ ] executor 函數簽名與 input_schema 一致
- [ ] 回傳 dataclass 欄位名與定義一致
- [ ] middleware 覆蓋需要攔截的新工具
- [ ] registry/config loader 真的載入新項目

此步驟捕捉的 bug 類型：

| Bug 類型 | 實例 | 為何 diff 看不出來 |
|----------|------|-------------------|
| executor 未接上 | `executor=_executor` -> `executor=None` | diff 只看到參數值變了，看不出工具不能跑 |
| dataclass 欄位名稱錯誤 | `ToolResult(text=...)` 而非 `ToolResult(output=...)` | 語法看似合法，invoke 才會 TypeError |
| schema 與 executor 簽名不一致 | N/A（典型問題） | schema 和 executor 分在不同位置 |
| middleware 未攔截新工具 | N/A（典型問題） | 註冊與 middleware 邏輯常在不同檔案 |

### 第三步：形成 findings

Finding 必須符合：

```markdown
### [P0/P1/P2/P3] Title

**Location:** `path:line`
**Evidence:** [diff/code/test/GitNexus]
**Impact:** [使用者或系統會怎樣]
**Trigger:** [在什麼情境下發生]
**Suggested fix:** [具體但不過度指定風格]
```

Severity：
- **P0**：會造成資料破壞、安全事故、無法 release。
- **P1**：核心功能 broken、明確 regression、測試應阻擋。
- **P2**：邊界情境錯誤、可預期維運風險。
- **P3**：非阻塞改善、可讀性或一致性。

沒有 findings 時明確說「No blocking findings」，並列出剩餘風險或未驗證項。

### 第四步：輸出 review

預設格式：
```markdown
## Findings

[依 P0 -> P3 排序；沒有就寫 No blocking findings.]

## Open Questions

[只有真正影響判斷的問題]

## Summary

[1-3 句說明 PR 做了什麼、整體風險]

## Verification / Evidence

- `[command]` — [result]
- `npx gitnexus detect-changes` — [result]

## Verdict

[Approve / Comment / Request changes + 理由]
```

### 第五步：寫入 GitHub

先把 review body 寫入 `outputs/doc/`，再執行：

```bash
gh pr review {number} --repo {owner}/{repo} --request-changes --body-file outputs/doc/review_body.md
# 或
gh pr review {number} --repo {owner}/{repo} --approve --body-file outputs/doc/review_body.md
# 或
gh pr comment {number} --repo {owner}/{repo} --body-file outputs/doc/review_body.md
```

成功後立即 verify：
```bash
gh pr view {number} --repo {owner}/{repo} --json reviews,comments
```

---

## 回饋品質守則

| 不夠好 | 更好 |
|--------|------|
| 「這段 code 有問題」 | 「這個實作在 X 情境下會造成 Y」 |
| 「我會這樣寫」 | 「若採用 Z，能避免 Y，但代價是 A」 |
| 「應該要加測試」 | 「缺少覆蓋 X regression 的測試，因為 Y path 目前會漏掉」 |

---

*Code_Weaver PR/變更審查情境 · v3.0 — 2026-05-19*
