# Scope-Aware Permission 規劃

本文件定義 Issue #45 的底層設計方向：將 Loom 目前以「工具名稱」為主的授權模型，提升為以「實際資源範圍」為主的 scope-aware permission model。

目標不是立刻做完整 UX，而是先把後續 `#88 approval lease` 與 `#47 autonomy legitimacy` 會依賴的底層 contract 定清楚。

---

## 問題定義

目前 Loom 的 `PermissionContext` 只記錄：

- 這個 session 授權過哪些工具名稱
- `run_bash` 是否開啟 `exec_auto`

這種模型足以表達：

- `write_file` 有沒有被批准
- `run_bash` 這一輪要不要每次詢問

但不足以表達：

- `write_file` 只能寫 `doc/`，不能寫 `loom/`
- `run_bash` 只能在 workspace 內執行，且不能碰絕對路徑
- `fetch_url` / `web_search` 只能觸及指定 domain 或 provider
- `spawn_agent` 只能再開 1 個 agent，而不是無上限展開
- autonomy 在評估「能不能合法地自動做」時，是否有已存在的授權邊界可依附

換句話說，現在的模型知道「哪個工具被允許」，卻不知道「允許到什麼範圍」。

---

## 目標

Issue #45 應完成以下底層能力定義：

1. `PermissionContext` 能表達具體 scope grant，而非只有 tool-name authorization。
2. `ToolDefinition` 能宣告工具需要哪些 scope，且允許根據實際參數動態解析。
3. `BlastRadiusMiddleware` 能在 tool 執行前，比對「本次請求 scope」與「當前已授權 scope」。
4. 當 agent 嘗試超出既有 scope 時，系統能產生結構化的 scope-expansion request，而不是只有模糊的 yes/no 確認。
5. 整個設計必須與現有 trust-level / capability / lifecycle 架構相容，且可漸進導入。

---

## 非目標

以下內容不屬於 Issue #45 的範圍：

- 不定義最終使用者要看到的 lease UX、TTL、revocation 呈現方式。
  這是 Issue #88。
- 不在 autonomy planner 內加入 legitimacy reasoning、risk justification、probe-vs-execute 決策。
  這是 Issue #47。
- 不把 workspace sandbox 升級成 OS 級隔離機制。
  `strict_sandbox` 仍只是其中一個 scope signal，不是完整沙箱。
- 不要求所有工具一次到位支援完整 scope 抽象。
  初期只需要先覆蓋高風險、最常用的幾類工具。

---

## 與 #88 / #47 的邊界

### #45 負責什麼

`#45` 只定義「可計算、可驗證、可審計」的 permission substrate：

- grant 長什麼樣
- request 長什麼樣
- middleware 如何比較
- tool registry 如何宣告 scope
- scope 超界時系統如何表達 expansion request

### #88 之後接什麼

`#88` 再決定這些底層結構如何被包裝成 operator-facing approval lease：

- `manual / scoped / auto` 模式
- lease TTL
- lease visibility / revoke UI
- current turn / current skill / current session 綁定規則

### #47 之後接什麼

`#47` 再把 scope-aware permission 當成 legitimacy gate 的一部分：

- 若行動超出已知 scope，planner 應傾向 `NOTIFY / HOLD`
- 若只需 read-only probe，planner 應優先選擇 scope 較小的操作
- 過去被拒絕的 scope expansion 可成為 legitimacy 反例訊號

---

## 設計原則

### 1. Resource-first，不是 tool-first

授權判斷的核心應從：

- `write_file` 是否被批准

轉向：

- 此次寫入的路徑 prefix 是否被批准
- 此次 shell command 的工作區 / 路徑觸及範圍是否被批准
- 此次網路請求的 destination 是否被批准

工具名稱仍然有用，但只應作為 grant 的其中一維，不應是全部。

### 2. Scope 必須可由實際參數推導

真正的 blast radius 不是由工具名稱決定，而是由工具參數決定。

例如：

- `write_file(path="doc/x.md")` 與 `write_file(path="loom/core/session.py")`
- `run_bash(command="pytest tests")` 與 `run_bash(command="cat /etc/passwd")`

都不能只因為是同一工具就被視為相同授權。

### 3. 授權必須單調擴張，不能隱式放大

若既有 scope 是：

- 可寫 `doc/`

那麼寫 `doc/45-plan.md` 應直接放行，但寫 `loom/` 應明確被視為 expansion。

系統不能因為同一工具曾被批准，就默默把新範圍也算進去。

### 4. 與現有 trust level 相容，而非取代

`TrustLevel` 仍回答「需要多強的人類控制」；
scope model 回答「即使已經允許，允許的邊界到哪裡」。

兩者是正交維度：

- trust level 決定是否需要確認
- scope 決定確認後能覆蓋哪些資源

### 5. 先定義結構化結果，再談 UX 包裝

底層應先能產生：

- 已授權
- 需要首次授權
- 需要 scope expansion
- 明確拒絕

等 verdict。`#88` 再決定這些 verdict 如何被 UI 呈現成 lease 流程。

---

## 核心資料模型

以下是建議的底層抽象。

### ScopeGrant

`ScopeGrant` 代表「目前已批准的一塊資源邊界」。

```python
@dataclass
class ScopeGrant:
    resource: str                 # path / network / exec / agent / mutation
    action: str                   # read / write / execute / spawn / mutate
    selector: str                 # 例如 path prefix、domain、tool target
    constraints: dict[str, Any]   # 例如 max_calls、workspace_only、budget
    source: str                   # manual_confirm / lease / auto / system
```

範例：

- `resource="path", action="write", selector="/workspace/doc/"`
- `resource="network", action="connect", selector="api.openai.com"`
- `resource="exec", action="execute", selector="workspace", constraints={"absolute_paths": "deny"}`
- `resource="agent", action="spawn", selector="default", constraints={"remaining_budget": 1}`

### ScopeRequest

`ScopeRequest` 代表單次 tool call 實際需要的 scope。

```python
@dataclass
class ScopeRequest:
    tool_name: str
    capabilities: ToolCapability
    resources: list[ScopeGrant]
    metadata: dict[str, Any] = field(default_factory=dict)
```

注意：`ScopeRequest` 與 `ScopeGrant` 結構相近，但語意不同：

- grant = 已授權邊界
- request = 本次請求需求

### ScopeDiff

`ScopeDiff` 代表 request 相對於當前 grant 缺少了哪些部分。

```python
@dataclass
class ScopeDiff:
    missing: list[ScopeGrant]
    covered: list[ScopeGrant]
    reason: str
```

這是 `#45` 很重要的中介層，因為 `#88` 的 lease prompt 與 `#47` 的 legitimacy 判斷都應基於這個 diff，而不是自己重新計算。

### PermissionVerdict

建議把 middleware 的授權結果提升為結構化 verdict：

```python
class PermissionVerdict(Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    EXPAND_SCOPE = "expand_scope"
    DENY = "deny"
```

語意如下：

- `ALLOW`: 既有 grant 已完整覆蓋本次 request
- `CONFIRM`: 目前沒有相應 grant，但這是第一次授權，可走一般確認
- `EXPAND_SCOPE`: 有既有 grant，但本次超出範圍，需要擴張批准
- `DENY`: 政策或 trust gate 明確不允許

### PermissionContext

`PermissionContext` 應從目前的：

- `session_authorized: set[str]`
- `exec_auto: bool`

擴充為：

```python
@dataclass
class PermissionContext:
    session_id: str
    grants: list[ScopeGrant] = field(default_factory=list)
    legacy_authorized_tools: set[str] = field(default_factory=set)
    exec_auto: bool = False
```

並新增這類 API：

```python
def grant(self, scope: ScopeGrant) -> None: ...
def grant_many(self, scopes: list[ScopeGrant]) -> None: ...
def revoke_matching(self, predicate) -> None: ...
def evaluate(self, request: ScopeRequest, trust_level: TrustLevel) -> PermissionVerdict: ...
def diff(self, request: ScopeRequest) -> ScopeDiff: ...
```

### 為何保留 legacy_authorized_tools

因為 `#45` 應該是漸進導入，而不是一次把所有 call site 打斷。

建議遷移策略：

- 未提供 `scope_resolver` 的工具，暫時維持舊行為
- 已提供 `scope_resolver` 的工具，優先走 scope-aware path
- 全面穩定後，再考慮淘汰純 tool-name 授權

---

## Tool Registry 合約變更

Issue #45 的關鍵不只是 `PermissionContext`，還包含工具如何宣告自己的 scope 需求。

建議在 `ToolDefinition` 增加兩個欄位：

```python
scope_descriptions: list[str] = field(default_factory=list)
scope_resolver: Callable[[ToolCall], ScopeRequest] | None = None
```

### `scope_descriptions`

人類可讀的摘要，用於：

- audit log
- confirm prompt
- 文件與除錯輸出

範例：

- `writes under requested workspace path`
- `executes shell commands within workspace sandbox`
- `spawns one sub-agent`

### `scope_resolver`

由工具作者提供的動態解析器，負責把 `ToolCall.args` 轉成 `ScopeRequest`。

這是 `#45` 的核心，因為 scope-aware authorization 不可能只靠靜態 schema 完成。

範例：

```python
def write_file_scope(call: ToolCall) -> ScopeRequest:
    path = normalize_workspace_path(call.args["path"])
    return ScopeRequest(
        tool_name=call.tool_name,
        capabilities=call.capabilities,
        resources=[
            ScopeGrant(
                resource="path",
                action="write",
                selector=str(path.parent),
                constraints={},
                source="request",
            )
        ],
    )
```

---

## 優先覆蓋的工具類型

`#45` 不需要一次把所有工具 scope 化，但至少應先定義下列工具的 resolver contract。

### 1. `write_file`

request 維度：

- 寫入目標 path prefix
- 是否覆蓋既有檔案

### 2. `run_bash`

request 維度：

- 執行位置是否限於 workspace
- 命令是否包含絕對路徑
- 是否含明顯 mutation 特徵

這裡不要求 `#45` 做完美 shell 靜態分析，但至少要把既有 `exec_escape_fn` 的能力整合進 scope request。

### 3. `fetch_url` / `web_search`

request 維度：

- destination domain 或 provider 類型
- 是否允許任意對外連線，或僅允許特定 service

### 4. `spawn_agent`

request 維度：

- spawn 次數
- child agent 類型
- 是否允許帶入工具能力升級

### 5. `memorize` / `relate`

request 維度：

- mutation target 類別
- 是否屬於 persistent state mutation

---

## Middleware 流程變更

### 現況

目前 `BlastRadiusMiddleware` 流程是：

1. 用 `tool_name + trust_level` 查 `PermissionContext`
2. 若已授權則放行
3. 否則呼叫 `confirm_fn(call)`
4. 若使用者批准，對一般 `GUARDED` 工具記住 `tool_name`

### 問題

這個流程不知道本次實際 scope，因此：

- 無法判斷是否為 scope expansion
- 無法給出具體「你要批准的是哪個路徑 / domain / budget」
- 無法把 approval 壓縮成可複用的、明確有邊界的 grant

### 建議流程

新的 `BlastRadiusMiddleware` 應變成：

1. 從 `ToolRegistry` 讀出該工具的 `scope_resolver`
2. 將 `ToolCall` 解析成 `ScopeRequest`
3. 寫入 `call.metadata["scope_request"]`
4. 由 `PermissionContext.evaluate()` 算出 verdict
5. 依 verdict 分流：
   - `ALLOW` → 直接執行
   - `CONFIRM` → 發出一般授權請求；批准後把 request 轉成 grant
   - `EXPAND_SCOPE` → 發出 scope-expansion 請求；批准後僅補上缺少部分
   - `DENY` → 回傳 `permission_denied`

### 為何 scope 解析要發生在 BlastRadius，而不是等到 PREPARED

語意上，scope resolution 很像 preparation；
但實務上，授權判斷必須先知道 scope，否則無法決定要不要 prompt。

因此建議：

- scope resolution 在 `BlastRadiusMiddleware` 先執行
- 結果寫入 `call.metadata`
- `LifecycleGateMiddleware` 與 audit layer 再把它當成 PREPARED 階段的一部分記錄

這樣可以同時滿足：

- 授權時序正確
- lifecycle audit 仍保留完整上下文

---

## 結構化 HITL 介面

目前 `confirm_fn` 只收 `ToolCall -> bool`，這對 scope-aware 流程不夠。

Issue #45 應至少把底層 prompt payload 抽象化，即使 UI 還沒升級完整 lease 體驗。

建議新增：

```python
@dataclass
class PermissionPrompt:
    kind: str                    # confirm_tool / expand_scope
    tool_name: str
    trust_level: TrustLevel
    request: ScopeRequest | None
    diff: ScopeDiff | None
    summary: str
```

以及：

```python
@dataclass
class PermissionDecision:
    allowed: bool
    grant_scopes: list[ScopeGrant] = field(default_factory=list)
```

### 相容策略

為避免一次改爆所有平台層，建議分兩階段：

#### Phase 1

- middleware 內部先能建立 `PermissionPrompt`
- 若平台只提供舊版 `confirm_fn(call) -> bool`
  - 則退化成一般 yes/no 確認
  - 若批准，middleware 以預設規則將 request 或 diff 寫回 grant

#### Phase 2

- CLI / TUI / Discord 改接 `PermissionPrompt`
- `#88` 再把這個 prompt 包裝成真正 lease UX

---

## `exec_auto` 的位置

`exec_auto` 不應直接消失，但其語意應從「shell 特例開關」降級為「一種 grant source」。

也就是說：

- 現在：`exec_auto=True` 代表 `run_bash` 幾乎都跳過確認
- 未來：`exec_auto` 代表「系統替 session 注入一個受限 exec grant」

例如：

```python
ScopeGrant(
    resource="exec",
    action="execute",
    selector="workspace",
    constraints={"absolute_paths": "deny"},
    source="exec_auto",
)
```

這樣可以把現有特例納回同一 permission substrate，而不是永久保留分叉邏輯。

---

## 與 Action Lifecycle 的整合

Issue #45 不需要重寫 lifecycle，但應規劃好資料流：

- `BlastRadiusMiddleware` 解析出的 `scope_request` 寫入 `call.metadata`
- 授權 verdict 與 expansion reason 也寫入 `call.metadata`
- `LifecycleMiddleware` / `LifecycleGateMiddleware` 可在 action record 中保留：
  - resolved scope
  - granted scope
  - missing scope
  - authorization reason

這對後續兩件事很重要：

- `#88` 需要 audit 與 revoke 顯示
- `#47` 需要讀取過去哪些 scope expansion 常被拒絕

---

## 對 Autonomy 的前置價值

Issue #45 本身不改 planner，但它會提供 `#47` 必須依賴的可計算訊號：

- 這個 planned action 是否完全落在既有 grant 內
- 它需要的是首次授權，還是 scope expansion
- expansion 的幅度是小幅（例如多一個子目錄）還是大幅（例如從 workspace 內跳到 `/etc`）

沒有這層資料，`autonomy legitimacy` 只能停留在抽象 trust-level 判斷，無法做真正細緻的合法性評估。

---

## 遷移策略

### Phase A: Additive substrate

- 新增 `ScopeGrant / ScopeRequest / ScopeDiff / PermissionVerdict`
- `PermissionContext` 支援 grant-based API
- 舊 `authorize(tool_name)` 行為保留

### Phase B: High-risk tool adoption

- 先為 `write_file / run_bash / spawn_agent / fetch_url / web_search` 提供 resolver
- `BlastRadiusMiddleware` 優先對有 resolver 的工具做 scope-aware evaluation

### Phase C: Structured prompts

- 平台層開始接 `PermissionPrompt`
- scope expansion 與 first-time approval 可被明確區分

### Phase D: Legacy contraction

- 減少純 `tool_name` 授權依賴
- `exec_auto` 收斂為 grant injection
- 文件與測試全面改以 scope-aware model 為主

---

## 測試要求

Issue #45 開始實作時，至少應補下列測試：

1. `PermissionContext.diff()` 的單元測試
2. `write_file` path-prefix 覆蓋 / 超界測試
3. `run_bash` workspace-only / absolute-path escape 測試
4. `spawn_agent` budget 耗盡測試
5. `BlastRadiusMiddleware` 的 `ALLOW / CONFIRM / EXPAND_SCOPE / DENY` 分支測試
6. lifecycle audit metadata 是否記錄 resolved scope

---

## Review 出口條件

若要說 `#45` 規劃完成、可以進 PR review，至少應滿足：

1. 底層資料模型已明確，且與現有 `PermissionContext` / `ToolDefinition` / middleware 可以對接。
2. 已明確切開 `#45` 與 `#88` 的責任邊界，沒有把 lease UX 偷渡進底層 issue。
3. 已明確切開 `#45` 與 `#47` 的責任邊界，讓 legitimacy model 建立在 scope signal 上，而不是反過來。
4. 已定義漸進遷移路徑，避免一次重寫整個 harness。
5. 已列出第一批必須支援 scope resolver 的工具類型與對應測試面。

---

## 結論

`#45` 的本質不是「多加幾個 permission 欄位」，而是把 Loom 的授權模型從：

- 對工具做一次性的粗粒度批准

升級為：

- 對真實資源範圍做可重用、可審計、可擴張的邊界批准

只有先把這層 substrate 建好，`#88` 才有辦法做出真正可用的 approval lease UX，`#47` 也才有足夠具體的 legitimacy signal 可以判斷 agent 的自主行為是否合理。
