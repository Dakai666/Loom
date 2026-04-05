# 新增 Notifier

本指南說明如何為 Loom 新增自訂的 Notifier 適配器。

---

## Notifier 結構

```python
# loom/core/notification/adapters/base.py
class Notifier(ABC):
    """Notifier 抽象基類"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Notifier 名稱"""
        pass
    
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """發送通知"""
        pass
```

---

## 步驟 1：創建 Notifier 類

```python
# loom/core/notification/adapters/slack.py
from dataclasses import dataclass
from loom.core.notification.adapters.base import Notifier
from loom.core.notification.models import Notification, NotificationType

class SlackNotifier(Notifier):
    """Slack 通知器"""
    
    def __init__(
        self,
        webhook_url: str,
        channel: str | None = None,
        username: str = "Loom Bot",
        icon_emoji: str = ":robot_face:",
    ):
        self.name = "slack"
        self.webhook_url = webhook_url
        self.channel = channel
        self.username = username
        self.icon_emoji = icon_emoji
    
    async def send(self, notification: Notification) -> bool:
        """發送 Slack 通知"""
        
        payload = self._build_payload(notification)
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                json=payload,
            ) as response:
                return response.status == 200
    
    def _build_payload(self, notification: Notification) -> dict:
        """構建 Slack payload"""
        
        # 根據類型選擇顏色
        color = self._get_color(notification.type)
        
        # 構建訊息區塊
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{notification.title}*"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": notification.message
                }
            }
        ]
        
        payload = {
            "username": self.username,
            "icon_emoji": self.icon_emoji,
            "blocks": blocks,
        }
        
        if self.channel:
            payload["channel"] = self.channel
        
        if color:
            payload["attachments"] = [{
                "color": color,
                "blocks": blocks
            }]
        
        return payload
    
    def _get_color(self, notification_type: NotificationType) -> str:
        """根據類型獲取顏色"""
        return {
            NotificationType.INFO: "#36a5db",
            NotificationType.SUCCESS: "#2ecc71",
            NotificationType.WARNING: "#f39c12",
            NotificationType.ERROR: "#e74c3c",
            NotificationType.CONFIRM: "#9b59b6",
        }.get(notification_type, "#95a5a6")
```

---

## 步驟 2：註冊 Notifier

### 程式化註冊

```python
from loom.core.notification.registry import NotifierRegistry

# 創建 Notifier
slack = SlackNotifier(
    webhook_url="https://hooks.slack.com/services/xxx",
    channel="#alerts"
)

# 註冊
registry = NotifierRegistry.get_instance()
registry.register(slack, channels=["critical", "default"])
```

### loom.toml 配置

```toml
[notification.channels]

# Slack
[notification.channels.slack]
enabled = true
type = "slack"
webhook_url = "${SLACK_WEBHOOK_URL}"
channel = "#alerts"
username = "Loom Bot"
icon_emoji = ":robot_face:"
```

---

## 步驟 3：測試 Notifier

```bash
# 測試通知發送
loom notify test --notifier slack --message "Test message"
```

---

## 完整範例：Line Notify

```python
# loom/core/notification/adapters/line.py
class LineNotifyNotifier(Notifier):
    """Line Notify 通知器"""
    
    def __init__(self, token: str):
        self.name = "line_notify"
        self.token = token
        self.api_url = "https://notify-api.line.me/api/notify"
    
    async def send(self, notification: Notification) -> bool:
        """發送 Line Notify"""
        
        payload = {
            "message": f"\n{notification.title}\n{notification.message}"
        }
        
        headers = {
            "Authorization": f"Bearer {self.token}"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.api_url,
                data=payload,
                headers=headers,
            ) as response:
                data = await response.json()
                return data.get("status") == 200
```

---

## 總結

新增 Notifier 的步驟：

1. 創建 Notifier 子類
2. 實現 `name` 屬性和 `send()` 方法
3. 構建通知 payload
4. 註冊到 NotifierRegistry
5. 在 loom.toml 配置或測試
