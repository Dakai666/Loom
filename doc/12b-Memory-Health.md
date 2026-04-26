# Memory Health 操作手冊

> Issue #133 — `loom/core/memory/health.py`。

---

## 定位

Memory Health 是 Loom 的記憶子系統自我觀測層，補足 `memory.db` 的儲存能力與「實際健康狀態」的診斷能力。

| 系統 | 觀察對象 | 資料粒度 |
|------|---------|---------|
| Memory（`memory.db`）| 記憶內容本身 | 按 entry / triple |
| **Memory Health** | 記憶子系統的 I/O 成敗 | 按 operation + success/failure |

設計目標：將「記憶子系統操作失敗時的靜默吞掉錯誤」替換為「可查詢、可持久化、可被 agent 自己看見的健康記錄」。

---

## 追蹤的 Operation 類型

| Operation | 說明 |
|-----------|------|
| `embedding_write` | 向量化寫入（semantic memory） |
| `embedding_search` | 向量化相似度搜尋 |
| `session_compress` | Episodic → Semantic 壓縮 |
| `session_log_write` | Session log 寫入 |
| `decay_cycle` | Decay cycle 執行 |
| `skill_evolution` | Skill evolution 候選產出 |
| `governed_upsert` | Governed semantic upsert |

---

## 資料表結構

```sql
CREATE TABLE memory_health (
    operation          TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    success_count      INTEGER NOT NULL DEFAULT 0,
    failure_count      INTEGER NOT NULL DEFAULT 0,
    last_failure_at    TEXT,          -- ISO timestamp
    last_failure_msg   TEXT,          -- 錯誤訊息（最多 500 字）
    updated_at         TEXT NOT NULL, -- 最後更新時間
    PRIMARY KEY (operation, session_id)
);
```

---

## MemoryHealthTracker

### 使用方式

```python
from loom.core.memory.health import MemoryHealthTracker

tracker = MemoryHealthTracker(db, session_id)
await tracker.ensure_table()     # 建立資料表
await tracker.load_prior()       # 讀取上個 session 的問題

# 在 memory operation 的 try/except 區塊中：
try:
    result = await semantic.upsert(entry)
    tracker.record_success("governed_upsert")
except Exception as exc:
    tracker.record_failure("governed_upsert", str(exc))

# Session 結束時：
await tracker.flush()   # 持久化到 DB
```

### 兩個層次的健康狀態

```
┌─────────────────────────────────────────┐
│  Current Session（記憶體計數器）           │
│  ├─ 讀取後馬上遞增，無 I/O 負擔          │
│  └─ session.stop() 時 flush() 到 DB      │
├─────────────────────────────────────────┤
│  Prior Sessions（DB 查詢）                │
│  ├─ 查詢最近 7 天有失敗的 operation       │
│  └─ load_prior() 在 session 啟動時呼叫    │
└─────────────────────────────────────────┘
```

### 熱路徑零 I/O

所有 `record_success()` / `record_failure()` 都是記憶體操作，**完全沒有 I/O**。只有 `flush()` 才寫 DB。

---

## HealthReport

```python
@dataclass
class HealthReport:
    session_id: str
    operations: dict[str, OperationHealth]   # 當前 session
    prior_session_issues: list[OperationHealth]  # 跨 session
```

### OperationHealth

```python
@dataclass
class OperationHealth:
    operation: str
    success_count: int = 0
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure_msg: str | None = None

    @property
    def total(self) -> int
    @property
    def failure_rate(self) -> float
    @property
    def is_healthy(self) -> bool  # failure_count == 0
    @property
    def status_icon(self) -> str
        # "OK" | "DEGRADED" (< 10%) | "FAILING"
```

### 渲染格式

#### `render_summary()` — 完整摘要

```markdown
## Memory Health: All systems nominal this session.

## Prior Session Issues (unresolved)
- **embedding_write**: 3 failure(s) at 2026-04-24T...
  Last error: Connection timeout after 10s
```

#### `render_agent_context()` — 注入 agent context 的濃縮版本

```markdown
⚠ MEMORY HEALTH ALERT — The following memory subsystems have recorded failures.
  • [PRIOR] embedding_write: 3 failure(s) — Connection timeout after 10s
  • [NOW] governed_upsert: 1/12 failed — Conflicting trust tier
```

當所有 operation 都健康時，回傳 `None`（不注入噪音）。

---

## 與 Agent 的整合

Agent 可透過 `memory_health` 工具主動查詢健康狀態：

```python
# loom/core/cognition/memory_health.py 中的 memory_health tool
# （實為 MemoryHealthTracker 的 wrapper tool）

result = await memory_health_tool(call)
# 回傳 HealthReport.render_summary() 的字串
```

平台在 session 啟動時注入 `render_agent_context()` 的內容（如果有 prior issues）。

---

## 與 Agent Telemetry 的比較

| | `memory_health` (#133) | `agent_telemetry` (#142) |
|---|---|---|
| 觀察對象 | 記憶子系統的 I/O 成敗 | agent 自己的行為 |
| 資料粒度 | 按 operation + success/failure | 按 dimension（tool_call / context_layout / memory_compression）|
| 跨 session | 有 prior session issues | 留空白（未來考慮）|
| Push 時機 | session 啟動注入 prior issues | turn 邊界注入異常 |
| 觸發方式 | 記憶體操作失敗時主動 record | agent 主動呼叫 `agent_health` |
| I/O 特性 | 熱路徑零 I/O，只有 flush() 有 I/O | 熱路徑零 I/O，persist_interval 控制批次寫入 |

兩者並行存在、互補：
- `memory_health` 告訴你「記憶子系統健康嗎？」
- `agent_health` 告訴你「我的行為正常嗎？」

---

## loom.toml 配置

```toml
[memory]
health.enabled = true   # 預設 true，關閉 = false
```

---

*文件草稿 | 2026-04-26 03:21 Asia/Taipei*