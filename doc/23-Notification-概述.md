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
│   ┌─────────────┐     ┌─────────────┐                     │
│   │  Routable   │────▶│  Notifier   │                     │
│   │             │     │  Registry   │                     │
│   └─────────────┘     └──────┬──────┘                     │
│                               │                             │
│        ┌──────────┬──────────┬──────────┬──────────┐       │
│        ▼          ▼          ▼          ▼          ▼       │
│   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐  │
│   │   CLI  │ │ Webhook│ │ Telegram│ │Discord │ │  ...  │  │
│   └────────┘ └────────┘ └────────┘ └────────┘ └────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 五種通知類型

```python
# loom/core/notification/types.py
class NotificationType(Enum):
    """通知類型"""
    
    # 資訊
    INFO = "info"              # 一般資訊
    SUCCESS = "success"         # 成功訊息
    
    # 警告
    WARNING = "warning"         # 警告（需要關注）
    ERROR = "error"             # 錯誤（需要處理）
    
    # 確認
    CONFIRM = "confirm"         # 需要用戶確認
```

---

## Notification 結構

```python
# loom/core/notification/models.py
@dataclass
class Notification:
    """通知"""
    
    type: NotificationType      # 通知類型
    title: str                 # 標題
    message: str               # 內容
    
    # 來源
    source: str                # 來源模組（如 "autonomy.daemon"）
    source_id: str | None      # 來源 ID（如觸發器 ID）
    
    # 選項
    channel: str = "default"   # 發送頻道
    metadata: dict = field(default_factory=dict)  # 額外資料
    
    # 時間
    created_at: datetime = field(default_factory=datetime.now)
    
    # 狀態（用於 CONFIRM 類型）
    status: NotificationStatus = NotificationStatus.PENDING
    response: ConfirmResult | None = None
```

---

## NotificationRouter

### 統一入口

```python
# loom/core/notification/router.py
class NotificationRouter:
    """通知路由器"""
    
    def __init__(
        self,
        notifier_registry: NotifierRegistry,
        confirm_flow_factory: Callable[[], ConfirmFlow],
    ):
        self.notifier_registry = notifier_registry
        self.confirm_flow_factory = confirm_flow_factory
        self._queue: asyncio.Queue = asyncio.Queue()
    
    async def send(
        self,
        notification_type: NotificationType,
        message: str,
        title: str | None = None,
        channel: str = "default",
        **kwargs,
    ) -> Notification:
        """
        發送通知的統一介面
        """
        
        notification = Notification(
            type=notification_type,
            title=title or self._default_title(notification_type),
            message=message,
            source=kwargs.get("source", "unknown"),
            source_id=kwargs.get("source_id"),
            channel=channel,
            metadata=kwargs,
        )
        
        # CONFIRM 類型需要特殊處理
        if notification_type == NotificationType.CONFIRM:
            return await self._send_confirm(notification)
        
        # 一般通知加入佇列
        await self._queue.put(notification)
        
        # 異步處理佇列
        asyncio.create_task(self._process_queue())
        
        return notification
    
    async def _process_queue(self):
        """處理通知佇列"""
        while not self._queue.empty():
            notification = await self._queue.get()
            await self._deliver(notification)
            self._queue.task_done()
```

### 發送到 Notifier

```python
async def _deliver(self, notification: Notification):
    """將通知投遞到所有訂閱的 Notifier"""
    
    # 根據 channel 找到訂閱的 notifier
    notifiers = self.notifier_registry.get_for_channel(notification.channel)
    
    for notifier in notifiers:
        try:
            await notifier.send(notification)
        except Exception as e:
            logger.error(f"Failed to send notification via {notifier.name}: {e}")
```

---

## NotifierRegistry

```python
# loom/core/notification/registry.py
class NotifierRegistry:
    """Notifier 註冊表"""
    
    def __init__(self):
        self._notifiers: dict[str, Notifier] = {}
        self._channel_map: dict[str, list[str]] = defaultdict(list)  # channel -> [notifier_id, ...]
    
    def register(self, notifier: Notifier, channels: list[str] | None = None):
        """註冊 Notifier"""
        self._notifiers[notifier.name] = notifier
        
        # 預設發到所有 channel
        if channels is None:
            channels = ["default"]
        
        for channel in channels:
            self._channel_map[channel].append(notifier.name)
    
    def unregister(self, notifier_name: str):
        """取消註冊"""
        if notifier_name in self._notifiers:
            del self._notifiers[notifier_name]
        
        # 從 channel_map 中移除
        for channel, names in self._channel_map.items():
            if notifier_name in names:
                names.remove(notifier_name)
    
    def get(self, name: str) -> Notifier | None:
        return self._notifiers.get(name)
    
    def get_for_channel(self, channel: str) -> list[Notifier]:
        """根據 channel 獲取 notifier 列表"""
        notifier_names = self._channel_map.get(channel, ["default"])
        
        result = []
        for name in notifier_names:
            if name in self._notifiers:
                result.append(self._notifiers[name])
        
        # 如果沒有找到，返回 default
        if not result and "default" in self._notifiers:
            result.append(self._notifiers["default"])
        
        return result
    
    def list_all(self) -> list[Notifier]:
        return list(self._notifiers.values())
```

---

## 與 ConfirmFlow 的整合

### 發送確認請求

```python
async def _send_confirm(self, notification: Notification) -> Notification:
    """發送需要確認的通知"""
    
    # 創建 ConfirmFlow
    flow = self.confirm_flow_factory()
    
    # 執行確認流程
    result = await flow.run(
        prompt=notification.message,
        timeout=notification.metadata.get("timeout", 300),
    )
    
    # 更新 notification 狀態
    notification.status = NotificationStatus.RESOLVED
    notification.response = result
    
    # 根據結果回調
    if notification.metadata.get("on_approve"):
        await self._handle_callback(
            notification.metadata["on_approve"],
            result == ConfirmResult.APPROVED
        )
    
    return notification
```

詳見 [25-ConfirmFlow.md](25-ConfirmFlow.md)。

---

## 使用範例

### 程式化發送通知

```python
# 簡單通知
await notification_router.send(
    NotificationType.INFO,
    "Task completed successfully",
    source="task.scheduler"
)

# 錯誤通知
await notification_router.send(
    NotificationType.ERROR,
    f"Tool '{tool_name}' execution failed: {error}",
    source="harness.executor"
)

# 需要確認
await notification_router.send(
    NotificationType.CONFIRM,
    "Do you want to delete all test data?",
    source="autonomy.action_planner",
    source_id="cleanup_task",
    timeout=600,
    on_approve="execute_delete",
    on_deny="abort"
)
```

### loom.toml 配置

```toml
[notification]

# 預設 channel
default_channel = "cli"

# 佇列設定
queue_size = 100
process_interval = 1  # 秒

# 通知設定
[notification.channels]

# CLI 通知（總是啟用）
[notification.channels.cli]
enabled = true
type = "cli"

# Webhook 通知
[notification.channels.webhook]
enabled = true
type = "webhook"
url = "https://hooks.example.com/notify"

# Telegram 通知
[notification.channels.telegram]
enabled = false
type = "telegram"
bot_token = "${TELEGRAM_BOT_TOKEN}"
chat_id = "${TELEGRAM_CHAT_ID}"

# Discord 通知
[notification.channels.discord]
enabled = false
type = "discord"
webhook_url = "${DISCORD_WEBHOOK_URL}"
```

---

## 總結

Notification Layer 提供：

| 元件 | 職責 |
|------|------|
| NotificationType | 五種通知類型（INFO/SUCCESS/WARNING/ERROR/CONFIRM） |
| Notification | 統一的通知資料結構 |
| NotificationRouter | 統一的通知入口和路由 |
| NotifierRegistry | 管理多個 Notifier 適配器 |
| ConfirmFlow | 確認請求的特殊處理 |

透過 Notification Layer，Loom 的各個模組可以統一地發送通知，而不需要關心底層的發送方式（CLI、Webhook、Telegram 等）。
