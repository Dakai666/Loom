# Notification Layer 概述

Notification Layer 是 Loom 的通知系統。它負責在各種事件發生時通知用戶或其他系統。

---

## 為什麼需要 Notification Layer？

Loom 的各個 Layer 都會產生需要通知的事件：

| Layer | 事件 | 通知內容 |
|-------|------|----------|
| Harness | 工具執行失敗 | 「工具 XXX 執行失敗」 |
| Memory | 記憶寫入完成 | （通常不需要通知） |
| Cognition | Token 使用過高 | 「Context 使用率達 80%」 |
| Task | 任務執行完成 | 「任務 A 已完成」 |
| Autonomy | 觸發器觸發 | 「定時任務已執行」 |
| Autonomy | 需要確認 | 「是否允許執行 XXX？」 |

Notification Layer 提供統一的介面來發送這些通知。

---

## 架構

```
┌─────────────────────────────────────────────────────────────┐
│                    Notification Layer                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────┐                                         │
│   │ Notification │                                         │
│   │   Router    │  ← 統一入口                              │
│   └──────┬──────┘                                         │
│          │                                                 │
│          ▼                                                 │
│   ┌─────────────┐                                         │
│   │ BaseNotifier│  router 內部以 channel 註冊              │
│   └──────┬──────┘                                         │
│          │                                                 │
│        ┌──────────┬──────────┬──────────┬──────────┐       │
│        ▼          ▼          ▼          ▼          ▼       │
│   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────────┐         │
│   │   CLI  │ │ Webhook│ │Discord │ │Discord Bot │         │
│   └────────┘ └────────┘ └────────┘ └────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 五種通知類型

```python
# loom/notify/types.py
class NotificationType(Enum):
    """通知類型"""
    
    INFO    = "info"     # 一般資訊，無需回覆
    CONFIRM = "confirm"  # yes/no 確認
    INPUT   = "input"    # 需要自由文字輸入
    ALERT   = "alert"    # 急迫警示
    REPORT  = "report"   # 週期性摘要
```

---

## Notification 結構

```python
# loom/notify/types.py
@dataclass
class Notification:
    type: NotificationType
    title: str
    body: str
    trigger_name: str = ""
    timeout_seconds: int = 60
    thread_id: int = 0
    attachments: list[Path] = field(default_factory=list)
    inline_image: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

---

## NotificationRouter

### 統一入口

```python
# loom/notify/router.py
class NotificationRouter:
    """通知路由器"""

    def __init__(self) -> None:
        self._notifiers: dict[str, BaseNotifier] = {}

    def register(self, notifier: BaseNotifier) -> "NotificationRouter":
        self._notifiers[notifier.channel] = notifier
        return self

    async def send(self, notification: Notification) -> dict[str, bool]:
        """投遞到所有已註冊 notifier，回傳各 channel 成敗。"""
        tasks = {
            channel: notifier.send(notification)
            for channel, notifier in self._notifiers.items()
        }
        outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            channel: not isinstance(outcome, Exception)
            for channel, outcome in zip(tasks.keys(), outcomes)
        }
```

### 發送到 Notifier

```python
async def _deliver(self, notification: Notification):
    """將通知投遞到所有訂閱的 Notifier"""
    
    await self.send(notification)
```

---

## Notifier 註冊

Loom 沒有獨立 `NotifierRegistry`。`NotificationRouter` 自己保存 `{channel: notifier}`，每個 adapter 透過 `channel` 欄位決定註冊名稱。

```python
# loom/notify/router.py
router = NotificationRouter()
router.register(CLINotifier(console))
router.register(WebhookNotifier(url="https://..."))
```

---

## 與 ConfirmFlow 的整合

### 發送確認請求

```python
# loom/notify/confirm.py
flow = ConfirmFlow(send_fn=router.send, wait_fn=cli_notifier.wait_reply)
result = await flow.ask(Notification(
    type=NotificationType.CONFIRM,
    title="Loom autonomy: cleanup_task",
    body="Do you want to delete all test data?",
    trigger_name="cleanup_task",
    timeout_seconds=300,
))
```

詳見 [25-ConfirmFlow.md](25-ConfirmFlow.md)。

---

## 使用範例

### 程式化發送通知

```python
# 簡單通知
await notification_router.send(Notification(
    type=NotificationType.INFO,
    title="Task completed",
    body="Task completed successfully",
))

# 警示通知
await notification_router.send(Notification(
    type=NotificationType.ALERT,
    title="Tool failed",
    body=f"Tool '{tool_name}' execution failed: {error}",
))

# 需要確認
result = await confirm_flow.ask(Notification(
    type=NotificationType.CONFIRM,
    title="Cleanup task",
    body="Do you want to delete all test data?",
    trigger_name="cleanup_task",
    timeout_seconds=600,
))
```

### loom.toml 配置

```toml
[notify]
default_channel = "cli"
```

`default_channel` 目前是 informational；實際 fan-out 由 runtime 註冊了哪些 notifiers 決定（CLI、Webhook、DiscordBotNotifier 等）。

---

## 總結

Notification Layer 提供：

| 元件 | 職責 |
|------|------|
| NotificationType | 五種通知類型（INFO/CONFIRM/INPUT/ALERT/REPORT） |
| Notification | 統一的通知資料結構 |
| NotificationRouter | 統一的通知入口和路由 |
| ConfirmFlow | 確認請求的特殊處理 |

透過 Notification Layer，Loom 的各個模組可以統一地發送通知，而不需要關心底層的發送方式（CLI、Webhook、Discord 等）。
