# Notifier 適配器

Notifier 適配器是 Notification Layer 的底層實作。實際已實作的適配器：CLI、Webhook、Discord（Webhook + Button handler + 工具）。

> **Phase X**：TelegramNotifier、EmailNotifier 在 Phase X 規劃中，以下章節中的代碼範例為**說明性示意**，非實際運行代碼。

---

## Notifier 抽象

```python
# loom/notify/adapters/__init__.py
class BaseNotifier(ABC):
    name: str

    @abstractmethod
    async def send(self, notification: "Notification") -> None: ...

    async def reply(
        self, notification: "Notification", result: "ConfirmResult"
    ) -> None:
        """Handle user reply to a CONFIRM notification (override for async reply)."""
        raise NotImplementedError
```

所有適配器實作 `send()` 即可；需要非同步回覆處理時（如 Discord Button）覆寫 `reply()`。

---

## CLI Notifier

### 用途

Rich 格式化輸出到終端（`stdout`）。

```python
# loom/notify/adapters/cli.py
class CLINotifier(BaseNotifier):
    """Rich Panel 格式化輸出到 stdout"""
    name = "cli"

    def __init__(self, console: Console) -> None:
        self._console = console

    async def send(self, notification: "Notification") -> None:
        self._console.print(Panel(
            notification.body,
            title=f"[{notification.type.value.upper()}] {notification.title}",
            border_style=_STYLE_MAP.get(notification.type, "dim"),
        ))
```

> **CONFIRM 通知**由 `ConfirmFlow` 的 `wait_fn` 接管（stdin 讀取），不走 `CLINotifier.send()`。

---

## Webhook Notifier

### 用途

HTTP POST 發送 JSON 到外部 URL。

```python
# loom/notify/adapters/webhook.py
class WebhookNotifier(BaseNotifier):
    name = "webhook"

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout

    async def send(self, notification: "Notification") -> None:
        async with aiohttp.ClientSession() as session:
            await session.post(
                self._url,
                json={
                    "title": notification.title,
                    "body": notification.body,
                    "type": notification.type.value,
                    "source": notification.source,
                    "timestamp": notification.created_at.isoformat(),
                },
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
```

> `reply()` 由 `WebhookNotifier` 的 `/webhook/reply` endpoint 處理，無需在 class 內實作。

---

## Discord Notifier（v0.2.3.1 + v0.2.5.2）

### 用途

Discord Webhook（`POST /webhooks/...`）發送 rich embed；Button 回覆由 `discord_bot.py` 的 `/webhook/reply` endpoint 處理。

### Webhook 模式

```python
# loom/notify/adapters/discord.py
class DiscordNotifier(BaseNotifier):
    name = "discord"

    def __init__(
        self,
        webhook_url: str,
        username: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        self._url = webhook_url
        self._username = username
        self._avatar = avatar_url

    async def send(self, notification: "Notification") -> None:
        payload = {
            "embeds": [self._build_embed(notification)],
        }
        if self._username:
            payload["username"] = self._username
        if self._avatar:
            payload["avatar_url"] = self._avatar
        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, json=payload) as resp:
                resp.raise_for_status()
```

### Agent 工具（v0.2.5.2）

Discord Notifier 同時提供兩個 Agent 可主動呼叫的工具：

#### send_discord_file（GUARDED）

發送工作區檔案至 Discord thread：

```python
async def send_discord_file(filepath: str, thread_id: int | None = None) -> ToolResult:
    """
    發送工作區檔案至 Discord。
    
    Args:
        filepath: 相對於工作區根目錄的路徑
        thread_id: 可選，發送至指定 thread（預設使用當前 thread）
    """
```

#### send_discord_embed（SAFE）

發送格式化 Rich Embed 面板至 Discord：

```python
async def send_discord_embed(
    title: str,
    description: str,
    color: str = "#0099ff",
    fields: list[dict] | None = None,
    thread_id: int | None = None,
) -> ToolResult:
    """
    發送 Discord Rich Embed 面板。
    
    Args:
        title: Embed 標題
        description: 主要描述文字
        color: Hex 色碼（如 "#ff0000"）
        fields: 可選欄位列表
            [{"name": "...", "value": "...", "inline": true/false}, ...]
        thread_id: 可選，發送至指定 thread
    """
```

### Discord 附件接收（v0.2.5.2）

Bot 接收用戶上傳的檔案時自動處理：

```
用戶上傳附件 → Bot 下載至 .discord_downloads/<filename.ext>
          → prompt 注入：「[User uploaded: .discord_downloads/<filename>]」
```

---

## Phase X：Telegram Notifier

> **尚未實作**（Phase X 規劃）。以下為說明性代碼。

```python
# loom/notify/adapters/telegram.py  （Phase X，尚未實作）
class TelegramNotifier(BaseNotifier):
    """Telegram Bot 通知器（Phase X）"""

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id

    async def send(self, notification: "Notification") -> None:
        # 格式化、發送 HTTP POST 到 Telegram API
        ...
```

loom.toml 中的 `[notification.channels.telegram]` 設定亦屬 Phase X。

---

## Phase X：Email Notifier

> **尚未實作**（Phase X 規劃）。以下為說明性代碼。

```python
# 自訂 Email Notifier（Phase X，尚未實作）
class EmailNotifier(BaseNotifier):
    """SMTP 郵件通知器"""
    name = "email"

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: list[str],
    ) -> None:
        ...

    async def send(self, notification: "Notification") -> None:
        ...
```

---

## 實際已實作的 loom.toml 配置

```toml
[notification]
default_channel = "cli"

[notification.channels.webhook]
enabled = true
url = "https://hooks.example.com/notify"
headers = { Authorization = "Bearer ${WEBHOOK_TOKEN}" }
timeout = 10.0

[notification.channels.discord]
enabled = true
webhook_url = "${DISCORD_WEBHOOK_URL}"
username = "Loom Bot"
```

> Telegram、Email 的 loom.toml 設定格式屬 Phase X 規劃，尚未支援。

---

## 總結

| Notifier | 檔案 | 實際狀態 |
|----------|------|----------|
| CLI | `adapters/cli.py` | ✅ 已實作 |
| Webhook | `adapters/webhook.py` | ✅ 已實作 |
| Discord | `adapters/discord.py` | ✅ Webhook + Rich Embed |
| Discord Bot | `adapters/discord_bot.py` | ✅ Button 回覆處理 |
| Discord Agent Tools | `adapters/discord.py` | ✅ `send_discord_file`（GUARDED）、`send_discord_embed`（SAFE）|
| Telegram | — | ❌ Phase X |
| Email | — | ❌ Phase X |

所有實作共享同一介面：
- `send(notification)`: 發送通知
- `reply(notification, result)`: 處理 CONFIRM 回覆（可選覆寫）
