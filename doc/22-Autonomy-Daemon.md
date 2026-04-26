# Autonomy Daemon（更新版）

> 依據 v0.2.9.4 實際程式碼更新。

---

## ⚠️ 與舊版文件的差異

本文件取代原有的 `doc/22-Autonomy-Daemon.md`，以下是已確認的實作變更：

| 項目 | 舊版文件說法 | 實際實作 |
|------|------------|---------|
| DecisionPipeline 類別 | 存在獨立的 DecisionPipeline | **不存在**。決策邏輯整合在 `ActionPlanner.handle()` 內 |
| Decision enum | APPROVE / DENY / CONFIRM / DEFER | `ActionDecision`：EXECUTE / NOTIFY / HOLD / SKIP |
| `attach_outputs` | 未提及 | **新增欄位**：允許排程在結果中附帶 workspace 檔案 |
| Config tamper detection | 未提及 | **Issue #91**：loom.toml 改變時記錄 SHA-256 hash 並警告 |
| Scope grants revoke | 未提及 | **try/finally**：執行後自動 revoke scope grants |
| Recent facts assembly | 未提及 | `ActionPlanner.handle()` 主動載入 semantic memory 最近的事實 |

---

## 架構總覽

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
           ├─ evaluate_cron()      ← 每分鐘評估 CronTrigger
           ├─ emit(event_name)     ← 手動發送事件
           └─ poll_conditions()   ← 每分鐘評估 ConditionTrigger
           │
           ▼
ActionPlanner.handle(trigger, context)
           ├─ 載入 semantic memory 最近 5 筆事實（若有的話）
           ├─ 根據 trust_level 映射 decision
           └─ 回傳 PlannedAction
           │
           ▼
_on_trigger_fire() 拦截
           │
           ▼
_execute_plan(plan)
           ├─ SKIP  → 什麼都不做
           ├─ EXECUTE → _run_agent(plan)
           └─ NOTIFY/HOLD → ConfirmFlow.ask() → APPROVED → _run_agent()
           │
           ▼
_run_agent(plan)
           ├─ 注入 allowed_tools + scope_grants（try/finally 確保 revoke）
           ├─ stream_turn(origin="autonomy")
           ├─ 收集 output chunks
           ├─ _resolve_attachments() → 找出新檔案
           └─ 發送 REPORT notification（含附件）
```

**注意**：沒有 `DecisionPipeline` 這層。決策邏輯直接在 `ActionPlanner.handle()` 中。

---

## ActionPlanner.handle() — 完整實作邏輯

```python
async def handle(self, trigger, fire_context) -> PlannedAction:
    # 1. 解析 trust level
    trust_level = _parse_trust(trigger.trust_level)

    # 2. 組裝 context
    context = dict(fire_context)
    context["trigger_name"] = trigger.name
    context["intent"] = trigger.intent
    context["notify_thread_id"] = getattr(trigger, "notify_thread_id", 0)
    context["allowed_tools"] = getattr(trigger, "allowed_tools", [])
    context["scope_grants"] = getattr(trigger, "scope_grants", [])
    context["attach_outputs"] = getattr(trigger, "attach_outputs", [])

    # 若有 semantic memory，載入最近 5 筆事實
    if self._semantic is not None:
        recent_facts = await self._semantic.list_recent(limit=5)
        context["recent_facts"] = [...]

    # 3. 映射 decision
    if not trigger.enabled:
        decision = ActionDecision.SKIP
    elif trust_level == TrustLevel.SAFE:
        decision = ActionDecision.EXECUTE
    elif trust_level == TrustLevel.GUARDED:
        decision = ActionDecision.NOTIFY if trigger.notify else ActionDecision.EXECUTE
    else:  # CRITICAL
        decision = ActionDecision.HOLD

    # 4. 建立 prompt
    prompt = _build_prompt(trigger, context)

    return PlannedAction(...)
```

---

## _run_agent() — 完整實作邏輯

```python
async def _run_agent(self, plan: PlannedAction) -> None:
    # Pre-authorize
    for tool_name in plan.context.get("allowed_tools", []):
        self._session.perm.authorize(tool_name)

    for g in plan.context.get("scope_grants", []):
        self._session.perm.grant(ScopeGrant(
            resource=g["resource"], action=g["action"],
            selector=g.get("selector", "*"),
            constraints=g.get("constraints", {}),
            source=f"autonomy:{plan.trigger_name}",
        ))

    turn_start = datetime.now(UTC)

    try:
        output_chunks = []
        async for event in self._session.stream_turn(
            plan.prompt,
            abort_signal=self._abort.signal,
            origin="autonomy",
        ):
            if hasattr(event, "text") and isinstance(event.text, str):
                output_chunks.append(event.text)

        response = "".join(output_chunks).strip()

        # Resolve new files written during this turn
        attachments = _resolve_attachments(
            self._session.workspace,
            plan.context.get("attach_outputs", []),
            turn_start,
        )

        # Send result
        await self._notify.send(Notification(
            type=NotificationType.REPORT,
            title=f"Autonomy result: {plan.trigger_name}",
            body=response[:1000],
            attachments=attachments,
            ...
        ))
    except Exception as exc:
        await self._notify.send(Notification(type=NotificationType.ALERT, ...))
    finally:
        # Revoke — 排程間不累積權限
        for tool_name in _added_tools:
            self._session.perm.revoke(tool_name)
        self._session.perm.revoke_matching(
            lambda g: g.source == f"autonomy:{plan.trigger_name}"
        )
```

---

## _resolve_attachments()

展開 `attach_outputs` glob patterns，僅回傳 `mtime >= turn_start` 的新檔案，避免附上歷史舊檔：

```python
def _resolve_attachments(
    workspace: Path,
    patterns: list[str],
    since: datetime,
) -> list[Path]:
    # 跳過：絕對路徑、包含 .. 的路徑、非 workspace 內的路徑
    # mtime < since → 視為舊檔，跳過
```

---

## Config Tamper Detection（Issue #91）

loom.toml 的 `autonomy` 區段會在首次載入時計算 SHA-256 hash，寫入 `~/.loom/autonomy_config.hash`。之後每次啟動若 hash 不匹配，會發出 WARNING 並記錄日誌，但**不會阻斷執行**（fail-open）：

```python
# hash mismatch
logger.warning(
    "[autonomy] CONFIG CHANGE DETECTED — autonomy section hash mismatch. "
    "Review loom.toml and restart to update the stored hash."
)
# 繼續執行
```

---

## loom.toml 完整排程格式（v0.2.9.4）

```toml
[autonomy]
enabled = true

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
attach_outputs = ["news/*.md", "outputs/*"]  # 結果通知時附帶這些新檔案

[[autonomy.triggers]]
name          = "deploy_done"
event         = "deployment_done"
intent        = "跑 smoke test 並回報結果"
trust_level   = "guarded"
notify        = true
notify_thread = 1490024181994225744
allowed_tools = ["run_bash"]
scope_grants  = [
  { resource = "exec", action = "execute", selector = "workspace", constraints = { absolute_paths = "deny" } },
]
```

---

## 與舊版文件的關係

`doc/21-Action-Planner.md` 提到的 `DecisionPipeline` 在實作中並不存在。原本 `DecisionPipeline` 的職責已全部整合進 `ActionPlanner.handle()`：
- `_evaluate_trust()` → trust level parsing
- `_evaluate_risk()` → 目前由 `trigger.notify` 布林值代理
- `_is_appropriate_time()` → 目前無實作（可擴充）
- `Decision` → `ActionDecision`

`doc/21-Action-Planner.md` 需要同步更新，移除 `DecisionPipeline` 的獨立描述。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*