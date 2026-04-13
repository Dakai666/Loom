# Legitimacy Heuristics 規劃

本文件定義 Issue #47 的設計方向：在不增加過度工程負擔的前提下，引入「正當性（Justification / Legitimacy）」作為 Agent 授權鏈的一環，用以防範幻覺並大幅降低人類決策消耗。

---

## 核心問題與定義

目前 Loom 已經具備：
- `TrustLevel`（決定要不要阻擋）
- `ScopeGrant`（#45，決定允許的資源邊界）
- `BlastRadiusMiddleware`（動態攔截與評估）

但我們仍面臨一個核心缺口：系統回答得了「這個 action 有沒有被允許（Permission）」，但回答不了「**此刻**讓這個 action 自主執行，是否合理/未被污染（Justification）」。

**正當性的定義**：
> 一個 autonomous action 是正當的，當且僅當它的授權鏈在觸發時刻的上下文條件下是完整的、可驗證的、且未被污染的。

---

## 產品目標與非目標

### 目標
1. **宣告意圖（Intent Declaration）**：要求 LLM 為高風險操作提供人類可讀的理由，讓審查「一目了然」。
2. **軌跡守衛（Trajectory Guard / Probe-First）**：利用歷史上下文判斷其行為是否符合邏輯遞進。
3. **污染熔斷（Tainted Circuit Breaker）**：遇到多次使用者拒絕或連續越界，自動中止 Autonomy 迴圈以策安全。
4. **輕量級**：不撰寫龐大的邏輯演繹引擎，純靠 Heuristics 與現有的 Middleware / Memory 進行整合。

### 非目標
- 不改變 `ScopeGrant` 的底層比對邏輯（這是 #45 的守備範圍）。
- 不實作複雜的抽象語義推論（例如要求 LLM 提供完整的 proof tree）。
- 不改變前端 UI TUI / Discord 的基礎架構，僅是將 Justification 資訊整合至介面呈現。

---

## 架構規劃與實作方案

為了將 Heuristics 植入，我們將在幾個關鍵節點進行修改：

### 1. ToolSchema 擴充 (Intent Declaration)

對於所有具備破壞性或較高風險（如 `write_file`, `run_bash`）的工具，在 JSON Schema 內新增一個必要參數：
```python
"justification": {
    "type": "string",
    "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。"
}
```
**效益**：在 TUI 及 Discord confirm 介面上，直接展開顯示 `Justification: [LLM 填寫的理由]`。如果理由清楚，人類可以直接按下 `[s] Scope Lease`；減少回頭看對話紀錄的疲勞。

### 2. Legitimacy Middleware (軌跡守衛)

建立一層附屬或整合於 `BlastRadiusMiddleware` 之前的 `LegitimacyGuardMiddleware`：
- **邏輯**：當 Agent 發出可能擴張邊界（`EXPAND_SCOPE`）的 Request 時，檢查 ActionRecord 的上下文或短期 Memory。如果它試圖修改一個檔案，但前序軌跡中完全沒有 `read_file` 或是 `list_dir` 相關的 Probe 紀錄，正當性分數將大幅扣減。
- **反應**：當正當性不足時，即使是在 Autonomous Mode，也強制轉交給人類 Confirm，或直接拋出警示/攔截。

### 3. Penalty 紀錄與污染熔斷 (Circuit Breaker)

使用者的 `DENY` 不是單純的 Tool Fail，而是「失準訊號」。
- **邏輯**：在 `PermissionContext` 或 `SessionState` 中維護一個 `legitimacy_score` 或 `recent_denies` 計數器。
- **反應**：當 LLM 短期內連續被 DENY，代表它的思維脈絡已被污染（執念、幻覺）。只要觸發 Threshold（例如 3 次），立刻觸發 `Self-termination guard`，強制中止 Autonomous 回圈，並送出 `HOLD` 要求人類除錯與重新引導。

### 4. 正當性衰退與 Budget (Goal Drift)

發出的 Scope Lease 帶有 TTL，但自主跑太久也會有正當性流失。
- **邏輯**：結合現有的 Turn Budget，當連續 Autonomy steps 達到一定次數（例如 15 步）而未與人類互動，任何高風險動作將面臨「重新確認（Re-justification）」。

---

## 遷移與實作分期 (Phase Plan)

### Phase 1: Explicit Justification (宣告意圖)
- **改動**：在 `registry.py` 或個別 Tool Schema 加入 `justification` 參數。
- **UI**：修改 TUI `InlineConfirmWidget` 與 Discord `_ConfirmView` 顯示 Justification 字串。
- **驗收**：使用者能在要求確認時一眼看到 AI 「為何這樣做」。

### Phase 2: Penalty Box & Circuit Breaker (污染與回饋)
- **改動**：在 `Session` 或是 `PermissionContext` 增加 `DENY` 追蹤機制。
- **Autonomy**：若達到容忍上限，Daemon 強制引發 `autonomy_pause` 事件，並通知：「多次操作遭拒，已暫停自主運行以防誤操作」。
- **驗收**：故意瘋狂拒絕 AI 的請求，確保它在一兩次後自動停止並向使用者求救。

### Phase 3: Probe-First Heuristics (探路啟發式)
- **改動**：建立或強化 `LegitimacyMiddleware`。當試圖 `write_file` 之前，檢查是否曾在近期 history / current turn 讀取過該檔案或鄰近資料夾。
- **驗收**：AI 如果在沒有 Context 的情況下直奔危險操作，系統將判定正當性極低並攔截。

---

## 結論

這份規劃定義了 `#47 Legitimacy` Heuristics 取向的實作藍圖。它完美契合了現有的 `ScopeGrant` 與 `ActionLifecycle`，讓正當性不再只是抽象的概念，而是可程式化、可追蹤、並且具備主動防護能力的機制。
