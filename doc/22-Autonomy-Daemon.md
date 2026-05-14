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
           ├─ EXECUTE
           │     ├─ mode == "chime" → _deliver_chime(plan)（issue #369）
           │     │      └─ chime_delivery callback → bot.deliver_chime
           │     │           → 在既有 Discord thread session 內喚醒，
           │     │             不另開 session、不發 Notification
           │     └─ mode == "independent"（預設）→ _run_agent(plan)
           └─ NOTIFY/HOLD → ConfirmFlow.ask() → APPROVED → 同上分流
           │
           ▼
_run_agent(plan)                          _run_chime_turn(thread_id, req)
   ├─ allowed_tools / scope_grants            ├─ allowed_tools / scope_grants
   │  （try/finally 確保 revoke）             │  （task 自己 try/finally；revoke
   ├─ stream_turn(origin="autonomy")         │   是 cancel cleanup 的一部分）
   ├─ 收集 output chunks                     ├─ stream_turn(origin="chime"),
   ├─ _resolve_attachments()                 │   intent 包在 <system_chime> 標籤
   └─ 發送 REPORT notification               └─ 直接寫回 Discord thread
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

## Chime mode — 喚醒既有 session（issue #369）

`mode = "chime"` 是 `mode = "independent"`（預設）的替代分支。觸發時不開新 session，而是**把該排程的 intent 注入到一個既有的 Discord thread session**，由那個 session 用既有上下文回應。

### 為什麼分流

| 維度 | independent | chime |
|---|---|---|
| Session | 全新、無上下文 | 既有、有完整對話歷史 |
| 輸出通道 | NotificationRouter（REPORT 通知 / 附件） | session 自己的 channel（Discord thread 訊息） |
| Agent 看到的 | 排程 prompt | `<system_chime schedule="…" fired_at="…">intent</system_chime>` |
| 適用場景 | 後台寫檔、housekeeping、生成報告 | 對 user 主動發話、定期關心、上下文相關的提醒 |

### 路由

```python
async def _execute_plan(self, plan):
    if plan.context.get("mode") == "chime":
        accepted = await self._deliver_chime(plan)  # 呼叫注入的 chime_delivery callback
        if accepted:
            return
        # callback 找不到 target session
        if plan.context["target"].get("fallback") == "independent":
            await self._run_agent(plan)  # 退回 spawn-new-session 行為
        # 否則 skip（預設）
        return
    await self._run_agent(plan)
```

`chime_delivery` 是 platform 層注入的 callback。在 `loom discord start --autonomy` 路徑裡，`AutonomyDaemon` 接的是 `LoomDiscordBot.deliver_chime`，daemon 本身不知道 Discord 存在。

### Bot 端：dedupe + 序列化

```
deliver_chime(req)
   └─ self._chime_pending[(thread_id)][schedule_name] = req   # latest-wins dedupe
   └─ 確保 _chime_dispatcher_loop(thread_id) 在跑

_chime_dispatcher_loop(thread_id)
   while pending:
     name = next(iter(pending))                # 先挑名字、不 pop
     await running_turns[thread_id]            # 等當下 user turn 跑完（不打斷）
        # 等的過程中若同名 chime 進來會 overwrite 同 key
     req = pending.pop(name)                   # wait 完才 pop → 拿到最新版
     await _run_chime_turn(thread_id, req)
```

### 同 process invariant — permission scoping

Chime 是 Loom **第一個從外部 source 插隊到 shared session state 的機制**。為了不讓 chime 的暫時權限 leak 到「中途插隊的 user turn」，必須遵守兩條 invariant：

1. **`_apply_chime_permissions` / `_revoke_chime_permissions` 一對綁在 `_run_chime_turn` 自己的 try/finally**，不能放在 dispatcher 外層。原因：task cancel 時，task 自己的 finally 是 cancellation cleanup 的一部分，會在 task 真正 `done()` 前跑完；放在外層 dispatcher 的話，要等 dispatcher 從 `await task` 回來才 revoke，這之間有 event-loop tick 可以讓搶占的 user task 啟動，看到殘留的 grants。

2. **`on_message` 的 cancel-and-replace 路徑必須 `await asyncio.wait_for(existing, timeout=...)` 才 schedule 下一個 turn**，不能 fire-and-forget cancel。配合 (1) 雙保險：cancel 後等被 cancel 的 task 把 finally 跑完，新 user turn 才開始。

3. **`asyncio.CancelledError` 不被 `except Exception` 抓**（Py3.8+ 改繼承 BaseException）。需要顯式分流：
   ```python
   try:
       await existing
   except asyncio.CancelledError:
       if asyncio.current_task().cancelling():
           raise               # 自己被 cancel → 真的 shutdown
       # 否則只是 awaited 的 task 被 cancel → 繼續處理 chime
   except Exception:
       pass
   ```

### loom.toml 寫法

```toml
[[autonomy.schedules]]
name          = "morning_greeting"
cron          = "0 1 * * *"            # 01:00 UTC = 09:00 Asia/Taipei
intent        = "向 user 主動道早安，提一兩件今天值得留意的事"
trust_level   = "safe"
mode          = "chime"
allowed_tools = ["memorize"]           # chime turn 期間生效，結束後 revoke

  [autonomy.schedules.target]
  type     = "discord_thread"
  id       = "1490024181994225744"
  fallback = "skip"                    # 或 "independent"
```

### v1 邊界

- target 只支援 `discord_thread`（CLI session target 留 P2）
- 同 process only：必須走 `loom discord start --autonomy`，daemon 跟 bot 共用 event loop
- Cold thread（bot 重啟後 user 還沒講話）**不會**被 chime 隱式 resume，走 `target.fallback`
- 真正打斷 mid-turn 的 interrupt 模式留 P2

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

# Chime（issue #369）— 喚醒既有 Discord thread session，不另開 session
[[autonomy.schedules]]
name          = "morning_greeting"
cron          = "0 1 * * *"
intent        = "向 user 主動道早安"
trust_level   = "safe"
mode          = "chime"

  [autonomy.schedules.target]
  type     = "discord_thread"
  id       = "1490024181994225744"
  fallback = "skip"
```

### 欄位對照表

| 欄位 | 預設 | 用途 |
|---|---|---|
| `mode` | `"independent"` | `"chime"` 切換喚醒既有 session（issue #369） |
| `target.type` | — | chime 模式下必填，v1 只支援 `"discord_thread"` |
| `target.id` | — | chime 模式下必填，目標 thread ID（字串） |
| `target.fallback` | `"skip"` | chime 找不到 target 時的退路：`"skip"` 放棄 / `"independent"` 退回 spawn-new-session |

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