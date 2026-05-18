# 新增 Notifier

本指南說明如何為 Loom 新增自訂的 Notifier 適配器。

---

## Notifier 結構

```python
# loom/notify/router.py
class BaseNotifier:
    """Abstract base for all notification adapters."""
    channel: str

    async def send(self, notification: Notification) -> None:
        raise NotImplementedError
```

---

## 步驟 1：創建 Notifier 類

以下 Slack 範例是自訂 plugin 內的 adapter，不是核心 repo 內建檔案。

```python
import aiohttp

from loom.notify.router import BaseNotifier
from loom.notify.types import Notification, NotificationType

class SlackNotifier(BaseNotifier):
    """Slack 通知器"""

    channel = "slack"
    
    def __init__(
        self,
        webhook_url: str,
        slack_channel: str | None = None,
        username: str = "Loom Bot",
        icon_emoji: str = ":robot_face:",
    ):
        self.webhook_url = webhook_url
        self.slack_channel = slack_channel
        self.username = username
        self.icon_emoji = icon_emoji
    
    async def send(self, notification: Notification) -> None:
        """發送 Slack 通知"""
        
        payload = self._build_payload(notification)
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                json=payload,
            ) as response:
                response.raise_for_status()
    
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
                    "text": notification.body
                }
            }
        ]
        
        payload = {
            "username": self.username,
            "icon_emoji": self.icon_emoji,
            "blocks": blocks,
        }
        
        if self.slack_channel:
            payload["channel"] = self.slack_channel
        
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
            NotificationType.CONFIRM: "#9b59b6",
            NotificationType.INPUT: "#9b59b6",
            NotificationType.ALERT: "#e74c3c",
            NotificationType.REPORT: "#2ecc71",
        }.get(notification_type, "#95a5a6")
```

---

## 步驟 2：註冊 Notifier

### 程式化註冊

```python
from loom.notify.router import NotificationRouter

# 創建 Notifier
slack = SlackNotifier(
    webhook_url="https://hooks.slack.com/services/xxx",
    slack_channel="#alerts"
)

# 註冊
router = NotificationRouter()
router.register(slack)
```

### loom.toml 配置

```toml
[notify]
default_channel = "cli"
```

核心設定目前只定義通知預設 channel。自訂 Slack token、channel 等設定通常由 plugin 自己讀取環境變數或自己的 config 區段。

---

## 步驟 3：測試 Notifier

目前沒有通用 `loom notify test` CLI。測試自訂 notifier 時，建議在 plugin 單元測試中建立 `Notification` 並呼叫 `send()`，或把 notifier 註冊到 `NotificationRouter` 後用 `router.send(notification)` 做整合測試。

---

## 完整範例：Line Notify

以下同樣是自訂 adapter 範例；核心 repo 尚未內建 Line Notify。

```python
class LineNotifyNotifier(BaseNotifier):
    """Line Notify 通知器"""

    channel = "line_notify"
    
    def __init__(self, token: str):
        self.token = token
        self.api_url = "https://notify-api.line.me/api/notify"
    
    async def send(self, notification: Notification) -> None:
        """發送 Line Notify"""
        
        payload = {
            "message": f"\n{notification.title}\n{notification.body}"
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
                response.raise_for_status()
```

---

## 總結

新增 Notifier 的步驟：

1. 創建 Notifier 子類
2. 實現 `channel` 屬性和 `send()` 方法
3. 構建通知 payload
4. 註冊到 `NotificationRouter`，通常由 plugin 的 `notifiers()` 貢獻
5. 在 plugin/config 中保存必要 token 或 webhook URL
