# Notifier 適配器

Notifier 適配器是 Notification Layer 的底層實現。每個適配器負責一種具體的通知方式（CLI、Webhook、Telegram、Discord 等）。

---

## Notifier 抽象

```python
# loom/core/notification/adapters/base.py
class Notifier(ABC):
    """Notifier 抽象基類"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Notifier 名稱"""
        pass
    
    @property
    def supported_types(self) -> list[NotificationType]:
        """支援的通知類型"""
        return list(NotificationType)
    
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """
        發送通知
        
        Returns:
            bool: 是否發送成功
        """
        pass
    
    async def format(self, notification: Notification) -> str:
        """
        格式化通知（可被子類覆寫）
        """
        return f"[{notification.type.value.upper()}] {notification.title}\n{notification.message}"
```

---

## CLI Notifier

### 用途

在終端輸出通知。

```python
# loom/core/notification/adapters/cli.py
class CLINotifier(Notifier):
    """CLI 通知器（輸出到終端）"""
    
    def __init__(
        self,
        use_colors: bool = True,
        output_stream: TextIO | None = None,
    ):
        self.name = "cli"
        self.use_colors = use_colors
        self.output_stream = output_stream or sys.stdout
    
    @property
    def supported_types(self) -> list[NotificationType]:
        # CLI 不支援 CONFIRM（CONFIRM 需要互動）
        return [t for t in NotificationType if t != NotificationType.CONFIRM]
    
    async def send(self, notification: Notification) -> bool:
        """發送到終端"""
        
        # 格式化輸出
        formatted = self._format(notification)
        
        # 寫入輸出流
        print(formatted, file=self.output_stream, flush=True)
        
        return True
    
    def _format(self, notification: Notification) -> str:
        """格式化通知"""
        
        color = self._get_color(notification.type)
        
        if self.use_colors and color:
            prefix = f"\033[{color}m"
            suffix = "\033[0m"
        else:
            prefix = suffix = ""
        
        return (
            f"{prefix}[{notification.type.value.upper()}] "
            f"{notification.title}{suffix}\n"
            f"{notification.message}"
        )
    
    def _get_color(self, notification_type: NotificationType) -> str:
        """根據類型獲取 ANSI 顏色碼"""
        return {
            NotificationType.INFO: "36",     # 青色
            NotificationType.SUCCESS: "32",   # 綠色
            NotificationType.WARNING: "33",  # 黃色
            NotificationType.ERROR: "31",    # 紅色
        }.get(notification_type, "0")
```

---

## Webhook Notifier

### 用途

發送 HTTP POST 請求到指定的 Webhook URL。

```python
# loom/core/notification/adapters/webhook.py
class WebhookNotifier(Notifier):
    """Webhook 通知器"""
    
    def __init__(
        self,
        url: str,
        method: str = "POST",
        headers: dict | None = None,
        timeout: float = 10.0,
        retry_count: int = 3,
    ):
        self.name = "webhook"
        self.url = url
        self.method = method.upper()
        self.headers = headers or {"Content-Type": "application/json"}
        self.timeout = timeout
        self.retry_count = retry_count
    
    async def send(self, notification: Notification) -> bool:
        """發送到 Webhook"""
        
        payload = {
            "type": notification.type.value,
            "title": notification.title,
            "message": notification.message,
            "source": notification.source,
            "timestamp": notification.created_at.isoformat(),
            "metadata": notification.metadata,
        }
        
        # 重試機制
        for attempt in range(self.retry_count):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        self.method,
                        self.url,
                        json=payload,
                        headers=self.headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as response:
                        if response.status < 400:
                            return True
                        logger.warning(
                            f"Webhook returned {response.status}, "
                            f"attempt {attempt + 1}/{self.retry_count}"
                        )
            
            except Exception as e:
                logger.warning(
                    f"Webhook error: {e}, "
                    f"attempt {attempt + 1}/{self.retry_count}"
                )
        
        return False
```

### loom.toml 配置

```toml
[notification.channels.webhook]
enabled = true
type = "webhook"
url = "https://hooks.example.com/notify"
method = "POST"
headers = { Content-Type = "application/json", Authorization = "Bearer ${WEBHOOK_TOKEN}" }
timeout = 10
retry_count = 3
```

---

## Telegram Notifier

### 用途

發送 Telegram 訊息。

```python
# loom/core/notification/adapters/telegram.py
class TelegramNotifier(Notifier):
    """Telegram 通知器"""
    
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        parse_mode: str = "Markdown",
    ):
        self.name = "telegram"
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
    
    async def send(self, notification: Notification) -> bool:
        """發送到 Telegram"""
        
        # 格式化訊息
        text = self._format_message(notification)
        
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
        }
        
        async with aiohttp.ClientSession() as session:
            url = f"{self.api_url}/sendMessage"
            async with session.post(url, json=payload) as response:
                data = await response.json()
                return data.get("ok", False)
    
    def _format_message(self, notification: Notification) -> str:
        """格式化 Telegram 訊息"""
        
        emoji = {
            NotificationType.INFO: "ℹ️",
            NotificationType.SUCCESS: "✅",
            NotificationType.WARNING: "⚠️",
            NotificationType.ERROR: "❌",
            NotificationType.CONFIRM: "❓",
        }.get(notification.type, "📢")
        
        return (
            f"{emoji} *{notification.title}*\n\n"
            f"{notification.message}\n\n"
            f"_Source: {notification.source}_"
        )
```

### loom.toml 配置

```toml
[notification.channels.telegram]
enabled = true
type = "telegram"
bot_token = "${TELEGRAM_BOT_TOKEN}"
chat_id = "${TELEGRAM_CHAT_ID}"
parse_mode = "Markdown"
```

---

## Discord Notifier

### 用途

發送到 Discord Webhook。

```python
# loom/core/notification/adapters/discord.py
class DiscordNotifier(Notifier):
    """Discord 通知器"""
    
    def __init__(
        self,
        webhook_url: str,
        username: str | None = None,
        avatar_url: str | None = None,
    ):
        self.name = "discord"
        self.webhook_url = webhook_url
        self.username = username
        self.avatar_url = avatar_url
    
    async def send(self, notification: Notification) -> bool:
        """發送到 Discord"""
        
        # 格式化 Discord 嵌入
        embeds = [self._create_embed(notification)]
        
        payload = {
            "embeds": embeds,
        }
        
        if self.username:
            payload["username"] = self.username
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                json=payload,
            ) as response:
                return response.status == 204
    
    def _create_embed(self, notification: Notification) -> dict:
        """創建 Discord 嵌入"""
        
        color = {
            NotificationType.INFO: 0x3498db,      # 藍色
            NotificationType.SUCCESS: 0x2ecc71,   # 綠色
            NotificationType.WARNING: 0xf39c12,   # 橙色
            NotificationType.ERROR: 0xe74c3c,    # 紅色
            NotificationType.CONFIRM: 0x9b59b6,   # 紫色
        }.get(notification.type, 0x95a5a6)
        
        embed = {
            "title": notification.title,
            "description": notification.message,
            "color": color,
            "footer": {
                "text": f"Source: {notification.source}"
            },
            "timestamp": notification.created_at.isoformat(),
        }
        
        return embed
```

### loom.toml 配置

```toml
[notification.channels.discord]
enabled = true
type = "discord"
webhook_url = "${DISCORD_WEBHOOK_URL}"
username = "Loom Bot"
# avatar_url = "https://example.com/avatar.png"
```

---

## 自訂 Notifier

### 實現步驟

1. 繼承 `Notifier` 抽象類
2. 實現 `name` 屬性和 `send()` 方法
3. 在 `NotifierRegistry` 中註冊

```python
# 自訂 Email Notifier
class EmailNotifier(Notifier):
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: list[str],
    ):
        self.name = "email"
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
    
    async def send(self, notification: Notification) -> bool:
        """發送 Email"""
        
        msg = MIMEText(notification.message)
        msg["Subject"] = notification.title
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False
```

### 註冊自訂 Notifier

```python
# 在應用初始化時
registry.register(
    EmailNotifier(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        username="${EMAIL_USER}",
        password="${EMAIL_PASS}",
        from_addr="loom@example.com",
        to_addrs=["user@example.com"],
    ),
    channels=["critical", "default"]
)
```

---

## 總結

| Notifier | 用途 | 配置 key |
|----------|------|----------|
| CLI | 終端輸出 | 無 |
| Webhook | HTTP POST | `url`, `method` |
| Telegram | Telegram Bot | `bot_token`, `chat_id` |
| Discord | Discord Webhook | `webhook_url` |
| Email | SMTP 郵件 | `smtp_host`, `smtp_port`, `*_addr` |
| 自訂 | 用戶實現 | 視情況 |

所有 Notifier 都實現統一的介面：
- `name`: 識別名稱
- `send(notification)`: 發送通知
