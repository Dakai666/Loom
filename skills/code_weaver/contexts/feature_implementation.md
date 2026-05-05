# 功能實作（Feature Implementation）

**觸發後自動載入本檔案。**

---

## 成功定義

- **產出**：乾淨的 git diff + 測試覆蓋 + 可即時行動的 PR 或 commit
- **品質指標**：最小改動 Scope 精準；pytest --collect-only 先跑；所有相關測試通過
- **驗收方式**：變更範圍內的功能正常運行，範圍外絲毫未動

---

## 工作流程（六階段）

### 階段 1：理解意圖

目標：搞清楚要什麼，同時用圖譜找到正確的改動位置。

- 取得 issue 內容（URL → fetch_url；本地 → read_file）
- 識別 issue type：`bug` / `enhancement` / `refactor` / `feature`
- **用圖譜快速定位**：
  ```
  mcp__gitnexus__query(query="<issue 的核心概念或症狀>",
                       task_context="<issue 標題>")
  → 找到相關執行流程，確認要動哪些符號
  ```
- 識別 constraint：有沒有破壞性變更（breaking change）？

產出：一段「意圖說明書」（不超過 50 字）+ 圖譜找到的候選符號清單

### 階段 2：Scope 確認（必經！）

目標：讓使用者在實作前確認範圍，**並用 impact 自動驗證 scope 是否完整**。

**在這個階段之前絕對不動手。**

```
mcp__gitnexus__impact(target="<主要改動的符號>",
                      direction="upstream", maxDepth=2)
→ 自動列出 d=1（WILL BREAK）和 d=2（LIKELY AFFECTED）
→ 若 risk = HIGH/CRITICAL，scope 必須包含 d=1 的所有項目
```

對照 **Loom 高風險 hub**（`permissions.py`/`registry.py`/`procedural.py`）：
若改動涉及這些，主動告知使用者影響面廣，確認後才進入。

說清楚：
1. 我要改哪些檔案（從 impact 結果確認）
2. 我不改哪些（即使它看起來也有問題）
3. 這個改動的邊界在哪裡

使用者閱讀後確認方向，才進入下一階段。

### 階段 3：實作計畫

目標：說計畫，不是埋下去就做。

在開始改 code 之前，用以下格式說明：

```markdown
## 實作計畫：{issue title}

### 改動策略
[我要怎麼做，2-3 句話]

### 具體步驟
1. [step 1 — 具體檔案和修改內容]
2. [step 2]
3. [step 3]

### 預期效果
[改完之後，系統會變成什麼樣]

### 潛在風險
[我担心什麼（如果有的話）]
```

使用者說「方向對，開始」才進入階段 4。

### 階段 4：實作

目標：按計畫執行，不多不少。

- 讀取目標檔案
- 做指定改動
- 如有需要，同時寫測試
- 每個 commit 有一個明確的 change
- 過程中發現 scope 需要擴大，**停下來回報**

產出：乾淨的 git diff

### 階段 5：驗證（硬規則，沒過不進入下一階段）

目標：確認修乾淨了。

**第一步永遠是 `pytest --collect-only`** — 這條 0.5 秒，只 import + 收集，不跑任何 test 邏輯。
它擋掉 import-time 級別的炸：dataclass 欄位順序錯、循環 import、模組級 NameError。

完整流程：
1. `pytest --collect-only` — import sanity（**必跑，必先跑**）
2. 跑相關測試：`pytest tests/test_<area>.py` 或專案指定的測試腳本
3. 確認沒有破壞其他功能（必要時跑全套）
4. UI / display 改動：寫死 sample input 跑一次，眼睛看輸出長相
5. 確認所有 linter 通過（`ruff` / `ruff format`）
6. **圖譜收尾確認**：
   ```
   mcp__gitnexus__detect_changes()
   → 確認 git diff 影響的符號與 Stage 2 的 scope 一致
   → 若有意外的符號出現，重新評估是否超出範圍
   ```

驗證沒過 → **停下來修**，不是假裝看不見繼續前進。

### 階段 6：產出

目標：讓使用者可以立即行動。

> **GitNexus 自動更新**：commit 完成後 `.git/hooks/post-commit` 會在背景靜默執行 `gitnexus analyze`，索引自動保持新鮮，無需手動觸發。

**PR / commit 模式：**
```markdown
## {issue title}

**Fixes:** #{issue_number}
**Type:** {bug/enhancement/refactor/feature}

### 改了什麼
[1-2 句話]

### 怎麼改的
[具體描述，不只是「優化」或「修復」]

### 測試
- [測試名稱或描述]
- 結果：pass / fail
```

**直接 commit 模式：**
直接 commit，並在 commit message 引用 issue。

---

## 紀律提醒

- **跳過階段 2（Scope 確認）是最大的浪費時間行為**
- **Scope 是鐵律** — 只做約定好的範圍，不做「順便」
- **停下來比做完再說好** — 過程中發現 scope 需要擴大，立即回報
- **沒有測試的實作不完整** — 如果原本沒有測試，改動後要補充
- **自己先 review diff 再當作最後防線** — 每一次產出都先自己看一遍
- **驗證沒通過絕對不進入下一階段** — 這是硬規則