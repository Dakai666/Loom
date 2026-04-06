# ConfirmFlow

ConfirmFlow 是 Loom 的「用戶確認流程」。當 Autonomy Engine 的 `ActionPlanner` 決定行動需要用戶授權時，會透過 ConfirmFlow 發送確認通知並等待回覆。

---

## 使用場景

```
ActionPlanner.handle(trigger, context)
    │
    └─→ PlannedAction(decision=NOTIFY 或 HOLD)
              │
              ▼
ConfirmFlow.ask(Notification)
              │
              ├─▶ APPROVED  → _run_agent()
              ├─▶ DENIED   → 不執行，結束
              └─▶ TIMEOUT  → 由 ConfirmFlow.default_on_timeout 決定
```

---

## 實際實現（loom/notify/confirm.py）

```python
class ConfirmFlow:
    def __init__(
        self,
        send_fn: Callable[[Notification], Awaitable[None]],   # NotificationRouter.send
        wait_fn: Callable[[Notification], Awaitable[ConfirmResult]] | None = None,
        default_on_timeout: ConfirmResult = ConfirmResult.TIMEOUT,
    ) -> None:
        self._send = send_fn
        self._wait = wait_fn
        self._default_on_timeout = default_on_timeout

    async def ask(self, notification: Notification) -> ConfirmResult:
        assert notification.type == NotificationType.CONFIRM

        await self._send(notification)

        if self._wait is None:
            # 無回覆機制 → 自動批准（用於無人類介入的測試場景）
            return ConfirmResult.APPROVED

        try:
            result = await asyncio.wait_for(
                self._wait(notification),
                timeout=notification.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            return self._default_on_timeout
```

---

## 參數說明

| 參數 | 說明 |
|------|------|
| `send_fn` | 發送通知的回調，通常傳入 `NotificationRouter.send` |
| `wait_fn` | 等待回覆的回調；CLI 模式下傳入 stdin 回覆處理；`None` = 自動批准 |
| `default_on_timeout` | 超時時的回覆（預設 `TIMEOUT`）；`TIMEOUT` → caller 自行決定行為 |

---

## 回覆機制

CLI 模式下，`wait_fn` 由平台層注入。回覆接收方式：

| 平台 | 回覆方式 |
|------|----------|
| CLI | `Rich Panel` + stdin 輸入 `a`/`d`/`t` |
| Discord | `discord.py` Button（`APPROVE` / `DENY`）|
| Webhook | `WebhookNotifier` 輪詢回覆佇列 |
| 測試 | `wait_fn=None` → 自動 `APPROVED` |

---

## 與 NotificationRouter 的整合

`AutonomyDaemon` 中的初始化：

```python
confirm_flow = ConfirmFlow(
    send_fn=notify_router.send,
    wait_fn=cli_notifier.wait_reply,  # 平台層注入
    default_on_timeout=ConfirmResult.TIMEOUT,
)
daemon = AutonomyDaemon(
    notify_router=notify_router,
    confirm_flow=confirm_flow,
    loom_session=session,
)
```

---

## ConfirmResult 枚舉

```python
# loom/notify/types.py
class ConfirmResult(Enum):
    APPROVED = "approved"
    DENIED   = "denied"
    TIMEOUT  = "timeout"
```

---

## 與決策映射

`ActionPlanner.handle()` 的決策 → ConfirmFlow 行為：

| Decision | ConfirmFlow.ask() | 結果處理 |
|----------|-------------------|---------|
| `EXECUTE` | 不呼叫 | 直接執行 |
| `NOTIFY` | 呼叫，超時 60s | TIMEOUT → skip（不下放 EXECUTE）|
| `HOLD` | 呼叫，超時 300s | TIMEOUT → 不執行 |

---

## 超時設計原則

TIMEOUT **不等於** DENIED。TIMEOUT 表示「無法確認意圖」，由 caller（如 `AutonomyDaemon`）自行判斷：
- `NOTIFY` + TIMEOUT → 跳過（保守：既然沒有明確同意就不要做）
- `HOLD` + TIMEOUT → 不執行（強制確認失敗 = 不執行）

---

## loom.toml 中的相關設定

ConfirmFlow 本身無獨立設定區段。超時行為由 `ActionPlanner` 在發送 `Notification` 時設定：

```toml
[[autonomy.schedules]]
name         = "daily_backup"
cron         = "0 3 * * *"
intent       = "執行每日備份"
trust_level  = "guarded"
notify       = true       # → ActionPlanner 設 timeout=60s
```

`HOLD` 等級（`trust_level = "critical"`）由 `AutonomyDaemon._execute_plan()` 設 `timeout=300s`。

---

## 總結

| 功能 | 說明 |
|------|------|
| 發送確認 | `send_fn(notification)` |
| 等待回覆 | `wait_fn` 回調（平台層注入）|
| 超時處理 | `asyncio.wait_for` + `default_on_timeout` |
| 無回覆機制 | `wait_fn=None` → 自動 APPROVED（測試用）|
| 結果枚舉 | APPROVED / DENIED / TIMEOUT |
| 設計原則 | TIMEOUT ≠ DENIED；由 caller 決定超時行為 |
