# Notification 完整類型定義

> 補充 `doc/23-Notification-概述.md`，提供完整的類型與 dataclass 定義。

---

## NotificationType 枚舉

定義在 `loom/notify/types.py`：

```python
class NotificationType(Enum):
    INFO     = "info"      # 一般資訊通知
    CONFIRM  = "confirm"   # 需要用戶確認
    INPUT    = "input"     # 需要用戶文字輸入
    ALERT    = "alert"     # 警告/緊急通知
    REPORT   = "report"    # 任務結果報告
```

---

## Notification dataclass

```python
@dataclass
class Notification:
    id: str                      # UUID，唯一識別
    type: NotificationType       # 通知類型
    title: str                   # 標題
    body: str                    # 主體內容
    trigger_name: str | None     # 來源 trigger 名稱
    timeout_seconds: int          # 等待回覆的超時秒數
    created_at: datetime
    metadata: dict[str, Any]     # 附加資料
    attachments: list[Path]      # 附帶檔案路徑
    inline_image: Path | None    # 內嵌圖片（Discord inline image）
    thread_id: str | None        # Discord thread ID（跨 thread 發送時使用）
```

### 各類型欄位使用說明

| 類型 | 必要欄位 | 選填欄位 |
|------|---------|---------|
| `INFO` | title, body | metadata, attachments |
| `CONFIRM` | title, body, timeout_seconds | thread_id |
| `INPUT` | title, body, timeout_seconds | thread_id |
| `ALERT` | title, body | metadata |
| `REPORT` | title, body | metadata, attachments |

---

## ConfirmResult 枚舉

定義在 `loom/notify/types.py`：

```python
class ConfirmResult(Enum):
    APPROVED = "approved"   # 用戶批准
    DENIED   = "denied"     # 用戶拒絕
    TIMEOUT  = "timeout"    # 超時（沒有明確回覆）
```

### TIMEOUT ≠ DENIED

這是設計原則：`TIMEOUT` 表示「無法確認意圖」（網路問題、用戶不在），`DENIED` 表示「用戶明確拒絕」。

行為差異：
- CONFIRM + TIMEOUT → `ActionPlanner` 保守跳過（不下放 EXECUTE）
- HOLD + TIMEOUT → 不執行
- CONFIRM + DENIED → 明確不執行， PenaltyBox 計數 +1

---

## BaseNotifier 介面

```python
class BaseNotifier(ABC):
    channel: str  # "cli" | "discord" | "webhook" | "telegram"

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """發送通知到目標平台"""

    async def wait_reply(self, notification: Notification) -> ConfirmResult:
        """等待用戶回覆（default: 自動 APPROVED）"""
        return ConfirmResult.APPROVED

    def push_reply(self, notification_id: str, result: ConfirmResult) -> None:
        """外部系統推送回覆時呼叫（用於 webhook/telegram）"""
        pass
```

---

## 已實現的 Notifier 對照

| Notifier | channel | 位置 | 回覆方式 |
|----------|---------|------|---------|
| `CLINotifier` | `cli` | `loom/notify/adapters/cli.py` | stdin `y/n` 輸入 |
| `DiscordNotifier` | `discord` | `loom/notify/adapters/discord.py` | push_reply()（由 REST API 呼叫）|
| `WebhookNotifier` | `webhook` | `loom/notify/adapters/webhook.py` | push_reply()（外部 HTTP handler）|
| `TelegramNotifier` | `telegram` | `loom/notify/adapters/webhook.py` | push_reply()（bot reply webhook）|

---

## DiscordNotifier 的 send_discord_file / send_discord_embed

### send_discord_file

在 `loom/platform/discord/tools.py` 中實作：

```python
async def send_discord_file(
    filepath: str,       # workspace 相對路徑
    channel_id: str | None = None,
    thread_id: str | None = None,
    caption: str = "",
) -> str:
```

- 發送 workspace 內的檔案到 Discord channel 或 thread
- 自動判斷 MIME type（jpg/png/gif/mp4/pdf/txt）
- Discord 有 8MB 檔案大小限制（MIME type 決定上傳方式）
- `thread_id` 可用於跨 thread 發送

### send_discord_embed

```python
async def send_discord_embed(
    title: str,
    description: str,
    color: str | int = 0x5865F2,   # Discord 色值（十進位）
    fields: list[dict] | None = None,
    # fields[i] = {"name": str, "value": str, "inline": bool}
    footer: str | None = None,
    thumbnail: str | None = None,
    image: str | None = None,
    channel_id: str | None = None,
    thread_id: str | None = None,
) -> str:
```

#### fields 格式

```python
fields = [
    {"name": "工具", "value": "write_file", "inline": True},
    {"name": "狀態", "value": "SUCCESS", "inline": True},
    {"name": "執行時間", "value": "42ms", "inline": True},
]
```

`inline=True` 的 fields 會併排顯示（最多 3 個一排）；`inline=False` 佔滿一行。

#### 顏色對照

| 用途 | color（十進位）| RGB |
|------|--------------|-----|
| INFO | 0x5865F2 | #5865F2（Discord blurple）|
| CONFIRM | 0xFEE75C | #FEE75C（黃色）|
| REPORT | 0x57F287 | #57F287（綠色）|
| ALERT | 0xED4245 | #ED4245（紅色）|
| INPUT | 0xEB459E | #EB459E（粉紅色）|

---

## Loom REST API 的 /webhook/reply 端點

用於 Discord/Telegram 的回覆接收（`loom/platform/api/server.py`）：

```python
@app.post("/webhook/reply")
async def receive_reply(request: Request):
    body = await request.json()
    notification_id = body["notification_id"]
    result = ConfirmResult(body["result"])  # "approved" / "denied"
    notifier.push_reply(notification_id, result)
    return {"status": "ok"}
```

呼叫方式（來自 Discord 或其他平台）：
```bash
curl -X POST http://localhost:8000/webhook/reply \
  -H "Content-Type: application/json" \
  -d '{"notification_id": "...", "result": "approved"}'
```

---

## 通知的 thread 路由

`Notification.thread_id` 控制 Discord 發送目標：
- `thread_id=None` → 發送到 channel
- `thread_id="123456"` → 發送到 thread

AutonomyDaemon 的 `notify_thread` 配置對應到這裡：

```toml
[[autonomy.schedules]]
name          = "morning_briefing"
notify_thread = 1490024181994225744   # Discord thread ID
```

通知時自動將 `thread_id` 帶入，確保回覆在同一 thread。

---

*文件草稿 | 2026-04-26 03:10 Asia/Taipei*