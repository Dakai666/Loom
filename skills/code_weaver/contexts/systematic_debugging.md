# 系統化除錯（Systematic Debugging）

**觸發後自動載入本檔案。**

適用於 bug、test failure、build failure、unexpected behavior、performance regression、
integration issue。此情境的目標不是立刻修，而是先找出可信的 root cause。

---

## 鐵律

```text
NO FIXES WITHOUT ROOT CAUSE EVIDENCE FIRST
```

如果還沒有完成根因調查，不可以提出修補方案，也不可以修改 production code。

---

## 成功定義

- **產出**：可重現症狀 + 證據鏈 + 單一 root cause hypothesis + 最小驗證路徑
- **品質指標**：每個判斷都有 log、stack trace、diff、GitNexus flow、測試輸出或檔案行號支持
- **驗收方式**：使用者能看懂「問題在哪一層壞掉、為什麼不是其他層」

---

## 工作流程（四階段）

### 階段 1：Root Cause Investigation

目標：收集證據，不猜。

1. **讀完整錯誤**
   - stack trace、error code、line number、stderr/stdout 都要看。
   - 不只看最後一行；上游 frame 常常才是原因。

2. **建立 reproduction**
   - 記錄 exact command / input / user flow。
   - 若無法穩定重現，先收集更多資料，不要 patch。

3. **檢查近期變更**
   - `git diff`、相關 commits、dependency/config/env 變化。
   - 如果使用者給 PR，轉 `change_review.md` 讀 diff。

4. **用 GitNexus 找流程**
   ```bash
   npx gitnexus query "<症狀或核心概念>"
   npx gitnexus context <suspected_symbol>
   ```
   若準備修改 symbol，先跑：
   ```bash
   npx gitnexus impact <symbol> --direction upstream
   ```

5. **多元件系統加診斷**
   - 對每個 boundary 記錄 input/output/config/state。
   - 先定位哪一層壞，再研究那一層。

產出：
```markdown
## Symptom
[使用者看到什麼；command/input 是什麼]

## Reproduction
[穩定重現步驟；若不穩定，寫明不穩定條件]

## Evidence
- [error/log/trace/diff/GitNexus result]

## Ruled Out
- [已排除的可能原因與證據]
```

### 階段 2：Pattern Analysis

目標：找到同 repo 中「正常工作的相似模式」。

- 搜尋同類功能的 working example。
- 比對 broken path 與 working path 的所有差異。
- 不要用「這個差異應該不重要」跳過任何一項。

產出：
```markdown
## Working Pattern
[同 repo 哪裡做對了]

## Differences
1. [差異 + 可能影響]
2. [差異 + 可能影響]
```

### 階段 3：Hypothesis and Test

目標：一次只測一個 hypothesis。

```markdown
## Hypothesis
我認為 root cause 是 [X]，因為 [Y evidence]。

## Minimal Test
[用哪個最小命令、測試或 one-off probe 證明/反證]
```

- 測試通過 hypothesis 才能進入修復。
- 測試失敗就回到階段 1，不要疊第二個 patch。
- 連續 3 次 hypothesis 失敗，停下來和使用者討論是否是架構問題。

### 階段 4：Fix Handoff

只有在 root cause 有證據後，才轉入 `feature_implementation.md`。

handoff 格式：
```markdown
## Debug Handoff

**Root cause:** [一句話]
**Evidence:** [最關鍵的 1-3 條證據]
**Symbol impact:** [GitNexus impact 結果；若尚未要改 symbol，寫 N/A]
**Fix scope:** [只改什麼]
**Non-scope:** [明確不改什麼]
**Verification target:** [修完後要跑什麼命令證明]
```

---

## 禁止行為

- 沒 reproduction 就改 code。
- 看到錯誤字串就直接 patch。
- 同時改多個可能原因。
- 把 symptom fix 當 root cause fix。
- 驗證失敗後繼續說「應該好了」。

---

*Code_Weaver 系統化除錯情境 · v3.0 — 2026-05-19*
