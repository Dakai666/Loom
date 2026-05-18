# 代碼理解（Code Comprehension）

**觸發後自動載入本檔案。**

適用於「分析」「說說這段」「架構怎麼運作」「幫我理解流程」。
此情境只做理解與判斷，不改 code；若要修改，handoff 到 `feature_implementation.md`。

---

## 成功定義

- **產出**：架構快照 + 執行流程 + 事實/推測分離 + 對使用者有用的判斷
- **品質指標**：不是流水帳；每個重要判斷都回答「所以呢？」
- **驗收方式**：非原作者看完能說出這段設計解決什麼問題、風險在哪、下一步該看哪裡

---

## 工作流程（圖譜優先 + 精讀少量檔案）

### Layer 0：圖譜速查

先用 GitNexus 建立地圖，再決定要讀哪些檔案。

```bash
npx gitnexus query "<要理解的模組或概念>"
npx gitnexus context <重要符號>
```

若需要架構層檢查，可用 GitNexus cypher/MCP 查：
- hub 檔案
- 循環依賴
- `core/` -> `autonomy/` 違規

目標：選出 3-5 個值得精讀的檔案或符號。

### Layer 1：結構快照

回答：
- 這個模組/流程在系統中負責什麼？
- 入口點在哪？
- 哪些檔案/符號是核心，哪些只是支援？
- 它依賴哪些外部或內部能力？

### Layer 2：執行流程

用「資料/控制流」描述，而不是檔案列表。

```text
User / caller
  -> entry point
  -> validation / routing
  -> core state change
  -> output / side effect
```

標記每一步的證據來源：檔案、函數、GitNexus process、測試。

### Layer 3：事實、推測、未知

固定分三類：

```markdown
## Facts
- [我已看到的證據]

## Inferences
- [根據 facts 推測；標明不確定性]

## Unknowns
- [需要更多資料才能確認的點]
```

### Layer 4：所以呢

每個重要觀察都補上工程意義：

- 解決了什麼問題？
- 在什麼條件下會失效？
- 如果要接手，第一個該碰哪裡？
- 哪個風險值得追，哪個只是噪音？

---

## 交付格式

```markdown
# [目標] — Code Comprehension

## Snapshot
[2-3 句說明這是什麼、負責什麼、在系統中的位置]

## Execution Flow
1. [入口與證據]
2. [中間流程與證據]
3. [輸出/副作用與證據]

## Facts
- [具體證據]

## Inferences
- [推測 + 為什麼合理 + 不確定性]

## Risks / Pressure Points
- **[風險]**：原因、條件、可能後果

## So What
[給使用者的判斷：下一步、接手順序、是否值得改]
```

---

## 深度標準

淺：
- 「這個檔案包含 X、Y、Z。」
- 「這裡用了 async。」

深：
- 「這個檔案把 routing 和 state mutation 放在一起，因此新增模式時改動集中，但測試也必須覆蓋 UI callback 與 session 狀態兩側。」
- 「這裡用 catch-all exception 提升互動式 CLI 的容錯，但代價是 root cause 會被延後到 log 才能看見。」

---

## 紀律提醒

- 不要為了看起來完整而讀太多檔案；圖譜先行，精讀少量。
- 不要把推測寫成事實。
- 不要在理解情境中順手實作。
- 不要只列檔案，要說出設計後果。

---

*Code_Weaver 代碼理解情境 · v3.0 — 2026-05-19*
