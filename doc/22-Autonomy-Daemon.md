# Autonomy Daemon

AutonomyDaemon 是 Loom 的背景服務。它從 `loom.toml` 載入觸發器，在後台執行 `TriggerEvaluator`，並將觸發的 `PlannedAction` 交給 `ActionPlanner` → `ConfirmFlow` → Session 執行。

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
        )
        self._evaluator.register(trigger)

    return count
```

---

## 執行階段

```python
async def _execute_plan(self, plan: PlannedAction) -> None:
    if plan.decision == ActionDecision.SKIP:
        return

    if plan.decision == ActionDecision.EXECUTE:
        await self._run_agent(plan)
        return

    # NOTIFY 或 HOLD → 發送確認請求
    notif = Notification(
        type=NotificationType.CONFIRM,
        title=f"Loom autonomy: {plan.trigger_name}",
        body=plan.intent,
        timeout_seconds=60 if plan.decision == ActionDecision.NOTIFY else 300,
        thread_id=plan.context.get("notify_thread_id", 0),
    )
    result = await self._confirm.ask(notif)

    if result == ConfirmResult.APPROVED:
        await self._run_agent(plan)
    elif result == ConfirmResult.TIMEOUT and plan.decision == ActionDecision.NOTIFY:
        # GUARDED + notify 超時 → 跳過（不下放到 EXECUTE）
        await self._notify.send(Notification(
            type=NotificationType.INFO,
            title=f"Autonomy: {plan.trigger_name} skipped",
            body="No response within timeout — action was skipped.",
        ))

async def _run_agent(self, plan: PlannedAction) -> None:
    """透過 Session 的 stream_turn 執行自主任務"""
    if self._session is None:
        return
    try:
        output_chunks: list[str] = []
        async for event in self._session.stream_turn(
            plan.prompt, abort_signal=self._abort.signal
        ):
            if hasattr(event, "text") and isinstance(event.text, str):
                output_chunks.append(event.text)
        response = "".join(output_chunks).strip()
        thread_id = plan.context.get("notify_thread_id", 0)
        if response:
            await self._notify.send(Notification(
                type=NotificationType.REPORT,
                title=f"Autonomy result: {plan.trigger_name}",
                body=response[:1000],
                thread_id=thread_id,
            ))
    except Exception as exc:
        await self._notify.send(Notification(
            type=NotificationType.ALERT,
            title=f"Autonomy error: {plan.trigger_name}",
            body=str(exc),
            thread_id=plan.context.get("notify_thread_id", 0),
        ))
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

DreamingPlugin 將 `dream_cycle` 註冊為 SAFE 工具，可在 AutonomyDaemon 中透過 cron 觸發：

```toml
[[autonomy.schedules]]
name = "nightly_dream"
cron = "0 3 * * *"
intent = "執行 dream_cycle，探索記憶中的隱藏關聯"
trust_level = "safe"
notify = false
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

## loom.toml 實際支援的觸發器格式

```toml
[autonomy]
enabled  = true
timezone = "Asia/Taipei"   # IANA timezone，全域預設

[[autonomy.schedules]]
name         = "morning_briefing"
cron         = "0 0 * * *"   # UTC 00:00 = 台北 08:00
intent       = "生成每日晨報..."
trust_level  = "safe"
notify       = false
notify_thread = 1490024181994225744  # Discord thread ID

[[autonomy.triggers]]
name         = "deploy_done"
event        = "deployment_done"
intent       = "跑 smoke test 並回報結果"
trust_level  = "guarded"
notify       = true
```

> 完整的 `[[autonomy.triggers]]` 格式說明見 [37-loom-toml-參考.md](37-loom-toml-參考.md)。

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
| 任務執行 | 透過 Session streaming，**非** TaskScheduler |
| 結果通知 | `NotificationRouter` 發送 REPORT / ALERT |
| DreamingPlugin | Cron 排程 dream_cycle，探索隱藏關聯 |
| SelfReflectionPlugin | 每次 session 結束後分析模式寫入 loom-self 三元組 |
