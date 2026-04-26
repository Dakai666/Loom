# Web Tools（增量更新）

> 對 [doc/36-Web-Tools.md](doc/36-Web-Tools.md) 的增量更新，補充 Discord Notifier 工具細節。

---

## send_discord_embed 完整 schema

```python
send_discord_embed(
    title: str,
    description: str,
    color: str = "#0099ff",        # 支援 hex 或十進位
    fields: list[dict] | None = None,
    # dict: {"name": str, "value": str, "inline": bool}
    footer: str | None = None,
    thumbnail: str | None = None,
    image: str | None = None,
)
```

### Color 值對照（十進位，Discord API 要求）

| 用途 | hex | 十進位 |
|------|-----|--------|
| INFO | #5865F2 | 0x5865F2 |
| CONFIRM | #FEE75C | 0xFEE75C |
| REPORT | #57F287 | 0x57F287 |
| ALERT | #ED4245 | 0xED4245 |
| INPUT | #EB459E | 0xEB459E |

### fields 格式

```python
fields = [
    {"name": "工具", "value": "write_file", "inline": True},
    {"name": "狀態", "value": "SUCCESS", "inline": True},
]
```

- `inline=True`：最多 3 個一排（Discord 限制）
- `inline=False`：佔滿一行
- `name` 和 `value` 最多 1024 字元

### footer 格式

```python
footer={"text": "...", "icon_url": "..."}  # Discord Embed footer 格式
```

---

## send_discord_file 完整 schema

```python
send_discord_file(
    filepath: str,                    # workspace 相對路徑
    channel_id: str | None = None,   # 目標 channel（可選，預設 thread）
    thread_id: str | None = None,    # 目標 thread（可選）
)
```

- 自動判斷 MIME type（jpg/png/gif/mp4/pdf/txt）
- Discord 8MB 檔案大小限制
- 附檔上傳（MIME type）vs webhook 訊息附件（MIME type）

---

## Discord Thread 路由

`Notification.thread_id` 控制發送目標：

| thread_id | 行為 |
|-----------|------|
| `0` 或 `None` | 發送到 channel |
| `"123456"` | 發送到指定 thread |

Discord Bot 的 `send_discord_file` / `send_discord_embed` 工具（`loom/platform/discord/tools.py`）綁定固定 `thread_id`，由 `LoomDiscordBot._start_session()` 初始化時傳入。

---

## REST API 的 /webhook/reply

Discord 確認回覆透過 Loom REST API 接收：

```bash
curl -X POST http://localhost:8000/webhook/reply \
  -H "Content-Type: application/json" \
  -d '{"notification_id": "...", "result": "approved"}'
```

DiscordNotifier 的 `push_reply()` 被呼叫後，等待中的 `wait_reply()` 解除阻斷。

詳見 [doc/24b-Notification-Types.md](doc/24b-Notification-Types.md) 和 [doc/36b-Discord-Bot-平台.md](doc/36b-Discord-Bot-平台.md)。

---

*增量更新 | 2026-04-26 03:21 Asia/Taipei*