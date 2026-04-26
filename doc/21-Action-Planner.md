# Action Planner（更新版）

> 本文件取代原有描述。`DecisionPipeline` 獨立類別在實作中**不存在**，其職責已整合至 `ActionPlanner.handle()`。

---

## ActionDecision 枚舉

```python
class ActionDecision(Enum):
    EXECUTE  = "execute"   # SAFE：直接執行
    NOTIFY   = "notify"   # GUARDED + notify=true：通知用戶，等待確認
    HOLD     = "hold"     # CRITICAL：強制確認
    SKIP     = "skip"     # 排程已停用，無意義動作
```

---

## ActionPlanner.handle() — 決策邏輯

```python
async def handle(trigger, fire_context) -> PlannedAction:
    # 1. Trust level 解析
    trust_level = _parse_trust(trigger.trust_level)

    # 2. Context 組裝（包含 recent_facts）
    context = dict(fire_context)
    context["allowed_tools"] = getattr(trigger, "allowed_tools", [])
    context["scope_grants"]  = getattr(trigger, "scope_grants", [])
    context["attach_outputs"] = getattr(trigger, "attach_outputs", [])

    # 3. Decision 映射
    if not trigger.enabled:
        decision = ActionDecision.SKIP
    elif trust_level == TrustLevel.SAFE:
        decision = ActionDecision.EXECUTE
    elif trust_level == TrustLevel.GUARDED:
        decision = ActionDecision.NOTIFY if trigger.notify else ActionDecision.EXECUTE
    else:  # CRITICAL
        decision = ActionDecision.HOLD

    return PlannedAction(decision=decision, ...)
```

---

## Decision → Confirmed Action 映射

| Decision | _execute_plan() 行為 |
|----------|---------------------|
| `SKIP` | 什麼都不做，直接返回 |
| `EXECUTE` | `_run_agent(plan)` 直接執行 |
| `NOTIFY` | ConfirmFlow.ask()，60s 超時，TIMEOUT → 發送 INFO 通知後跳過 |
| `HOLD` | ConfirmFlow.ask()，300s 超時，TIMEOUT → 不執行（強制確認失敗 = 不執行）|

---

## PlannedAction 結構

```python
@dataclass
class PlannedAction:
    trigger_name: str
    intent: str
    decision: ActionDecision
    trust_level: TrustLevel
    context: dict[str, Any]   # allowed_tools, scope_grants, attach_outputs, recent_facts
    prompt: str               # _build_prompt() 產出的 LLM prompt
```

---

## 與 AutonomyDaemon 的整合

`AutonomyDaemon._on_trigger_fire()` 攔截每個觸發：

```python
async def _on_trigger_fire(self, trigger, context):
    plan = await self._planner.handle(trigger, context)
    await self._execute_plan(plan)
```

完整的決策流程見 [22-Autonomy-Daemon.md](22-Autonomy-Daemon.md)。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*