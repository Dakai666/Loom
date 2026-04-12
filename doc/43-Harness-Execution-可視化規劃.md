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

### 待規劃的 UI 工作

#### 1. Scope Grant 可視化面板

目前 `/scope list` 只在 CLI 輸出 Rich table，TUI 與 Discord 尚未有持續可見的 grant 狀態面板。

建議：
- **TUI**：在 `WorkspacePanel` 或 Status Bar 增加 `Grants` 指示，顯示當前 active lease 數與最近到期時間
- **TUI**：Lease grant 可在 Execution Dashboard 的 selected node detail 顯示剩餘 TTL
- **Discord**：考慮 `/scope` 等效 slash command，讓 Discord 使用者也能查詢與撤銷 grants

#### 2. Grant 到期可視化

當 scope lease 即將到期（例如剩餘 < 5 分鐘），Control Surface 應主動提示。

設計選項：
- TUI status bar 以顏色警示（green → yellow → expired dim）
- Discord 可在 lease 到期前發一則 ephemeral 提醒訊息

#### 3. Confirm Widget 與 Execution Graph 的整合

目前 `InlineConfirmWidget` 獨立嵌入 MessageList，與 Execution Dashboard 的 node state 視圖沒有連結。

長遠設計方向：
- 確認等待中的 node 在 Execution Graph 中標記為 `⏳ awaiting_confirm`
- 使用者在 graph view 中點選 blocked node，直接展開確認選項
- 確認後 node state 在 graph 中即時更新

---

## 當前系統現況

### 已具備能力

- `TaskGraph` 與 `ExecutionPlan` 已能表示 level-based DAG
- `TaskScheduler` 已能執行同層並行
- `ActionState` 已能表示完整 control-first lifecycle
- TUI 已有 `WorkspacePanel` / `SwarmDashboard`
- Discord 已有單一 `status_msg` 的 edit-based 顯示策略
- **（v0.2.9.5 新增）** `ConfirmDecision` enum（ONCE / SCOPE / AUTO / DENY）取代 bool 確認流程；`_normalize_decision()` 維持向下相容
- **（v0.2.9.5 新增）** TUI `InlineConfirmWidget` 支援 `y/s/a/N` 四選項 inline 確認，含 hint 文字與各 ConfirmDecision 對應按鈕
- **（v0.2.9.5 新增）** Discord `_ConfirmView` 支援四按鈕（Allow / Lease / Auto / Deny）；SCOPE / AUTO 決定後自動發送 follow-up TTL / grant 說明訊息
- **（v0.2.9.5 新增）** `ScopeGrant.valid_until` 欄位與自動過期過濾；`PermissionContext.purge_expired()` 清理
- **（v0.2.9.5 新增）** `/scope list / revoke / clear` CLI 指令，可查詢與撤銷當前 session 的 scope grants
- **（v0.2.9.5 新增）** Self-termination guard：`loom/core/security/self_termination_guard.py` 封鎖以工具呼叫方式終止 Loom 自身行程的嘗試

### 現況限制

#### 1. 前端收到的是平面事件，不是 graph event

目前 UI 主要消費：

- `ToolBegin`
- `ToolEnd`
- `ActionStateChange`

缺少：

- `EnvelopeStarted`
- `GraphPlanned`
- `NodeLinked`
- `EnvelopeCompleted`

#### 2. `_dispatch_parallel()` 只把工具當成同層獨立節點

目前 parallel dispatch 雖使用 `TaskGraph`，但所有工具都被視為同一層無依賴節點。

因此現在 UI 最誠實的說法是：

- 「parallel dispatch graph」

而不是：

- 「full agent planning graph」

#### 3. `ExecutionEnvelope` 尚未成為前端第一公民

概念已存在，但尚未完整成為：

- stream event 主體
- UI 顯示單位
- DB / replay / REST 查詢的一級實體

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

每個 turn 維持兩種訊息：

- `status_msg`
  - 只做 edit
  - 用於 envelope / execution 狀態
- `response_msg`
  - send-once
  - 保留 LLM 文本與 Markdown 顯示品質

## Status Message 結構

### 執行中

```text
-# Envelope e42 · 3 actions · 1 parallel group
-# L0  ✓ recall
-# L1  ⟳ read_file   ✓ list_dir
-# L2  · write_file
```

### 失敗時

```text
-# Envelope e42 · failed
-# L0  ✓ recall
-# L1  ✗ read_file (permission denied)
-# rollback 0 · blocked 1
```

### 完成時

```text
-# Envelope e42 · completed
-# 4 actions · 1 failed · 1 reverted · 2.4s
```

## Discord Embed Summary

建議欄位：

- `Envelope`
- `Actions`
- `Parallelism`
- `Failures`
- `Rollbacks`
- `Longest Action`
- `Paused / Redirected`

---

## 資料模型設計

### 新增 UI 專用 ViewModel

建議在 `loom/core/events.py` 或相鄰模組定義可序列化 view model。

```python
@dataclass
class ExecutionNodeView:
    node_id: str
    call_id: str
    action_id: str | None
    tool_name: str
    level: int
    state: str
    trust_level: str
    capabilities: list[str]
    args_preview: str = ""
    duration_ms: float = 0.0
    error_snippet: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ExecutionEnvelopeView:
    envelope_id: str
    session_id: str
    turn_index: int
    status: str
    node_count: int
    parallel_groups: int
    levels: list[list[str]]
    nodes: list[ExecutionNodeView]
```

### 新增 stream events

建議新增：

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
- 新 UI 優先使用 envelope events
- 舊 UI 可暫時維持不變

---

## 後端整合設計

### Phase 1：讓 `ExecutionEnvelope` 成為真實執行單位

必做事項：

- 在每個 `tool_use` 批次建立 `ExecutionEnvelope`
- 每個 action node 與 `call_id`、`action_id` 對齊
- batch 完成時標記 envelope complete
- 將 envelope 與 `ActionRecord` 綁定

### 成果

- DB 中 `action_records.envelope_id` 不再為空洞概念
- UI 能用 envelope 為單位呈現

### Phase 2：建立 graph-aware projection

在 Session 層建立 projection，把：

- tool uses
- action lifecycle updates
- task levels

整合成可直接丟給 TUI / Discord 的 `ExecutionEnvelopeView`

### 注意

Projection 層只做視圖整形，不應污染 middleware 核心邏輯。

### Phase 3：歷史與 replay

完成後可延伸支援：

- session replay 時重建 envelope timeline
- API 查詢最近 N 個 envelope
- 後續 Web UI 或 observability API

---

## TUI 實作分期

### TUI Phase A：最小可用版本

目標：

- 不改整體版面結構
- 只替換 `SwarmDashboard` 內容模型

功能：

- 顯示當前 envelope header
- 顯示 levels 與 node state
- 顯示最近完成 envelope 摘要

### TUI Phase B：節點詳情 + Grant 可視化

功能：

- 選取 node
- 展開 detail pane / modal
- 顯示完整 lifecycle state history
- **（新增）** 顯示 node 對應的 scope grant 狀態（ONCE / SCOPE TTL 剩餘 / AUTO）
- **（新增）** Status Bar 或 WorkspacePanel 加入 active grants 指示器
  - 格式範例：`grants: 2 active · next expiry 18m`
- **（新增）** 等待確認的 node 在 Execution Graph 標記為 `⏳`，點選直接展開 InlineConfirmWidget

### TUI Phase C：歷史檢視與 replay

功能：

- 瀏覽最近 N 個 envelope
- 可按 turn / envelope 切換
- 未來可銜接 time-travel / session replay

---

## Discord 實作分期

### Discord Phase A：batch snapshot

功能：

- `status_msg` 改為 envelope snapshot
- 同步顯示 level 與狀態
- turn 完成後輸出 compact summary

**（v0.2.9.5 已完成）確認 UX 升級：**
- `_ConfirmView` 支援四按鈕（Allow / Lease / Auto / Deny）
- SCOPE / AUTO 決策後發送 follow-up TTL / grant 說明訊息

### Discord Phase B：summary embed + grant 管理

功能：

- 補 `Embed` 顯示 envelope 指標
- 顯示最慢節點、回滾、暫停、redirect
- **（新增）** Embed 加入 `Active Grants` 欄位，顯示本 session 的 scope lease 數與最近到期時間
- **（新增）** 考慮 `/scope` Discord slash command（parity with CLI `/scope list / revoke / clear`）

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

- TUI 可看到以 envelope 為單位的 execution 視圖
- Discord 可看到 batch snapshot，而非單純 tool timeline
- 平行節點可被辨識為同層
- 失敗、回滾、denied、paused 狀態可明確辨識
- **（v0.2.9.5 已達標）** 確認提示支援 y/s/a/N 四選項，TUI inline 呈現，Discord 四按鈕呈現
- **（待達標）** active scope grant 可在 TUI / Discord 持續可見，不只有 `/scope list` 查詢

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

本規劃完成後，可進一步支援：

- REST / MCP 暴露 execution history
- Web dashboard
- autonomy daemon 的 execution stream
- sub-agent / multi-agent swarm envelope 視圖
- time-travel 與 envelope replay
- **Scope Grant 儀表板**：將 active grants 以結構化方式呈現（到期時間、resource/action/selector、來源決策），讓使用者隨時掌握當前授權狀態
- **Grant Audit Trail**：每次 SCOPE / AUTO grant 的建立、使用與到期紀錄寫入 memory.db，支援事後 audit

---

## 建議實作順序

1. 補齊 `ExecutionEnvelope` 的真實建立與關聯
2. 定義 `ExecutionEnvelopeView` 與新 stream events
3. TUI `SwarmDashboard -> ExecutionDashboard`
4. Discord `status_msg` 升級為 envelope snapshot
5. 補 summary embed
6. 補 replay / history query / API

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
