# Harness Execution 可視化規劃

本文件定義 Loom 下一階段在 **TUI** 與 **Discord** 的可視化升級方向。

核心目標不是把 agent 畫成「待辦事項 DAG」，而是把 Loom 的 **harness-first engineering** 精神以可觀察、可審計、可理解的方式呈現出來。

---

## 問題定義

Loom 已經具備大量控制能力：

- 工具有明確 Trust Level：`SAFE / GUARDED / CRITICAL`
- 工具執行受 middleware pipeline 管理
- Action Lifecycle 具備授權、準備、執行、驗證、提交、回滾等狀態
- Session 可在同一批次內做平行工具 dispatch

但目前前端呈現主要仍偏向：

- `ToolBegin / ToolEnd` 的線性時間軸
- 單工具狀態提示
- 單輪文字回放

這會導致一個落差：

- **內部系統很像受控執行機器**
- **外部視覺卻像普通 chat + tool log**

而 DAG 本身也不適合作為唯一主視圖，原因是：

- DAG 對工程師有用，但對使用者不一定直觀
- 當前 DAG 實際上主要代表的是 **parallel dispatch structure**
- 它不是完整的「agent 思考流程圖」，也不是專案管理式 TODO board

因此，新的可視化方向必須重新定義主敘事。

---

## 產品主張

### 一句話版本

> Loom UI 應該優先呈現「受控執行系統」而不是「任務清單」。

### 具體定義

使用者在 TUI 與 Discord 上，首先應該感知到的是：

- 這一輪 agent 想做哪些 action
- 哪些 action 被允許、拒絕、等待或回滾
- 哪些 action 可以並行
- 哪些 effect 已驗證並提交
- 整個過程是可觀察且可審計的

DAG 在此扮演：

- 底層結構圖
- 平行性與依賴關係的表達方式
- 不是首頁主視角，不是唯一視角

---

## 設計原則

### 1. Harness first，不是 task board first

畫面優先回答：

- `What is running?`
- `What is allowed?`
- `What is blocked?`
- `What is committed?`
- `What was rolled back?`

而不是只回答：

- `What are the nodes?`

### 2. 執行圖優先於思維圖

現階段先視覺化的是：

- 真實存在的 `tool batch`
- 真實存在的 `ActionState`
- 真實存在的 `parallel levels`

不先假裝存在完整的 cognition DAG。

### 3. 摘要優先，細節按需展開

預設視圖先給：

- batch 摘要
- level 摘要
- node 狀態
- 失敗與回滾提示

需要時再展開：

- state history
- validator / rollback reason
- args / output / duration

### 4. 平台適配，不追求同畫面複製

TUI 與 Discord 應共用同一資料模型，但 UX 不應相同：

- TUI 適合持續更新、局部展開、互動式 drill-down
- Discord 適合稀疏更新、批次快照、審計式摘要

---

## 非目標

以下內容不屬於本階段：

- 不做一般專案管理式 kanban / todo board
- 不做自由拖拉的 graph editor
- 不把 LLM 內在推理直接當成真實 DAG 可視化
- 不在 Discord 上追求高頻、逐節點動畫式更新
- 不引入前端比後端更複雜的圖形語言

---

## 概念模型

### 核心術語

#### Execution Envelope

一個 LLM `tool_use` 批次所形成的執行包。

它代表：

- 同一輪模型輸出的同一批 action
- 可能包含多個可並行節點
- 是 UI 的第一級觀察單位

#### Action Node

一個具備生命週期的工具執行節點。

每個 node 至少應可映射到：

- `call_id`
- `tool_name`
- `trust_level`
- `capabilities`
- `current_state`
- `duration_ms`
- `result / error`

#### Execution Graph

由 envelope 內 action node 組成的有向圖。

現階段至少支援：

- level-based parallel groups
- 明確 node state
- optional edge metadata

### UI 層級

Loom 的可視化應分成三層：

#### Layer 1: Control Surface

最先看到的畫面，回答：

- 現在有幾個 action
- 哪些在跑
- 哪些被 gate 控制
- 哪些成功 / 失敗 / 回滾

#### Layer 2: Execution Graph

以 level 與 edge 呈現：

- 哪些節點同層並行
- 哪些節點依賴前序結果

#### Layer 3: Audit Detail

單點展開後看到：

- 完整 state history
- validator reason
- rollback message
- trust / capability / args / duration

---

## 授權確認 UX（Confirm Interaction as Control Surface）

> 這是 v0.2.9.5 之後新增的設計維度，原文件未覆蓋。

### 定位

工具確認提示是 **Layer 1: Control Surface** 最直接的使用者接觸面。
它不只是「允許/拒絕」的 boolean gate，而是使用者對授權範圍進行精確管理的入口。

### 現有確認模型（v0.2.9.5+）

```
Allow this call?
  y — approve once       (ONCE, 無持久副作用)
  s — scope lease        (SCOPE, 30 分鐘 TTL，同 scope 自動允許)
  a — auto-approve       (AUTO, 持久 grant，此 tool class 永久授權)
  N — deny               (DENY)
```

這四個選項直接對應 `ConfirmDecision` enum 值，由 `BlastRadiusMiddleware` 路由到不同的 grant 建立邏輯。

### TUI 實作現況

`InlineConfirmWidget` 已 inline 嵌入 `MessageList`，無需 modal 跳出：
- 四個按鈕橫排：`✓ Allow [y]` / `⏱ Lease [s]` / `⚡ Auto [a]` / `✗ Deny [N]`
- hint 文字說明每個選項語意
- future 型別為 `asyncio.Future[ConfirmDecision]`，middleware 直接取用結果

### Discord 實作現況

`_ConfirmView` 四按鈕（green / blurple / grey / red），對應視覺語意：
- Allow：綠色（safe to approve once）
- Lease：藍紫（時間限制的特權）
- Auto：灰色（永久授權，謹慎操作）
- Deny：紅色

SCOPE / AUTO 決策後，bot 自動發送 follow-up 訊息說明授權範圍與 TTL：

```text
⏱️ Scope lease granted for `write_file` — auto-approved for this scope
   for the next 30 minutes.
```

### 已完成的 UI 工作

#### 1. Scope Grant 可視化面板 ✅

- **TUI**：`BudgetPanel` 整合 grants 指示器，顯示 active lease 數與最近到期時間，依 TTL 變色（green → yellow → red → dim）(#125)
- **TUI**：Execution Dashboard selected node detail 顯示 scope grant 狀態（auth_decision / TTL / selector）(#125)
- **Discord**：`/scope list` / `/scope revoke <id>` / `/scope clear` 指令已實作 (#127)

#### 2. Grant 到期可視化 ✅ (TUI) / 不做 (Discord)

- **TUI**：BudgetPanel grants 指示器依 TTL 變色（> 10m green / 5–10m yellow / < 5m red / expired dim），30 秒更新一次 (#125)
- **TUI**：lease 到期瞬間以 `app.notify()` 發出 toast 通知，不中斷操作 (#125)
- **Discord**：30 分鐘 lease 已足夠長，到期前提醒噪音大於價值，有意不做

#### 3. Confirm Widget 與 Execution Graph 的整合 ✅

- 等待確認的 node 在 Execution Dashboard 標記為 `⏳ awaiting confirm`（橙色）(#125)
- `LifecycleGateMiddleware` 注入 `AWAITING_CONFIRM` 狀態，session 層在 dispatch 等待期間持續 drain lifecycle events 並 yield `EnvelopeUpdated`，使 TUI 即時反映確認狀態 (#125)
- 確認後 node state 在 dashboard 中即時更新

---

## 當前系統現況

### 已具備能力

**基礎設施：**
- `TaskGraph` 與 `ExecutionPlan` 已能表示 level-based DAG
- `TaskScheduler` 已能執行同層並行
- `ActionState` 已能表示完整 control-first lifecycle
- `ExecutionEnvelope` / `ExecutionEnvelopeView` 已成為 stream event 與 UI 的第一級實體

**授權系統（v0.2.9.5+）：**
- `ConfirmDecision` enum（ONCE / SCOPE / AUTO / DENY）取代 bool 確認流程
- `ScopeGrant.valid_until` 欄位與自動過期過濾；`PermissionContext.purge_expired()` 清理
- `/scope list / revoke / clear` CLI + Discord 指令
- Self-termination guard

**TUI（#119, #125）：**
- `ExecutionDashboard` 取代舊 `SwarmDashboard`，envelope-aware execution surface
- Node 選取 + detail pane（trust / capabilities / state history / auth info）
- `BudgetPanel` 整合 grants 指示器（TTL 變色 + 到期 toast）
- Confirm graph：`⏳ awaiting_confirm` 即時顯示
- 歷史 envelope 左右瀏覽
- StatusBar 已移除，內容整合至 BudgetPanel

**Discord（#119, #127）：**
- `_ConfirmView` 四按鈕 + SCOPE/AUTO follow-up 訊息
- Envelope snapshot 顯示（level list + state icons）
- Completed envelope 凍結為永久訊息（連續性軌跡保留）
- Think summary 持久化為獨立訊息
- `/scope` + `/summary` slash commands
- Turn summary 精簡一行（預設 on）+ detail Embed 模式

### 現況限制

#### 1. ~~前端收到的是平面事件，不是 graph event~~ ✅ 已解決

`EnvelopeStarted` / `EnvelopeUpdated` / `EnvelopeCompleted` 已實作為 stream events（#106）。TUI 和 Discord 均以 envelope events 為主要消費來源，`ToolBegin / ToolEnd` 在 envelope 模式下降級為 fallback。

#### 2. `_dispatch_parallel()` 只把工具當成同層獨立節點

目前 parallel dispatch 將所有 tool calls 視為同一層無依賴節點（`level=0`, `parallel_groups=1`）。

因此現在 UI 最誠實的說法是「parallel dispatch graph」，而不是「full agent planning graph」。

未來方向：#128 (TaskGraph Agent-Driven Construction) 規劃讓 agent 主動建立帶依賴的 TaskGraph，使 envelope 支持多層執行（L0→L1→L2）。

#### 3. ~~`ExecutionEnvelope` 尚未成為前端第一公民~~ ✅ 已解決

`ExecutionEnvelopeView` 與 `ExecutionNodeView` 已定義於 `loom/core/events.py`，envelope 已成為：

- stream event 主體（`EnvelopeStarted` / `Updated` / `Completed`）
- TUI `ExecutionDashboard` 的顯示單位
- Discord `status_msg` 的顯示單位
- Session 層保留最近 10 個 envelope 供歷史瀏覽

---

## 目標體驗

### TUI 目標

TUI 的目標不是炫技圖形，而是讓工程師能快速讀出當前控制面。

### 預設視圖

右側 Workspace 預設新增或升級為 `Execution` 視圖，內容包含：

- 當前 envelope 摘要
- 每個 level 的節點行
- node state 顏色與 icon
- failure / rollback 標記
- 總並行度與批次耗時

示意：

```text
Envelope e42
3 nodes · 1 parallel group · 1.3s

L0  ✓ recall
L1  ⟳ read_file
L1  ✓ list_dir
L2  · write_file
```

### 展開視圖

使用快捷鍵或 modal 查看完整 graph detail：

- 節點詳細資料
- lifecycle timeline
- trust / capability
- args preview
- output / error snippet

### 互動行為

- 上下移動選取 node
- Enter 展開節點詳情
- `g` 或 `F5` 類型快捷鍵切換 graph/detail 模式
- 選取失敗節點時顯示 validator / rollback / error

### Discord 目標

Discord 的目標是讓 thread 中的參與者快速理解：

- 這一輪 agent 做了什麼
- 哪些工具並行
- 哪些被擋下或失敗
- 是否已完成、是否安全提交

### 預設策略

延續單一 `status_msg`，但內容從 tool timeline 升級為 batch snapshot。

示意：

```text
Envelope e42
L0  ✓ recall
L1  ✓ list_dir   ⟳ read_file
L2  · write_file

Guarded: 1
Rollback: 0
```

### turn 結束後

再補一則 summary embed：

- envelope 數
- action 總數
- 失敗數 / 回滾數
- 最慢節點
- 是否有人機暫停

### Discord 不做的事

- 不做複雜 ASCII edge drawing
- 不做高頻 node 級動畫
- 不做多訊息拼接成 pseudo-canvas

---

## TUI 規劃

### Information Architecture

目前 `WorkspacePanel` 為：

- `Artifacts`
- `Swarm`
- `Budget`

建議升級為：

- `Artifacts`
- `Exec`
- `Budget`

其中 `SwarmDashboard` 可演進為 `ExecutionDashboard`，保留歷史時間軸能力，但主體改為 envelope-aware execution surface。

## TUI 主畫面組成

### A. Envelope Header

顯示：

- envelope id
- node count
- parallel group count
- running / blocked / failed / reverted count
- batch elapsed time

### B. Level List

依 `L0 / L1 / L2` 顯示各層節點。

每個 node 顯示：

- icon
- tool name
- compact arg preview
- current lifecycle state
- duration

### C. Selected Node Detail

選定 node 後顯示：

- trust level
- capability flags
- state history
- observed effect
- validator reason
- rollback result

### D. History Mode

當前 envelope 完成後，仍保留最近 N 個 envelope 摘要以供追查。

---

## Discord 規劃

### Message Model

每個 turn 的訊息策略：

- `status_msg`
  - 動態 edit，只追蹤**當前執行中**的 envelope
  - Envelope 完成時凍結為永久訊息，建立新 `status_msg` 給下一個 envelope
  - 保留完整執行軌跡於 thread 中
- Think summary（`ThinkCollapsed`）
  - send-once，`-# 💭` 小字
  - 不再被 tool_buf 編輯覆蓋
- narration / response
  - send-once，`⬥` prefix
  - 保留 LLM 文本與 Markdown 顯示品質

## Status Message 結構

### 執行中（單層，典型情況）

```text
-# Envelope e9 · 2 actions
-# ⟳ recall  · run_bash
```

### 執行中（多層，未來 #128 完成後）

```text
-# Envelope e42 · 3 actions · 2 parallel groups
-# L0  ✓ recall
-# L1  ⟳ read_file   ✓ list_dir
```

### 完成時（凍結為永久訊息）

```text
-# Envelope e9 · 2 actions · completed 1.8s
-# ✓ recall  ✓ run_bash
```

### 失敗時

```text
-# Envelope e42 · 3 actions · failed
-# ✓ recall  ✗ read_file (permission denied)
```

## Discord Turn Summary ✅

由 `/summary` 指令控制，三段式模式：

- `off` — 不顯示
- `on`（預設）— 精簡一行：`-# ✓ N envelopes · M actions · X.Xs · grants N active`
- `detail` — Discord Embed，欄位包含 Envelopes / Actions / Failures / Elapsed / Paused / Rollbacks / Grants，footer 整合 persona / context / model

---

## 資料模型設計

### UI 專用 ViewModel ✅

已定義於 `loom/core/events.py`：

```python
@dataclass
class ExecutionNodeView:
    node_id: str
    call_id: str
    action_id: str
    tool_name: str
    level: int
    state: str                    # ActionState.value
    trust_level: str              # SAFE / GUARDED / CRITICAL
    capabilities: list[str]       # ToolCapability flag names
    args_preview: str = ""
    duration_ms: float = 0.0
    error_snippet: str = ""
    full_args: dict = field(default_factory=dict)       # Phase B (#108)
    state_history: list[dict] = field(default_factory=list)  # Phase B (#108)
    auth_decision: str = ""       # once / scope / auto / deny
    auth_expires: float = 0.0     # lease TTL timestamp
    auth_selector: str = ""       # scope selector
    output_preview: str = ""


@dataclass
class ExecutionEnvelopeView:
    envelope_id: str
    session_id: str
    turn_index: int
    status: str                   # running / completed / failed
    node_count: int
    parallel_groups: int
    elapsed_ms: float = 0.0
    levels: list[list[str]] = field(default_factory=list)
    nodes: list[ExecutionNodeView] = field(default_factory=list)
```

### Stream Events ✅

```python
@dataclass
class EnvelopeStarted:
    envelope: ExecutionEnvelopeView

@dataclass
class EnvelopeUpdated:
    envelope: ExecutionEnvelopeView

@dataclass
class EnvelopeCompleted:
    envelope: ExecutionEnvelopeView
```

### 設計原則

- `ToolBegin / ToolEnd` 保留向下相容
- TUI / Discord 優先使用 envelope events，`ToolBegin / ToolEnd` 在 envelope 模式下降級為 fallback
- `_build_envelope_view()` 在 Session 層做 projection，不污染 middleware

---

## 後端整合設計

### Phase 1：讓 `ExecutionEnvelope` 成為真實執行單位 ✅ (#106, #119)

- 每個 `tool_use` 批次建立 `ExecutionEnvelope`
- 每個 action node 與 `call_id`、`action_id` 對齊
- batch 完成時標記 `envelope.complete()`
- envelope 與 `ActionRecord` 綁定（`LifecycleContext` 注入 `call.metadata`）
- UI 以 envelope 為單位呈現

### Phase 2：建立 graph-aware projection ✅ (#106, #108)

`LoomSession._build_envelope_view()` 在 Session 層建立 projection，整合：

- tool uses → `ExecutionNodeView`
- action lifecycle updates → `state` / `state_history` / `duration_ms`
- scope grant info → `auth_decision` / `auth_expires` / `auth_selector`

Projection 層只做視圖整形，不污染 middleware 核心邏輯。

### Phase 3：歷史與 replay（部分完成）

- ✅ Session 層保留最近 10 個 envelope view 供歷史瀏覽（`_recent_envelopes`）
- ✅ TUI 左右鍵瀏覽歷史 envelope (#125)
- 待做：session replay 時重建 envelope timeline
- 待做：API 查詢最近 N 個 envelope
- 待做：Web UI 或 observability API

---

## TUI 實作分期

### TUI Phase A：最小可用版本 ✅ (#119)

- `SwarmDashboard` → `ExecutionDashboard`
- 顯示當前 envelope header（id / node count / parallel groups / elapsed）
- 顯示 levels 與 node state（icon + colour）
- 顯示最近完成 envelope 摘要

### TUI Phase B：節點詳情 + Grant 可視化 ✅ (#125)

- 上下選取 node，展開 detail pane（trust / capabilities / state history / auth info）
- Node 對應的 scope grant 狀態（ONCE / SCOPE TTL 剩餘 / AUTO）顯示於 detail
- `BudgetPanel` 整合 active grants 指示器（格式：`grants: 2 active · next expiry 18m`），依 TTL 變色
- 等待確認的 node 標記為 `⏳ awaiting confirm`（橙色），confirm graph 由 lifecycle events 驅動即時更新
- Lease 到期 toast 通知（`app.notify()`，5 秒自動消失）
- StatusBar 已移除，原有內容整合至 BudgetPanel

### TUI Phase C：歷史檢視 ✅ (#125) / replay 待做

- 左右鍵瀏覽最近 N 個 envelope（session 層保留最近 10 個）
- 按 turn / envelope 切換
- 未來可銜接 time-travel / session replay

---

## Discord 實作分期

### Discord Phase A：batch snapshot ✅ (#119)

- `status_msg` 改為 envelope snapshot（level list + state icons）
- 同步顯示 level 與狀態，debounce 0.5s
- `_ConfirmView` 支援四按鈕（Allow / Lease / Auto / Deny）
- SCOPE / AUTO 決策後發送 follow-up TTL / grant 說明訊息

### Discord Phase B：envelope trail + grant 管理 + summary ✅ (#127)

- Completed envelope 凍結為永久訊息，不再被後續 envelope 覆蓋，thread 保留完整執行軌跡
- Think summary（`ThinkCollapsed`）發送為獨立持久訊息，不再被 tool_buf 編輯覆蓋
- `/scope list` / `/scope revoke <id>` / `/scope clear` 指令
- `/summary` 指令：三段式切換（`off` / `on` 精簡一行 / `detail` Embed），預設 `on`
- Turn summary 精簡一行格式：`✓ N envelopes · M actions · X.Xs · grants N active`
- Detail 模式使用 Discord Embed 顯示完整欄位（envelopes / actions / failures / elapsed / grants）
- 單層 envelope 不再顯示冗餘 `L0` 前綴，僅多層時顯示 `L0` / `L1`
- Discord 到期前提醒有意不做（30 分鐘 lease 噪音大於價值）

### Discord Phase C：thread history query

可考慮新增 slash command：

- `/exec`
- `/exec recent`
- `/exec last`

用於查詢最近 envelope 摘要。

---

## UX 命名建議

為了讓語義更接近 Loom 的精神，建議避免過度 generic 的命名。

### 推薦命名

- `Execution`
- `Envelope`
- `Action`
- `Control Surface`
- `Audit Detail`

### 不推薦命名

- `Todo`
- `Task Board`
- `Project Graph`
- `Workflow Canvas`

### TUI Tab 命名建議

- `Art`
- `Exe`
- `Bgt`

或保留完整語義：

- `Artifacts`
- `Execution`
- `Budget`

---

## 驗收標準

當以下條件成立，代表第一階段規劃達標：

### 功能面

- ✅ TUI 可看到以 envelope 為單位的 execution 視圖
- ✅ Discord 可看到 batch snapshot，而非單純 tool timeline
- ✅ 平行節點可被辨識為同層
- ✅ 失敗、回滾、denied、paused 狀態可明確辨識
- ✅ 確認提示支援 y/s/a/N 四選項，TUI inline 呈現，Discord 四按鈕呈現
- ✅ active scope grant 可在 TUI（BudgetPanel）/ Discord（`/scope list`）持續可見

### 認知面

使用者能從畫面直接理解：

- 這不是普通 chat tool log
- Loom 正在執行的是受控 action system
- DAG 是執行結構，而不是單純任務清單

### 架構面

- envelope 成為 stream event 與 UI 的第一級實體
- `ToolBegin / ToolEnd` 保持相容
- TUI 與 Discord 共用同一 execution projection

---

## 風險與取捨

### 1. 過早追求 full graph 會讓畫面變難懂

處理方式：

- 預設顯示 level list
- graph detail 只在展開模式出現

### 2. Discord 的訊息長度與 edit 頻率有限

處理方式：

- 僅顯示 compact snapshot
- 避免每個 lifecycle transition 都 edit

### 3. 當前 DAG 還不是完整 cognition DAG

處理方式：

- 文件與 UI 明確稱其為 `execution graph`
- 不把它誤包裝成完整 thought graph

### 4. 舊有 ActivityLog / SwarmDashboard 心智模型會被替換

處理方式：

- 保留歷史時間軸作為次要區塊
- 漸進升級，不一次推翻整個右側欄

---

## 後續延伸

本規劃的 TUI / Discord 可視化已大致完成。下一階段方向：

- **TaskGraph Agent-Driven Construction (#128)**：讓 agent 主動建立帶依賴的 TaskGraph，解鎖多層 envelope 執行與長任務能力
- **Task Graph Governance (#44)**：node metadata / auditing / rollback，依賴 #128
- REST / MCP 暴露 execution history
- Web dashboard
- autonomy daemon 的 execution stream
- sub-agent / multi-agent swarm envelope 視圖
- time-travel 與 envelope replay
- ~~Scope Grant 儀表板~~ → ✅ 已由 BudgetPanel + `/scope` 指令實現
- **Grant Audit Trail**：每次 SCOPE / AUTO grant 的建立、使用與到期紀錄寫入 memory.db，支援事後 audit

---

## 實作進度

1. ✅ 補齊 `ExecutionEnvelope` 的真實建立與關聯 (#106)
2. ✅ 定義 `ExecutionEnvelopeView` 與新 stream events (#106)
3. ✅ TUI `SwarmDashboard` → `ExecutionDashboard` (#119)
4. ✅ Discord `status_msg` 升級為 envelope snapshot (#119)
5. ✅ TUI Phase B：節點詳情 + Grant 可視化 + Confirm Graph (#125)
6. ✅ Discord Phase B：envelope trail + `/scope` + `/summary` (#127)
7. 🔲 多層 envelope 執行（依賴 #128 TaskGraph Agent-Driven Construction）
8. 🔲 replay / history query / API

---

## 結論

Loom 下一階段的 UI/UX 升級，應該把 `DAG` 從「不夠直觀的結構圖」轉化為：

- **可被理解的 control surface**
- **可被稽核的 execution graph**
- **可被延伸的 harness observability substrate**

真正要被看見的，不是節點本身，而是：

- Loom 如何控制 action
- Loom 如何管理 consequence
- Loom 如何讓 agent execution 變得可觀察、可審計、可回溯

這才是 harness engineering 應該被 UI 放大的核心價值。
