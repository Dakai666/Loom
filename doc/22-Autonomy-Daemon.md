# Autonomy Daemon

AutonomyDaemon 是 Loom 的背景服務。它從 `loom.toml` 載入觸發器，在後台執行 `TriggerEvaluator`，並將觸發的 `PlannedAction` 交給 `ActionPlanner` → `ConfirmFlow` → Session 執行。

---

## 統一管線架構（v0.2.9.4）

自 v0.2.9.4 起，所有執行路徑（互動式、MCP、自主排程、子代理）都經過同一個 `MiddlewarePipeline`。行為差異由 `ToolCall.origin` 欄位驅動：

```
┌──────────────────────────────────────────��───────────────┐
│              MiddlewarePipeline（統一）                    │
│                                                          │
│  LifecycleMiddleware → TraceMiddleware                   │
│    → SchemaValidationMiddleware                          │
│    → BlastRadiusMiddleware (origin-aware)                │
│    → LifecycleGateMiddleware → tool executor             │
└──────────────────────────────────────────────────────────┘
         ↑             ↑              ↑            ↑
    interactive       mcp          autonomy     subagent
    (CLI/TUI/       (MCP客戶端)    (daemon)     (子代理)
     Discord)
```

### Origin 行為對照

| Origin | 遇到未授權工具時 | 說明 |
|--------|-----------------|------|
| `interactive` | 提示使用者確認 | CLI/TUI/Discord 互動 |
| `mcp` | 提示使用者確認 | MCP 客戶端呼叫 |
| `autonomy` | **DENY** | 無人可確認 — 只能花費既有授權 |
| `subagent` | **DENY** | 無人可確認 — 繼承父級授權 |

### 關鍵設計原則

**自主排程只能「花費」預先授權的權限，不能請求新權限。**

這代表：
- SAFE 工具（`read_file`, `list_dir`, `recall`, `load_skill`, `dream_cycle`, `memory_prune`）— 永遠可執行
- GUARDED 工具（`write_file`, `memorize`, `relate`, `run_bash`, `web_search`）— 必須在排程配置中透過 `allowed_tools` 或 `scope_grants` 預先授權
- CRITICAL 工具 — 永遠不可在自主排程中使用

---

## 排程授權配置

### allowed_tools — 工具名稱授權（Legacy 路徑）

列出排程執行期間需要的 GUARDED 工具名稱。這些授權在排程執行前注入，執行後自動撤銷。

```toml
[[autonomy.schedules]]
name          = "morning_briefing"
allowed_tools = ["write_file", "memorize"]
```

### scope_grants — 範圍授權（Scope-aware 路徑）

對有 `scope_resolver` 的工具（如 `write_file`, `run_bash`），可以指定更精確的資源範圍授權：

```toml
[[autonomy.schedules]]
name          = "morning_briefing"
allowed_tools = ["write_file", "memorize"]
scope_grants  = [
  { resource = "path", action = "write", selector = "news" },
]
```

#### scope_grants 欄位說明

| 欄位 | 說明 | 範例 |
|------|------|------|
| `resource` | 資源類型 | `"path"`, `"exec"`, `"network"`, `"agent"` |
| `action` | 動作類型 | `"write"`, `"execute"`, `"read"` |
| `selector` | 資源範圍 | `"news"` (目錄), `"workspace"` (工作區), `"*"` (全部) |
| `constraints` | 額外限制 (可選) | `{ absolute_paths = "deny" }` |

#### 常用 scope_grants 範例

```toml
# 允許寫入特定目錄
{ resource = "path", action = "write", selector = "news" }
{ resource = "path", action = "write", selector = "diary" }
{ resource = "path", action = "write", selector = "outputs" }

# 允許工作區內的 shell 命令（禁止絕對路徑）
{ resource = "exec", action = "execute", selector = "workspace", constraints = { absolute_paths = "deny" } }

# 允許特定網路域名
{ resource = "network", action = "read", selector = "api.search.brave.com" }
```

### 授權生命週期

```
排程觸發
  │
  ├─ 注入 allowed_tools → session.perm.authorize(tool_name)
  ├─ 注入 scope_grants  → session.perm.grant(ScopeGrant)
  │
  ├─ stream_turn(origin="autonomy")
  │     ├─ SAFE 工具 → 直接執行
  │     ├─ GUARDED + 有授權 → 執行
  │     └─ GUARDED + 無授權 → DENY（不提示，直接拒絕）
  │
  └─ 撤銷 allowed_tools + scope_grants
       （排程間不累積權限）
```

---

## Daemon 職責

```
loom.toml
  │
  ├─ [[autonomy.schedules]]   → CronTrigger
  └─ [[autonomy.triggers]]    → EventTrigger
           │
           ▼
AutonomyDaemon.load_config()
           │
           ▼
TriggerEvaluator.register(trigger)
           │
           ▼
TriggerEvaluator.run_forever(poll_interval=60s)
           │
           ├─ CronTrigger.should_fire()  ← 每分鐘評估
           ├─ EventTrigger             ← loom autonomy emit 觸發
           └─ ConditionTrigger         ← 每分鐘 poll
           │
           ▼
_planner.handle(trigger, context) → PlannedAction
           │
           ▼
_execute_plan(plan)
           │
           ├─ EXECUTE  → _run_agent(plan)
           ├─ NOTIFY   → NotificationRouter.send(CONFIRM) → wait → _run_agent or skip
           └─ HOLD     → NotificationRouter.send(CONFIRM) → wait 300s → skip
```

---

## 實際建構子（loom/autonomy/daemon.py）

```python
class AutonomyDaemon:
    def __init__(
        self,
        notify_router: NotificationRouter,
        confirm_flow: ConfirmFlow,
        loom_session=None,    # LoomSession：用於實際執行 prompt
        db=None,            # aiosqlite.Connection：用於 trigger_history 持久化
    ) -> None:
        self._notify = notify_router
        self._confirm = confirm_flow
        self._session = loom_session
        self._abort = AbortController()

        history = TriggerHistory(db) if db is not None else None
        self._planner = ActionPlanner(
            semantic_memory=getattr(loom_session, "_semantic", None)
                if loom_session else None,
        )
        self._evaluator = TriggerEvaluator(on_fire=self._planner.handle, history=history)
```

---

## loom.toml 配置載入

```python
def load_config(self, config_path: str | Path) -> int:
    """從 loom.toml 載入觸發器，返回註冊數量"""
    with open(path, "rb") as f:
        config = tomllib.load(f)

    autonomy_cfg = config.get("autonomy", {})
    if not autonomy_cfg.get("enabled", False):
        return 0

    for sched in autonomy_cfg.get("schedules", []):
        trigger = CronTrigger(
            name=sched["name"],
            intent=sched["intent"],
            cron=sched.get("cron", "0 9 * * 1-5"),
            timezone=sched.get("timezone", "UTC"),
            trust_level=sched.get("trust_level", "guarded"),
            notify=sched.get("notify", True),
            notify_thread_id=sched.get("notify_thread", 0),
            allowed_tools=sched.get("allowed_tools", []),
            scope_grants=sched.get("scope_grants", []),
        )
        self._evaluator.register(trigger)

    for evt in autonomy_cfg.get("triggers", []):
        trigger = EventTrigger(
            name=evt["name"],
            intent=evt["intent"],
            event_name=evt.get("event", evt["name"]),
            trust_level=evt.get("trust_level", "guarded"),
            notify=evt.get("notify", True),
            notify_thread_id=evt.get("notify_thread", 0),
            allowed_tools=evt.get("allowed_tools", []),
            scope_grants=evt.get("scope_grants", []),
        )
        self._evaluator.register(trigger)

    return count
```

---

## 執行階段

```python
async def _run_agent(self, plan: PlannedAction) -> None:
    """透過 Session 的 stream_turn 執行自主任務"""
    if self._session is None:
        return

    # 注入排程聲明的授權（執行後自動撤銷）
    from loom.core.harness.scope import ScopeGrant
    _added_tools = []
    for tool_name in plan.context.get("allowed_tools", []):
        if tool_name not in self._session.perm.session_authorized:
            self._session.perm.authorize(tool_name)
            _added_tools.append(tool_name)
    for g in plan.context.get("scope_grants", []):
        self._session.perm.grant(ScopeGrant(
            resource=g["resource"], action=g["action"],
            selector=g.get("selector", "*"),
            source=f"autonomy:{plan.trigger_name}",
        ))

    try:
        output_chunks = []
        async for event in self._session.stream_turn(
            plan.prompt,
            abort_signal=self._abort.signal,
            origin="autonomy",      # ← 統一管線入口
        ):
            if hasattr(event, "text") and isinstance(event.text, str):
                output_chunks.append(event.text)

        response = "".join(output_chunks).strip()
        if response:
            await self._notify.send(Notification(...))
    except Exception as exc:
        await self._notify.send(Notification(...))
    finally:
        # 撤銷臨時授權 — 排程間不累積權限
        for tool_name in _added_tools:
            self._session.perm.revoke(tool_name)
        _src = f"autonomy:{plan.trigger_name}"
        self._session.perm.revoke_matching(lambda g: g.source == _src)
```

---

## Runtime 控制

```python
async def start(self, poll_interval: float = 60.0) -> None:
    """啟動後台迴圈（阻塞，直到 stop() 被呼叫）"""
    run_task = asyncio.ensure_future(
        self._evaluator.run_forever(poll_interval=poll_interval)
    )
    abort_task = asyncio.ensure_future(wait_aborted(self._abort.signal))
    done, pending = await asyncio.wait(
        [run_task, abort_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

def stop(self) -> None:
    """發送中止信號，連帶中止 stream_turn"""
    self._abort.abort()
```

---

## Offline Dreaming（v0.2.5.3）

DreamingPlugin 將 `dream_cycle` 註冊為 SAFE 工具，可在 AutonomyDaemon 中透過 cron 觸發。
`relate`（寫入 relational triples）是 GUARDED，需在 `allowed_tools` 中聲明：

```toml
[[autonomy.schedules]]
name          = "nightly_dream"
cron          = "0 3 * * *"
intent        = "執行 dream_cycle，探索記憶中的隱藏關聯"
trust_level   = "safe"
notify        = false
allowed_tools = ["memorize", "relate"]
```

DreamingPlugin 程式碼位於 `loom/extensibility/dreaming_plugin.py`，由 `PluginRegistry.install_into(session)` 自動安裝。

---

## SelfReflectionPlugin（v0.2.5.3）

`SelfReflectionPlugin`（`loom/extensibility/self_reflection_plugin.py`）在 session 結束時自動觸發：

```python
class SelfReflectionPlugin(LoomPlugin):
    name = "self_reflection"
    version = "1.0"

    def tools(self) -> list[ToolDefinition]:
        return [reflect_self_tool]  # reflect_self tool (SAFE)

    def on_session_stop(self, session: object) -> None:
        """Session 結束時自動呼叫（背景非同步）"""
        asyncio.create_task(run_self_reflection(session))
```

每次 session 結束後，分析情節模式並寫入 RelationalMemory：
- `(loom-self, tends_to, <observation>)`
- `(loom-self, should_avoid, <observation>)`
- `(loom-self, discovered, <observation>)`

`MemoryIndex` 在下次 session 開始時展示 Self-Portrait。

---

## CLI 命令

```bash
loom autonomy start            # 前台啟動 daemon（blocking）
loom autonomy status           # 顯示已註冊的觸發器列表
loom autonomy emit <event>    # 發送事件，觸發 EventTrigger
```

> `start` 為 blocking（使用 `asyncio.wait`），適合 systemd/supervisord 管理；\
> `--daemon` flag 不存在，後台執行請透過 OS 服務管理工具。

---

## loom.toml 完整排程格式

```toml
[autonomy]
enabled  = true

[[autonomy.schedules]]
name          = "morning_briefing"
cron          = "0 0 * * *"          # UTC 00:00 = 台北 08:00
intent        = "生成每日晨報..."
trust_level   = "safe"
notify        = false
notify_thread = 1490024181994225744  # Discord thread ID
allowed_tools = ["write_file", "memorize"]
scope_grants  = [
  { resource = "path", action = "write", selector = "news" },
]

[[autonomy.triggers]]
name          = "deploy_done"
event         = "deployment_done"
intent        = "跑 smoke test 並回報結果"
trust_level   = "guarded"
notify        = true
allowed_tools = ["run_bash"]
scope_grants  = [
  { resource = "exec", action = "execute", selector = "workspace" },
]
```

> 完整的 `[[autonomy.triggers]]` 格式說明見 [37-loom-toml-參考.md](37-loom-toml-參考.md)。

---

## 排程設計檢查清單

設計新的自主排程時，依照以下清單確認：

1. **列出所有需要的工具** — 排程的 `intent` 會導致 LLM 呼叫哪些工具？
2. **分類工具信任層級** — SAFE 工具不需額外配置；GUARDED 工具必須列入 `allowed_tools`
3. **判斷是否需要 scope_grants** — 如果工具有 scope_resolver（write_file, run_bash, fetch_url, spawn_agent），聲明具體的資源範圍
4. **最小權限原則** — ���授權排程實際需要的工具和範圍
5. **測試** — 新排程先在互動模式中手動執行一次 intent，觀察呼叫了哪些工具

| 常見排程類型 | 通常需要的 allowed_tools |
|-------------|------------------------|
| 寫入報告 | `write_file`, `memorize` |
| 記憶維護 | （通常只需 SAFE 工具） |
| 夢境週期 | `memorize`, `relate` |
| Shell 腳本 | `run_bash` + scope_grant for exec |
| MCP 工具 | `minimax:text_to_image` 等 MCP 前綴工具 |

---

## 與 TaskScheduler 的關係

AutonomyDaemon **不直接持有** `TaskScheduler`。\
觸發後的任務透過 `LoomSession.stream_turn()` 執行，這是 LLM streaming 互動迴圈，非 Task Scheduler 的 DAG 執行。\
兩者是不同的執行模式：

| | Autonomy Daemon | Task Scheduler |
|---|---|---|
| 執行模式 | LLM streaming prompt | DAG `asyncio.gather` |
| 觸發來源 | cron / event / condition | LLM 拆解 |
| 整合點 | `_session.stream_turn()` | `_session._dispatch_parallel()` |
| 配置位置 | `loom.toml` | 程式呼叫 |

---

## 總結

| 功能 | 說明 |
|------|------|
| 觸發器載入 | 從 loom.toml 解析並註冊 CronTrigger + EventTrigger |
| 後台評估 | `TriggerEvaluator.run_forever()` 每分鐘檢查 cron/condition |
| 決策執行 | `ActionPlanner.handle()` → EXECUTE / NOTIFY / HOLD |
| 確認流程 | `ConfirmFlow.ask()` → APPROVED / DENIED / TIMEOUT |
| 授權注入 | `allowed_tools` + `scope_grants` 臨時注入，執行後撤銷 |
| 統一管線 | 所有 origin 共用同一 MiddlewarePipeline，行為由 origin 驅動 |
| 任務執行 | 透過 Session streaming，**非** TaskScheduler |
| 結果通知 | `NotificationRouter` 發送 REPORT / ALERT |
| DreamingPlugin | Cron 排程 dream_cycle，探索隱藏關聯 |
| SelfReflectionPlugin | 每次 session 結束後分析模式寫入 loom-self 三元組 |
