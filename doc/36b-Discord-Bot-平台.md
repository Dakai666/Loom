# Discord Bot 平台整合

> `loom/platform/discord/` — Loom 的 Discord 前端實作。

---

## 定位

Loom Discord Bot 讓 Loom 在 Discord 上運行，每個對話串（thread）是一個獨立的 LoomSession。多個 thread 可同時運行，互不干擾。

```
主頻道（lobby）
├─ 🧵「幫我分析這段程式碼…」   ← LoomSession A
├─ 🧵「今天的工作計劃」         ← LoomSession B
└─ 🧵「架構審查」             ← LoomSession C（目前）
```

---

## 檔案結構

```
loom/platform/discord/
├── bot.py          # LoomDiscordBot 主類
├── middleware.py    # TaskWriteDiscordReminderMiddleware
└── tools.py        # send_discord_file / send_discord_embed 工具工廠
```

---

## LoomDiscordBot（`bot.py`）

### 初始化

```python
class LoomDiscordBot:
    def __init__(
        self,
        model: str,
        db_path: str,
        channel_ids: list[int] | None = None,   # 限制運作的頻道
        allowed_user_ids: list[int] | None = None, # 限制可用的用戶
    ) -> None:
```

**安全閘門**：
- `allowed_user_ids`：白名單，設定後只回應這些用戶
- `allowed_channel_ids`：頻道白名單，設定後只在這些頻道（及其 thread）內運作

### 啟動方式

```bash
loom discord start \
    --token $DISCORD_BOT_TOKEN \
    --channel $DISCORD_CHANNEL_ID
    # --user $DISCORD_USER_ID  (可選：限制單一用戶)
```

### Session 生命週期

```
收到訊息
    ↓
判斷：thread → 繼續該 thread 的 session
        │ main channel → 建立新 thread → 啟動新 session
        ↓
_session.start()
    ├─ 註冊 Discord 工具（send_discord_file、send_discord_embed）
    ├─ authorize 這兩個工具
    ├─ Patch BlastRadiusMiddleware 的 _confirm_fn（→ Discord button confirm）
    ├─ 注入 TaskWriteDiscordReminderMiddleware（可選）
    ├─ 訂閱 skill_diagnostic 事件（→ 發送到 thread）
    └─ 訂閱 skill_promotion 事件（→ 發送到 thread）
    ↓
_bot.stream_turn(message, content, session)
    ↓（session 結束時）
_close_session(thread_id)
```

---

## 訊息路由邏輯

### 收到訊息時的決策樹

```
on_message(message)
    │
    ├─ 來自 bot 自身 → 忽略
    │
    ├─ 有 allowed_user_ids 且用戶不在名單內 → 忽略
    │
    ├─ 有 allowed_channel_ids 且不在名單內 → 忽略
    │
    ├─ 內容為 / 開頭 → 處理 slash command
    │
    ├─ 在 thread 內 → _get_thread_session() → _run_turn()
    │
    └─ 在 main channel → _create_session_thread() → _run_turn()
```

### Thread 建立邏輯

- 主頻道收到第一條訊息 → `message.create_thread()` 建立 thread
- Bot 在 thread 內 echo 使用者的第一條訊息作為 starter
- 同一 thread 再次收到訊息 → 自動取用該 thread 對應的 session

### Session 持久化

Bot 重啟後，`~/.loom/discord_threads.json` 會將 `thread_id → session_id` 的對應關係寫入磁碟，確保重啟後 thread 仍能恢復到正確的 session 上下文。

---

## Streaming 策略

Discord 有 rate limit（約 5 次編輯/5 秒），LoomDiscordBot 的 streaming 實作：

```
收到訊息 → 發送「◌ Thinking...」placeholder
    ↓
TextChunk 事件 → 累積文字，每 ~0.8s 編輯一次 placeholder
    ↓
ToolBegin → 在 placeholder 附加狀態行
    ↓
TurnDone → 編輯為最終文字（超過 2000 字自動分段）
```

---

## 確認流程（Confirm Flow）

### _ConfirmView

Discord 的確認不走 stdin，而是用 4 個 button：

```python
class _ConfirmView(View):
    # ✅ Allow (y)   → ConfirmDecision.ONCE（單次批准）
    # ⏱️ Lease (s)   → ConfirmDecision.SCOPE（scope lease）
    # ⚡ Auto (a)    → ConfirmDecision.AUTO（自動批准）
    # ❌ Deny (N)    → ConfirmDecision.DENY（拒絕）
```

BlastRadiusMiddleware 的 `_confirm_fn` 被 patch 成這個 Discord view，180s 超時自動視為 DENY。

### 整合方式

```python
confirm_fn = self._make_confirm_fn(thread_id)
for mw in session._pipeline._middlewares:
    if isinstance(mw, BlastRadiusMiddleware):
        mw._confirm = confirm_fn
        break
session._confirm_fn = confirm_fn
```

---

## Discord 工具

### send_discord_file

```python
def make_send_discord_file_tool(
    client: discord.Client,
    thread_id: int,
    workspace: Path,
) -> ToolDefinition
```

- 將 workspace 內的檔案發送到 Discord channel 或 thread
- 自動判斷 MIME type（jpg/png/gif/mp4/pdf/txt）
- **安全**：使用 `is_relative_to(workspace)` 防止路徑穿越
- 信任級別：**GUARDED**

### send_discord_embed

```python
def make_send_discord_embed_tool(
    client: discord.Client,
    thread_id: int,
) -> ToolDefinition
```

- 發送 Rich embed 面板
- Trust Level 為 **SAFE**（無寫入風險，只是呈現）
- Schema 支援：title / description / color（hex）/ fields（inline 陣列）

### fields 格式

```python
fields = [
    {"name": "工具", "value": "write_file", "inline": True},
    {"name": "狀態", "value": "SUCCESS", "inline": True},
]
```

`inline=True` → 最多 3 個一排；`inline=False` → 佔滿一行。

---

## TaskWriteDiscordReminderMiddleware

當 `loom.toml` 啟用時：

```toml
[task_write]
discord_reminder = true
```

每次 `task_write` 成功執行後，自動在 Discord thread 發送一個任務進度 embed：

```
🔄 任務進度
✅ ✅ audit — 審稿
▶️ → draft — 撰寫初稿到 tmp/draft.md
⬜ ⬜ scope — 與 user 確認研究範圍
⬜ ⬜ commit — 寫入最終位置 + 更新記憶
```

狀態映射：
- `completed` → ✅
- `in_progress` → ▶️
- `pending` → ⬜

---

## Skill 事件整合（Issue #120）

### Skill Diagnostic 事件

每次 `TaskReflector` 產生 `TaskDiagnostic`，自動發送到 Discord thread：

```
**Skill diagnostic:** skill:systematic_code_analyst: quality=3.2
› Consider clarifying the precondition_check documentation...
```

詳見度由 `session._reflection_visibility` 控制：
- `off` → 不發送
- `normal` → 單行 summary
- `verbose` → 包含 violated instructions 和 mutation suggestions

### Skill Promotion 事件

Issue #120 PR3 生命週期轉換時發送到 thread：

```
🔁 **Skill lifecycle:** promote skill:loom_engineer → v3 (from auto_c shadow)
↩️ **Skill lifecycle:** rollback skill:task_list → v2
🗑️ **Skill lifecycle:** deprecate skill:legacy_tool
```

---

## loom.toml 配置

```toml
[identity]
personality = "personalities/sisi.md"

[task_write]
discord_reminder = true

# Reflection visibility in Discord
# Values: "off" | "normal" | "verbose"
reflection_visibility = "normal"
```

---

## 依賴

```bash
pip install "loom[discord]"   # 安裝 discord.py
```

缺少 discord.py 時，import 會報有意義的錯誤訊息，引導使用者安裝。

---

## 與 NotificationRouter 的關係

| 層面 | LoomDiscordBot | NotificationRouter |
|------|---------------|------------------|
| 觸發方式 | Discord 訊息/button | `NotificationRouter.send()` |
| 回覆方式 | Button interaction | `wait_reply()`（polling/queue）|
| 確認按鈕 | _ConfirmView（4 button）| CLINotifier（stdin y/n）|
| 檔案發送 | send_discord_file tool | — |
| Embed 發送 | send_discord_embed tool | — |

兩者為平行架構：Discord Bot 有自己的工具和 middleware，NotificationRouter 的 DiscordNotifier 負責非同步通知（如 autonomy 結果報告）。

---

*文件草稿 | 2026-04-26 03:21 Asia/Taipei*