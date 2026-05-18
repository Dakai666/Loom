# Session 管理

Session 是 Loom 的對話 runtime。實作上沒有 `loom/core/session/` package；權威入口是單檔 `loom/core/session.py` 的 `LoomSession`，持久化歷史則由 `loom/core/memory/session_log.py` 的 `SessionLog` 負責。

---

## 真實模組分工

| 模組 | 職責 |
|------|------|
| `loom/core/session.py` | `LoomSession` runtime、工具註冊、turn loop、pause/resume/cancel/stop |
| `loom/core/memory/session_log.py` | SQLite `sessions` / `session_log` 兩張表的讀寫 |
| `loom/platform/cli/main.py` | `loom chat`、`loom sessions` CLI 與 slash commands |
| `loom/platform/session_registry.py` | 進程內 active session registry，供 chime / Discord runtime 查找 |

Session 的 runtime 狀態與持久化資料沒有拆成 `models.py`、`manager.py`、`store.py`；這些舊路徑不存在。

---

## LoomSession

`LoomSession` 是 live agent runtime。它擁有主要 agent loop、permission context、tool registry、memory facade、prompt stack、ledger emitter、notification router 等 session-scoped 物件。

```python
# loom/core/session.py
class LoomSession:
    def __init__(
        self,
        model: str | None = None,
        db_path: str | None = None,
        resume_session_id: str | None = None,
        ...
    ):
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self._resume = resume_session_id is not None
        self.perm = PermissionContext(session_id=self.session_id)
        self.registry = ToolRegistry()
```

主要生命週期：

| 方法 | 說明 |
|------|------|
| `start()` | 載入 config、prompt stack、memory、tools、plugins、ledger、session log |
| `stream_turn(user_input)` | 執行一輪對話並串流事件 |
| `pause()` / `resume()` / `resume_with()` / `cancel()` | HITL pause boundary 控制 |
| `stop()` | flush memory health、compact、更新 session metadata、關閉資源 |

---

## SessionLog

`SessionLog` 是持久化 session metadata 與完整對話歷史的 SQLite gateway。

```python
# loom/core/memory/session_log.py
class SessionLog:
    async def create_session(
        self, session_id: str, model: str, title: str | None = None
    ) -> None: ...

    async def log_message(
        self,
        session_id: str,
        turn_index: int,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        raw_json: str | None = None,
    ) -> None: ...

    async def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]: ...
    async def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def load_messages(self, session_id: str) -> list[dict[str, Any]]: ...
    async def delete_session(self, session_id: str) -> None: ...
```

兩張資料表：

| 表 | 用途 |
|----|------|
| `sessions` | `session_id`、model、title、started_at、last_active、turn_count |
| `session_log` | 每個 user / assistant / tool 訊息，包含 raw assistant JSON 以支援 resume |

`create_session()` 使用 `INSERT OR IGNORE`，所以 resume 已存在 session 時不會覆蓋 metadata。`log_message()` 在 hot path 會吞掉例外並記錄 warning，避免 DB 問題阻塞 agent turn。

---

## Session 生命週期

```
new LoomSession
      │
      ▼
start()
      │
      ├─ new session: SessionLog.create_session()
      └─ resume: SessionLog.load_messages() + restore turn_index
      │
      ▼
stream_turn()
      │
      ├─ tool batch boundary 可 pause/resume/cancel
      ├─ 訊息寫入 SessionLog
      └─ turn metadata 更新
      │
      ▼
stop()
      ├─ memory health flush
      ├─ optional compaction
      └─ SessionLog.update_session()
```

Loom 目前沒有 enum 型 `ACTIVE / PAUSED / COMPLETED / ARCHIVED` session status 欄位。CLI 顯示的 session 清單主要依 `last_active` 排序，並用目前載入的 `session_id` 標示 active。

---

## CLI 操作

### 建立或恢復 chat session

```bash
# 建立新 session
loom chat

# 恢復最近 session
loom chat --resume

# 恢復指定 session
loom chat --session <session_id>

# 啟動時設定或覆蓋 title
loom chat --name "Research run"
```

TUI 模式在未指定 session 時會自動恢復最近一次 session：

```bash
loom chat --tui
loom chat --tui --session <session_id>
```

### 管理已保存 sessions

```bash
# 列出最近 sessions
loom sessions list --limit 20

# 顯示完整對話 replay
loom sessions show <session_id>

# 刪除 session metadata 與訊息
loom sessions rm <session_id>
```

目前沒有 `sessions complete`、`sessions archive` 或 `sessions export` CLI 子命令；需要匯出時通常從 `SessionLog.load_messages()` 或 DB 查詢層讀出。

---

## 程式化操作

建立 runtime session：

```python
from loom.core.session import LoomSession

session = LoomSession(model="claude-sonnet-4-6")
await session.start()
async for event in session.stream_turn("幫我整理今天的任務"):
    ...
await session.stop()
```

讀取已保存 session：

```python
import aiosqlite
from loom.core.memory.session_log import SessionLog

async with aiosqlite.connect("~/.loom/memory.db") as conn:
    log = SessionLog(conn)
    recent = await log.list_sessions(limit=20)
    messages = await log.load_messages(recent[0]["session_id"])
```

---

## 與 active registry 的關係

`SessionLog` 保存歷史；`loom/platform/session_registry.py` 只保存目前進程內活著的 `LoomSession` 物件。Discord chime 模式會用 registry 找指定 thread 對應的 active session，找不到時依 trigger 的 `target.fallback` 決定 skip 或開獨立 session。

---

## 總結

| 概念 | 現行實作 |
|------|----------|
| Live runtime | `LoomSession` |
| Persistent history | `SessionLog` |
| Session metadata | SQLite `sessions` table |
| Message replay | SQLite `session_log` table |
| CLI list/show/delete | `loom sessions list/show/rm` |
| In-process lookup | `SessionRegistry` |
