# Session 管理

Session 是 Loom 的對話上下文容器。每次聊天都是在一個 Session 中進行的。

---

## Session 結構

```python
# loom/core/session/models.py
@dataclass
class Session:
    """Session 模型"""
    
    id: str                           # 唯一 ID
    created_at: datetime              # 建立時間
    updated_at: datetime              # 最後更新時間
    
    # 配置
    personality: str                  # 使用的人格
    model: str | None                 # 使用的模型
    trust_level: TrustLevel           # 信任級別
    
    # 歷史
    messages: list[Message]           # 訊息歷史
    
    # 統計
    message_count: int                # 訊息數量
    token_usage: int                  # Token 使用量
    
    # 狀態
    status: SessionStatus             # 狀態
    metadata: dict                    # 額外資料
```

---

## Session 狀態

```python
# loom/core/session/models.py
class SessionStatus(Enum):
    """Session 狀態"""
    ACTIVE = "active"       # 進行中
    PAUSED = "paused"       # 暫停
    COMPLETED = "completed"  # 已完成
    ARCHIVED = "archived"   # 已歸檔
```

---

## Session 生命週期

```
┌─────────────────────────────────────────────────────────────┐
│                  Session 生命週期                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   create ──▶ active ──▶ complete                           │
│                │      │                                     │
│                │      └──▶ archive                          │
│                │                                            │
│                └──▶ pause ──▶ resume ──▶ active            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 建立 Session

```bash
# 建立新 session（chat 命令自動建立）
loom chat

# 指定 session ID 建立新 session
loom chat --session new_session_id

# 從範本建立
loom chat --session new_id --template research
```

### 恢復 Session

```bash
# 恢復上次 session
loom chat --resume

# 恢復特定 session
loom chat --session abc123
```

### 完成 Session

```bash
# 手動標記完成
loom sessions complete abc123

# 或直接退出 chat（會自動標記）
```

---

## Session 命令

### `loom sessions list`

列出所有 sessions。

```bash
loom sessions list [options]

# 選項
--status       按狀態過濾（active/paused/completed/archived）
--limit        最大數量（預設: 20）
--format       輸出格式（text/table/json）
```

**輸出範例**

```
$ loom sessions list

 Sessions
 ─────────────────────────────────────────────────────────
 ID          Status     Messages  Updated            Personality
 ─────────────────────────────────────────────────────────
 abc123      active     12       2 minutes ago     architect
 def456      completed  45       2 days ago        minimalist
 ghi789      archived   23       1 week ago        barista
 ─────────────────────────────────────────────────────────
```

### `loom sessions show`

查看 Session 詳情。

```bash
loom sessions show <session_id> [options]

# 選項
--messages      顯示訊息歷史
--stats         顯示統計
--metadata      顯示元資料
```

**輸出範例**

```
$ loom sessions show abc123

 Session: abc123
 ─────────────────────────────────────────────────────────
 Status:       active
 Personality:  architect
 Model:        gpt-4o
 Trust Level:  GUARDED
 ─────────────────────────────────────────────────────────
 Created:      2024-01-15 10:00:00
 Updated:      2024-01-15 10:30:00
 ─────────────────────────────────────────────────────────
 Messages:     12
 Token Usage:  8,234
 ─────────────────────────────────────────────────────────
```

### `loom sessions resume`

恢復 Session。

```bash
loom sessions resume <session_id>
loom sessions resume  # 恢復最近一個
```

### `loom sessions delete`

刪除 Session。

```bash
loom sessions delete <session_id> [options]

# 選項
--force         跳過確認
```

### `loom sessions export`

匯出 Session。

```bash
loom sessions export <session_id> [options]

# 選項
--format        格式（json/markdown/text）
--output, -o    輸出檔案
--include       包含內容（messages/memory/stats/all）
```

**範例**

```bash
# 匯出為 JSON
loom sessions export abc123 -o session.json

# 匯出為 Markdown
loom sessions export abc123 --format markdown -o session.md
```

### `loom sessions archive`

歸檔 Session。

```bash
loom sessions archive <session_id>
```

---

## 程式化操作

### Python API

```python
from loom.core.session.manager import SessionManager

manager = SessionManager()

# 建立新 session
session = await manager.create(
    personality="architect",
    model="gpt-4o",
    trust_level=TrustLevel.GUARDED,
)

# 列出 sessions
sessions = await manager.list(status=SessionStatus.ACTIVE)

# 獲取 session
session = await manager.get("abc123")

# 更新 session
await manager.update("abc123", personality="minimalist")

# 刪除 session
await manager.delete("abc123")

# 匯出 session
exported = await manager.export("abc123", format="json")
```

### Session Manager

```python
# loom/core/session/manager.py
class SessionManager:
    """Session 管理器"""
    
    def __init__(self, store: SessionStore):
        self.store = store
    
    async def create(self, **kwargs) -> Session:
        """建立新 session"""
        session = Session(
            id=self._generate_id(),
            created_at=datetime.now(),
            updated_at=datetime.now(),
            **kwargs
        )
        await self.store.save(session)
        return session
    
    async def list(
        self,
        status: SessionStatus | None = None,
        limit: int = 20,
    ) -> list[Session]:
        """列出 sessions"""
        return await self.store.list(status=status, limit=limit)
    
    async def get(self, session_id: str) -> Session | None:
        """獲取 session"""
        return await self.store.get(session_id)
    
    async def update(self, session_id: str, **updates) -> Session:
        """更新 session"""
        session = await self.get(session_id)
        
        for key, value in updates.items():
            setattr(session, key, value)
        
        session.updated_at = datetime.now()
        await self.store.save(session)
        
        return session
    
    async def delete(self, session_id: str):
        """刪除 session"""
        await self.store.delete(session_id)
    
    async def archive(self, session_id: str):
        """歸檔 session"""
        await self.update(session_id, status=SessionStatus.ARCHIVED)
```

---

## Session 儲存

### SQLite 後端

```python
# loom/core/session/store.py
class SQLiteSessionStore:
    """SQLite Session 儲存"""
    
    def __init__(self, db_path: str):
        self.db = aiosqlite.connect(db_path)
        await self._init_tables()
    
    async def _init_tables(self):
        """初始化資料表"""
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                personality TEXT NOT NULL,
                model TEXT,
                trust_level TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                token_usage INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                metadata TEXT
            )
        """)
```

### 自動清理

```toml
[session]

# 自動歸檔設定
[session.auto_archive]
enabled = true
after_days = 30      # 30 天後自動歸檔
max_active = 10      # 最多 10 個 active sessions

# 自動刪除
[session.auto_delete]
enabled = false
after_days = 90       # 90 天後自動刪除已歸檔的 sessions
```

---

## 總結

Session 管理命令：

| 命令 | 功能 |
|------|------|
| `loom sessions list` | 列出 sessions |
| `loom sessions show` | 查看詳情 |
| `loom sessions resume` | 恢復 session |
| `loom sessions delete` | 刪除 session |
| `loom sessions export` | 匯出 session |
| `loom sessions archive` | 歸檔 session |

Session 狀態：

| 狀態 | 意義 |
|------|------|
| ACTIVE | 進行中 |
| PAUSED | 暫停 |
| COMPLETED | 已完成 |
| ARCHIVED | 已歸檔 |
