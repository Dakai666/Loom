# AgentLedger 設計文件 — Quest B Phase 1

> **狀態**：Phase 1 設計鎖定（2026-05-08）
> **來源**：integrate from #316 Round 1-6 consensus
> **下一步**：Phase 2 實作 issue（按 §11 六步驟線性）
>
> 本文件是 AgentLedger 的設計權威。實作分歧時以本文件為準；本文件未涵蓋的細節，按 §10「設計原則」推導。

---

## 1. 文件目的

Loom 目前有六種事件流在描述同一件事 — agent 在時間中如何行動：

```
ActionRecord / ExecutionEnvelope / SessionLog / TaskList / MemoryWrite / JudgeVerdict
```

本設計把這些收斂成單一 **AgentLedger**，並為未來 Turn Graph Branching、Operator Replay、#314 Capability Sheet 自動投影預留地基。

對應 doc/52 §1.4（Quest B 是中樞神經 / hub）。Quest D 的 replay corpus、#314 的 capability aggregation、Memory v2 的 emit、未來 Turn Graph Branching 全部下游。

---

## 2. 設計原則（推導未涵蓋細節時的判準）

按優先級排序：

1. **Append-only 純度** — Ledger 寫入後 immutable。例外：`thought.full_text` 容許 late-arrival（buffered → turn_end 決定 commit/discard），這是經 round 2 顯式同意的特例。
2. **單向耦合** — 各 subsystem 可以 emit 進 ledger，ledger 不反向依賴任何 subsystem。Ledger 是純 sink。
3. **Read-only subscriber** — Ledger 是 after-the-fact，subscriber 不能阻擋、修改、取消事件。介入是 middleware 層的事，不是 ledger 層。
4. **Consumer 不知道 storage 細節** — 三層介面（push iterator / pull fluent / replay snapshot）封閉所有 SQLite 細節，未來換存儲零改動。
5. **Vibe-coding 友好** — 設計優化 agent 讀寫便利，不為「人類維護者覽閱」做妥協。但要為解 query 與審計問題做妥協。
6. **Solo operator 假設** — 不做 HA、跨 user、即時 streaming、deprecation cycle。簡化 readers 多版本相容、不簡化 writers append-only。
7. **預留比 migration 便宜** — 加未實作欄位（`branch_id`）比未來 schema migration 痛。

---

## 3. Event Taxonomy（A）

### 3.1 九種事件類型（中粒度）

| event_type | 子類型 / phase | 出處 |
|---|---|---|
| `turn_start` | — | Session |
| `turn_end` | outcome: `clean` / `retry` / `abandoned` / `error` | Session |
| `thought` | — | Session |
| `model_event` | — | Session |
| `tool_lifecycle` | `BEGIN` / `STATE_CHANGE` / `END` / `ROLLBACK` | Middleware |
| `permission_decision` | `grant` / `deny` / `scope` | Middleware |
| `memory_op` | `read` / `write` / `compact` / `batch_read` | MemoryFacade |
| `task_mutation` | `write` / `done` / `modify` / `abandon` | TaskScheduler |
| `judge_verdict` | — | Session |
| `artifact_emit` | code / image / audio / 其他 | Session |
| `env_observation` | external / timer / notification / contradiction / anomaly | AutonomyDaemon / MemoryPulse |

`tool_lifecycle` **取代既有 `ActionRecord`**，state_history 內嵌進事件 payload，不另存。

> **v0.3 實作簡化**（PR #330 / Phase 2 Step 2，#337 lock-in）：
> 4 phase 列舉中 v0.3 只 emit `BEGIN` + `END`：
> - `STATE_CHANGE` **不 emit**。每 state transition 一筆事件對 v1 太重，且 `state_history` 已在 END payload 內完整保留。Mid-flight 中介狀態（PENDING / AUTHORIZED / EXECUTING / awaiting_confirm…）在 view 上一律 coarsen 成 `executing`——因為 sub-state granularity 不是 user-visible（spinner 期間 UI 一視同仁）。projector `_derive_state` tier 2「BEGIN 後沒 END → executing」就是這條規則的單一實作點。Schema 仍保留 `phase` string，未來真有 high-frequency capture 需求再 opt-in。
> - `ROLLBACK` 折疊進 `END.rolled_back=True`；REVERTED 在 memorialize 時已是最終狀態，獨立 ROLLBACK event 對 reader 不增加資訊。
> - #337 同步移除 `LoomSession._live_record_for` / `ExecutionEnvelope.records` 的 transitional bridge——projector 現在純從 ledger 讀取，沒有 live ActionRecord lookup 路徑。`ExecutionEnvelope` 變 thin lifecycle marker（id + 時戳），不再有 records list。

> **v0.3 task_mutation operation 簡化**（PR #330）：
> 列舉 `write/done/modify/abandon` 四種 operation 的設計來自 #205 collapse 之前的假設。Post-#205 task_write 是唯一 mutation 入口，done/modify/abandon 都被編碼進 status 欄位、沒有獨立呼叫點。每次 task_write 落 `operation="write"` 並帶完整 `task_state` snapshot，done/modify/abandon 為 reader-side derivable from successive snapshots。

### 3.2 粒度策略

- **預設中粒度（9 種）**
- **Opt-in debug 細粒度** — `LOOM_LEDGER_DEBUG=token_stream,prompt_stack,...` 環境變數可打開研究/debug 用事件，不污染主 ledger
- **PromptStack 不獨立發事件** — turn 內 PromptStack 不變，會變的場景（compaction / model switch / cancel rebuild）已被其他事件覆蓋

### 3.3 thought 事件的特殊處理

thought 是因果鏈的關鍵，但 full_text 大且敏感。採 **event-driven full_text capture**：

```python
THOUGHT_EXTERNAL_THRESHOLD = 50_000  # bytes
LEDGER_BLOB_DIR = Path(".loom/ledger_blobs/")  # 不進 git

@dataclass
class ThoughtPayload:
    digest: str                            # sha256 of full_text，always
    full_text: str | None = None           # ≤ threshold inline
    external_ref: str | None = None        # > threshold 外部檔的相對路徑
```

**觸發 capture 的條件**（任一成立則保留 full_text）：

```python
if verdict in (FAIL, ERROR):
    capture_full_text = True
elif turn_end.outcome in (retry, abandoned):
    capture_full_text = True
elif any artifact_emit.size_bytes > 10_000:
    capture_full_text = True
```

**實作機制**（buffer + turn_end commit-or-discard）：

```python
# Session 內部 — turn lifecycle 期間維護
self._thought_buffer: dict[event_id, str] = {}        # 暫存 full_text
self._thought_capture_signals: list[int] = []          # artifact size 信號累積器

# emit artifact_emit 時，順手累積 capture 信號（避免 turn_end 時 ledger query）
self._thought_capture_signals.append(artifact.size_bytes)

# 寫 thought 事件時 — ledger 暫不存全文
ledger.emit(ThoughtEvent(
    event_id="evt_thought_42",
    text_digest=sha256(text),
    full_text=None,
))
self._thought_buffer["evt_thought_42"] = text         # 內存暫存

# turn_end 時決定 — 全部信號都從 session-level state 取，零 ledger query
should_capture = (
    verdict in (FAIL, ERROR)
    or outcome in (retry, abandoned)
    or any(s > 10_000 for s in self._thought_capture_signals)
)
if should_capture:
    for evt_id, full_text in self._thought_buffer.items():
        ledger.update(evt_id, {"full_text": full_text})  # late-arrival 特例
self._thought_buffer.clear()
self._thought_capture_signals.clear()
```

**設計原則**：所有 capture 觸發信號應在 emit 時累積進 session-level state，不要在 turn_end 時跨層 query ledger。Session 是 emitter 同時也持有完整 turn-local 信號，是最便宜的 source of truth。

**Buffer cap = 20 thoughts**：先用此值觀察，autonomy 連跑場景實戰調整。

**> 50KB 走 blob storage**：

```python
def _store_thought(raw_text: str) -> ThoughtPayload:
    digest = sha256(raw_text.encode()).hexdigest()
    if len(raw_text.encode()) <= THOUGHT_EXTERNAL_THRESHOLD:
        return ThoughtPayload(full_text=raw_text, digest=digest)
    blob_path = f"{turn_id}/{event_id}.txt"
    (LEDGER_BLOB_DIR / blob_path).write_text(raw_text)
    return ThoughtPayload(full_text=None, external_ref=blob_path, digest=digest)
```

Blob 目錄結構：`.loom/ledger_blobs/{turn_id}/{event_id}.txt`。`external_ref` 顯式存 path（為未來搬到 S3/object store 留 URI 替換彈性）。

### 3.4 PromptStack snapshot

每個 `turn_start` 帶 PromptStack snapshot（不存全文，存 hash + components）：

```python
TurnStart(
    turn_id=...,
    prompt_stack_hash="sha256:...",
    prompt_stack_components={
        "system": "loom-default-v3",
        "persona": "neutral",
        "memory_layers": ["semantic_recent_5", "skill_genome"],
        "tool_catalog_size": 47,
        "context_token_count": 8420,
    },
    full_text=None,                        # 預設 None，debug mode opt-in
)
```

`prompt_stack_hash` 作 `context_token_count` 的 cache key（components 不變 → 跳過 tokenize）。

### 3.5 memory_op.read 邊界

| 來源 | 處理 |
|---|---|
| Explicit retrieval（agent 主動 search memory） | 每次都記 `MemoryOp(operation="read", trigger="agent_search")` |
| Background prefetch / Hook（MemoryPulse 自動注入） | 每 turn 聚合成單一 `MemoryOp(operation="batch_read", memory_ids=[...], trigger="hook_g_preheat")` |

避免每 turn N 次 read 撐爆 ledger。

---

## 4. Identity & Reference（B）— Level 3 Identity

### 4.1 五個 ID 欄位

每個事件至少帶這五個 ID：

```python
event_id         # 唯一識別（per event）
turn_id          # 屬於哪輪對話
parent_event_id  # 因為哪個事件而發生（直接因果）
correlation_id   # 整串 business action 共享
branch_id        # Turn Graph Branching 預留，永遠是 string，預設 "main"
```

### 4.2 correlation_id 派發策略

**主策略：middleware 自動繼承**

middleware pipeline 進來的事件帶上 parent_event_id 反查或 thread-local 取得 correlation_id，離開時自動傳遞。降低 agent 主動標記成本。

**三類例外（自開新 correlation_id，不繼承 parent）**：

| Event type | 理由 |
|---|---|
| `env_observation`（contradiction / anomaly） | 系統級觀察，不是 agent 決策 |
| `memory_op.type=compact` | 後台維護工作，不屬於任何 user-facing action |
| `turn_end.outcome=error` | Error 本身是獨立 root cause unit |

**延伸規則：exception 觸發的後續反應鏈，繼承 exception 自身的 correlation_id**（不回去繼承 parent turn）。

範例：

```
turn_xyz (correlation: "user_question_pytest")
  └─ tool_lifecycle (run_bash)
      └─ memory_op write
          └─ env_observation: contradiction (correlation: "contradiction_xxx" NEW)
              └─ thought (correlation: "contradiction_xxx") ← 繼承 exception
                  └─ memory_op write (修正)
```

未來查「這個矛盾最後怎麼被處理的」一個 correlation_id 涵蓋整條反應鏈。

**少數情境可 explicit override**（例如 dreaming 任務想把整段 reflection 視為同一 correlation）。

### 4.3 subagent 邊界

Subagent = **新 turn + correlation_id 串**（不 nested）：

- subagent 有獨立 prompt_stack、自己的 tool sequence、自己的 verdict
- subagent 出問題時要能單獨 replay → 新 turn 設計直接就能跑
- **subagent 繼承 launching turn 的 branch_id**（讓「哪個 branch 的哪次任務 spawn 的 subagent」可追溯）

副作用：turn graph 變成圖而非樹（一個 launching turn 可指向多個 child turn）。可接受。

### 4.4 branch_id v1 語意

| 規則 | 說明 |
|---|---|
| 永遠有值 | string，**不允許 None**，預設 `"main"` |
| Turn 一旦開始就固定 | `turn_start` 時 branch_id 寫死，整個 turn 內不變 |
| 創建新 branch 須 explicit | 透過未來的 `fork_turn(parent_turn_id, branch_name)` API 產生非 `"main"` 值（**v1 不實作此 API**，schema 預留） |
| 命名規則 | `"main"` 為保留字；fork 出來的 branch 形如 `"alt_001"` / `"replay_2026-05-08"` |
| Branch 不合併（v1） | 分出後獨立到底，merge 是 v2 議題 |
| 跨 branch 查詢 | query 預設只查 `branch_id="main"`，查 alternate 須 explicit filter |
| Subagent 繼承 | subagent 是新 turn，但繼承 launching turn 的 branch_id |

---

## 5. Storage（C）

### 5.1 獨立 ledger.db

不共用 `memory.db`。獨立檔案 `~/.loom/ledger.db`，sqlite WAL 模式。

理由：ledger 寫入頻率高、分檔可獨立 retention/rotation、避免 memory.db 變肥。

### 5.2 Schema — 窄 schema + JSON payload + generated columns

```sql
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    parent_event_id TEXT,
    correlation_id TEXT NOT NULL,
    branch_id TEXT NOT NULL DEFAULT 'main',
        -- v2 Turn Graph Branching 實作時，由 fork primitive 生成 non-"main" 值
        -- 詳見 Phase 2 issue（待開）
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    payload JSON NOT NULL,

    -- 高頻 query 拉 generated columns（不額外存）
    tool_name TEXT GENERATED ALWAYS AS (json_extract(payload, '$.tool_name')),
    verdict TEXT GENERATED ALWAYS AS (json_extract(payload, '$.verdict')),
    skill_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.skill_id')),
    predecessor_memory_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.predecessor_memory_id'))
);
```

### 5.3 Indexes（6 + 1）

全部以 `branch_id` 為前綴 covering index — v2 turn graph branching 不用重建：

```sql
CREATE INDEX idx_turn          ON events (branch_id, turn_id, timestamp);
CREATE INDEX idx_correlation   ON events (branch_id, correlation_id, timestamp);
CREATE INDEX idx_session_recent ON events (branch_id, session_id, timestamp DESC);
CREATE INDEX idx_tool          ON events (branch_id, tool_name, timestamp);
CREATE INDEX idx_verdict       ON events (branch_id, verdict, timestamp);
CREATE INDEX idx_parent        ON events (parent_event_id);
CREATE INDEX idx_predecessor   ON events (predecessor_memory_id);  -- compaction walk
```

### 5.4 Retention

容量估算：中粒度 ~30 events/turn × 50 turns/day × 500 bytes ≈ 275 MB/年。sqlite 完全夠用。

- **v1 不刪、不 rotate**
- 留 `LedgerStore.maintenance()` hook（v1 內只做 `PRAGMA optimize` + `VACUUM`）
- thought.full_text 個別 row max = 50KB（超出走 blob storage，見 §3.3）

### 5.5 跨 session 查詢

| 層 | v1 行為 |
|---|---|
| Schema | `session_id` 是 column，所有 index 不限 session（cost 0） |
| Pull API | 預設 query 限定當前 session，但接受 `session_id=None` 跨 session |
| UI | 不主動提供 cross-session 視圖 |
| v2 升級成本 | 0 — 純加 UI / aggregator，schema 已就緒 |

### 5.6 跟 memory.db 的關聯（Option C：ref + minimal snapshot + content digest）

```python
# 一般 write / read
MemoryOp(
    memory_id="mem_xyz",                  # live correlation 用
    operation="write",
    type_summary="semantic_triple",       # 高頻 aggregate query 維度
    trust_tier="user_explicit",           # Quest D 算「低 trust 寫入比例」用
    content_digest="sha256:...",          # 驗證 memory.db row 是否被後續覆蓋
    # 不存 embedding、不存完整 content
)
```

**digest 用 content hash，不含 embedding**：embedding 是 cache 是實現細節、model 升級會 break replay、content 才是 semantic artifact。

### 5.7 Compaction — pair 版（predecessor + successor）

```python
MemoryOp(
    operation="compact",
    memory_id="mem_xyz",                    # 被取代的
    predecessor_memory_id="mem_abc",        # 上一個前身（generated column 索引）
    successor_memory_id="mem_pqr",          # 取代它的
    content_digest="sha256:...",
    type_summary="semantic_triple",
    trust_tier="user_explicit",
)
```

每個 compact event 只存兩條鄰邊。完整 chain 由 `resolve_memory_id()` helper walk 出來：

```python
def resolve_memory_id(mem_id: str) -> str:
    """Walk compaction chain forward to current live memory_id."""
    current = mem_id
    while True:
        event = ledger.query(
            event_type="memory_op",
            operation="compact",
            predecessor_memory_id=current,
        ).first()
        if not event:
            return current
        current = event.successor_memory_id
```

Consumers 不需要知道 compaction — 拿著舊 memory_id 過來，helper 自動解到 current。

---

## 6. Projection Contract（D）

三層介面，consumer 只認這三層，不知道 SQLite 細節：

### 6.1 Push API — async iterator

```python
async for event in ledger.subscribe(
    event_types=["tool_lifecycle", "judge_verdict"],
    correlation_id=current_corr,
    branch_id="main",
    replay_from=now() - timedelta(seconds=10),  # tail -f --since 模式
) as subscriber:
    handle(event)
```

**規格**：

- async iterator 為主接口（與既有 envelope subscribe 同形狀）
- 每 subscriber 獨立 bounded buffer（預設 100 events）
- 落後 → drop-oldest with warning log（不阻塞 publisher）
- `replay_from` 銜接點 v1 即做（TUI/Discord 重連剛需）

**LedgerSubscriber 觀察 properties**：

```python
class LedgerSubscriber:
    @property
    def is_live(self) -> bool:
        """False when buffer has dropped events or subscriber is behind."""
        # v1 day 1 必有

    @property
    def lag_events(self) -> int:
        """Number of events currently buffered behind live edge."""
        # v1 補丁，可後續加

    @property
    def last_event_timestamp(self) -> datetime | None:
        """Timestamp of most recent event delivered."""
        # v1 補丁
```

> **v0.3 實作決策 — `is_live` monotonic 語意**（PR #332 / Phase 2 Step 4）：
> 一旦 buffer drop 發生，`is_live` 永久為 False，不會在 buffer 排空後切回 True。理由：drop 是歷史事實，被丟掉的事件無法復原；切回 True 會給 consumer 「我現在是最新的」錯覺，但 stream 已有永久缺口。Consumer 看到 False 就應 re-subscribe（fresh subscriber 的 dropped_total=0、is_live=True）。
> `lag_events` 是另一個獨立信號 — 純 buffer 長度，consumer 想知道「現在落後多少」可單獨讀。

Platform 自己決定 lag indicator 呈現（TUI 彩色閃爍 / Discord 編輯 card）。

### 6.2 Pull API — fluent 主 + raw SQL 副

```python
events = (
    ledger.events
        .where(event_type="tool_lifecycle", verdict="FAIL")
        .since(yesterday)
        .order_by("timestamp")
        .limit(100)
        .all()
)

# Aggregate 限制在簡單情境
ledger.events.where(tool_name="run_bash").since(yesterday).count()
ledger.events.where(verdict="FAIL").group_by("tool_name").count_by()
```

**支援動詞**：`.where()` / `.where_payload()` / `.since()` / `.until()` / `.order_by()` / `.limit()` / `.all()` / `.first()` / `.count()` / `.group_by(field).count_by()`。

**不做完整 ORM**。複雜需求走 raw SQL escape hatch：

```python
ledger.execute_sql("""
    SELECT tool_name, COUNT(*) as denials
    FROM events
    WHERE event_type='permission_decision' AND verdict='deny'
      AND timestamp > ?
    GROUP BY tool_name
""", [thirty_days_ago])
```

**Type safety 分階段**：

| 階段 | 策略 |
|---|---|
| **v1** | 所有欄位純 string，doc/53 列出支援的 event_type / phase 值。實作簡單、加新事件類型只改 schema 一處 |
| **v2** | top-level fields 升 `Literal[...]` union（IDE + type checker），固定 closed-set 用 StrEnum，payload 自由欄位維持 plain string |

### 6.3 Replay primitive — 兩層

```python
# Layer 1: raw event sequences
ledger.replay.events_for_turn(turn_id)
ledger.replay.events_for_correlation(corr_id)
ledger.replay.events_for_session(session_id)

# Layer 2: reconstructed snapshots
ledger.replay.turn_snapshot(turn_id) -> TurnSnapshot
ledger.replay.correlation_snapshots(corr_id) -> list[TurnSnapshot]
```

**v1 不放 fork primitive**。`branch_id` schema 留 comment 預告 v2 入口。

### 6.4 各 Platform 投影模式

| Platform | 模式 | 主要動作 |
|---|---|---|
| **CLI 線性流** | push | `async for event: cli.render(event)` |
| **TUI ExecutionDashboard** | push + pull | `replay_from=now-10s` 啟動，maintain envelope view，is_live False 時顯示 lag indicator |
| **Discord condensed cards** | push | 一張 active envelope card 隨事件更新；is_live False 編輯 card 顯示 warning |
| **Memory compaction** | push (`turn_end` trigger) + pull | subscribe → pull 近 N turns → extract facts |
| **#314 Capability Sheet** | pull (raw SQL aggregate) | 30 天 deny rate / tool usage 統計 |
| **Quest D corpus build** | pull (failed verdicts) → replay snapshots | fluent + replay.turn_snapshot |
| **JSON API trace** | pull (turn snapshot serialize) | replay.correlation_snapshots → JSON |

### 6.5 Subscriber Read-Only 邊界

Subscriber 是純觀察者，不能阻擋、不能修改、不能取消事件。Ledger 是 after-the-fact append-only。

如果未來想介入 agent 行為，那是 **middleware 層** 的事，不是 ledger 層。

---

## 7. Schema Dataclass 純宣告

> **本節是契約**：dataclass 形狀為 Phase 2 實作的 reference。實作可加方法、helper，但 field 形狀不能變（除非走 §9 schema evolution 流程）。

### 7.1 Base Event

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

@dataclass
class LedgerEvent:
    """所有 ledger 事件的基底。

    Field order 注意：non-default 欄位必須全部在 default 欄位前面（Python dataclass 限制）。
    """
    # 必填欄位（無 default）— 必須先列
    event_id: str                          # 唯一識別
    session_id: str
    turn_id: str
    correlation_id: str
    event_type: str                        # see §3.1 taxonomy
    timestamp: datetime                    # UTC Unix epoch (REAL)，見 §9.5
    payload: dict[str, Any]                # event-type-specific

    # 可選欄位（有 default）— 必須後列
    parent_event_id: str | None = None
    branch_id: str = "main"                # v1 永遠 "main"

    # payload schema_version 慣例（不是 column，是 payload 內 key）
    # payload["schema_version"]: int = 1
```

### 7.2 Per-event-type Payload Schemas（v1 schema_version=1）

每個 payload 都 implicitly 帶 `"schema_version": 1`（見 §9）。為簡潔，下表略。

```python
# turn_start
@dataclass
class TurnStartPayload:
    schema_version: int = 1
    prompt_stack_hash: str
    prompt_stack_components: dict[str, Any]
    full_text: str | None = None           # debug mode 才存

# turn_end
@dataclass
class TurnEndPayload:
    schema_version: int = 1
    outcome: str                           # "clean" / "retry" / "abandoned" / "error"
    duration_ms: int
    token_usage: dict[str, int]            # {"prompt": ..., "completion": ..., "total": ...}

# thought
@dataclass
class ThoughtPayload:
    schema_version: int = 1
    digest: str                            # sha256 of full_text
    full_text: str | None = None           # ≤ 50KB inline；> 50KB 走 external_ref
    external_ref: str | None = None        # blob path: {turn_id}/{event_id}.txt
    duration_ms: int
    produced_tool_calls: int

# model_event
@dataclass
class ModelEventPayload:
    schema_version: int = 1
    model: str                             # "claude-opus-4-7" 等
    tier: int
    token_usage: dict[str, int]

# tool_lifecycle (取代 ActionRecord)
@dataclass
class ToolLifecyclePayload:
    schema_version: int = 1
    phase: str                             # "BEGIN" / "STATE_CHANGE" / "END" / "ROLLBACK"
    tool_name: str
    tool_call_id: str
    args_digest: str                       # sha256 of args
    args: dict[str, Any] | None = None     # 大 args 可 truncate / digest only
    result_digest: str | None = None       # END phase 才有
    result_summary: str | None = None
    state_history: list[dict] = field(default_factory=list)  # 內嵌 state 變化序列
    rolled_back: bool = False
    error: str | None = None

# permission_decision
@dataclass
class PermissionDecisionPayload:
    schema_version: int = 1
    decision: str                          # "grant" / "deny" / "scope"
    tool_call_id: str                      # ref tool_lifecycle BEGIN
    trust_level: str
    scope_grant: dict | None = None
    reason: str | None = None

# memory_op
@dataclass
class MemoryOpPayload:
    schema_version: int = 1
    operation: str                         # "read" / "write" / "compact" / "batch_read"
    memory_id: str | None = None
    memory_ids: list[str] | None = None    # batch_read only
    predecessor_memory_id: str | None = None  # compact only
    successor_memory_id: str | None = None    # compact only
    type_summary: str | None = None        # "semantic_triple" / "skill_genome" / ...
    trust_tier: str | None = None
    content_digest: str | None = None
    trigger: str | None = None             # "agent_search" / "hook_g_preheat" / ...

# task_mutation
@dataclass
class TaskMutationPayload:
    schema_version: int = 1
    operation: str                         # "write" / "done" / "modify" / "abandon"
    task_id: str
    task_state: dict[str, Any] | None = None  # snapshot of task after mutation

# judge_verdict
@dataclass
class JudgeVerdictPayload:
    schema_version: int = 1
    verdict: str                           # "PASS" / "FAIL" / "ERROR" / "CONCERN"
    confidence: float                      # 0.0–1.0
    reason: str                            # short text
    judged_subject: str                    # "turn" / "tool_result" / ...

# artifact_emit
@dataclass
class ArtifactEmitPayload:
    schema_version: int = 1
    artifact_type: str                     # "code" / "image" / "audio" / ...
    size_bytes: int
    digest: str
    location: str | None = None            # path / URI

# env_observation
@dataclass
class EnvObservationPayload:
    schema_version: int = 1
    observation_type: str                  # "external" / "timer" / "notification" / "contradiction" / "anomaly"
    source: str                            # subsystem name
    detail: dict[str, Any]
```

---

## 8. TurnSnapshot 設計與實作複雜度標記

> 本節服務 Phase 2 估時。實作落後時 `TurnSnapshot` 可能成為瓶頸 — 預先把每欄位的 aggregation 複雜度標出。

### 8.1 TurnSnapshot dataclass

```python
@dataclass
class TurnSnapshot:
    turn_id: str
    branch_id: str
    correlation_ids: set[str]              # 這 turn 涵蓋的 business actions
    prompt_stack_snapshot: PromptStackSnapshot
    tool_calls: list[ToolCallSummary]      # name, args_digest, phase chain, result_digest, rolled_back
    memory_ops: list[MemoryOpSummary]
    permission_decisions: list[PermissionDecision]
    judge_verdict: JudgeVerdictPayload | None
    artifacts: list[ArtifactRef]
    outcome: str                           # turn_end.outcome
    duration_ms: int
```

### 8.2 各欄位 aggregation 複雜度

| 欄位 | 來源事件 | 複雜度 | 備註 |
|---|---|---|---|
| `turn_id` | `turn_start.turn_id` | trivial | |
| `branch_id` | `turn_start.branch_id` | trivial | |
| `prompt_stack_snapshot` | `turn_start.payload` | **trivial** — 直接讀 turn_start | |
| `correlation_ids` | 整輪事件的 distinct `correlation_id` | trivial — `SELECT DISTINCT correlation_id WHERE turn_id=?` | |
| `tool_calls` | `tool_lifecycle` BEGIN/STATE_CHANGE/END/ROLLBACK | **medium** — 多事件 group by tool_call_id 鏈，組 phase 序列 + result_digest | 含 rolled_back 判斷 |
| `memory_ops` | `memory_op`（含 batch_read 展開） | **medium** — batch_read v1 保留 batch 形式，不展開 | |
| `permission_decisions` | `permission_decision` | trivial | |
| `judge_verdict` | `judge_verdict`（一輪通常 0-1 個） | trivial | |
| `artifacts` | `artifact_emit` | trivial | |
| `outcome` | `turn_end.payload.outcome` | trivial | |
| `duration_ms` | `turn_end.timestamp - turn_start.timestamp` | trivial | |

**結論**：兩個 medium 欄位（`tool_calls`、`memory_ops`）需要明確 reconstruction logic，其餘都是 直接 select 或 group。

### 8.3 ToolCallSummary 重建邏輯

```python
@dataclass
class ToolCallSummary:
    tool_call_id: str
    tool_name: str
    args_digest: str
    state_history: list[str]               # ["BEGIN", "STATE_CHANGE", ..., "END"] 或 [..., "ROLLBACK"]
    result_digest: str | None
    rolled_back: bool
    error: str | None

def reconstruct_tool_calls(events: list[LedgerEvent]) -> list[ToolCallSummary]:
    by_call_id = defaultdict(list)
    for e in events:
        if e.event_type != "tool_lifecycle":
            continue
        by_call_id[e.payload["tool_call_id"]].append(e)
    
    summaries = []
    for call_id, phases in by_call_id.items():
        phases.sort(key=lambda p: p.timestamp)
        begin = phases[0]
        end_or_rollback = phases[-1]
        summaries.append(ToolCallSummary(
            tool_call_id=call_id,
            tool_name=begin.payload["tool_name"],
            args_digest=begin.payload["args_digest"],
            state_history=[p.payload["phase"] for p in phases],
            result_digest=end_or_rollback.payload.get("result_digest"),
            rolled_back=end_or_rollback.payload.get("rolled_back", False),
            error=end_or_rollback.payload.get("error"),
        ))
    return summaries
```

---

## 9. Schema Evolution（F）

### 9.1 Version field 放 payload

每事件 payload 帶 `schema_version`（integer，不用語意化版號）。每事件 ~10 bytes 成本可忽略。

不做 meta 表。混版本期間單一 meta version 不夠用。

### 9.2 Forward-compat（舊 reader 遇到新 ledger）

| 情境 | 處理 |
|---|---|
| 新 event_type 舊 reader 不認 | skip + warning log |
| 既有 event_type 新增 optional payload field | 忽略（json_extract 找不到回 NULL） |
| 既有 event_type 新增 required field | breaking change → §9.4 |
| 既有 field 語意變了 | breaking change → §9.4 |

### 9.3 Backward-compat（新 reader 遇到舊 ledger）

Reader 統一帶 version branching：

```python
def parse_tool_lifecycle(event):
    version = event.payload.get("schema_version", 1)
    if version >= 2:
        return _parse_v2(event)
    else:
        return _parse_v1(event)
```

### 9.4 Breaking change 流程（solo operator 簡化版）

```
1. 明確列出 breaking 點（PR description）
2. 在程式碼裡 bump schema_version 常數
3. Writer 端：發出 schema_version=N+1 的事件
4. Reader 端：加 version branching 同時支援 N 跟 N+1
5. doc/53 加一條 "Schema versions" 表
6. 不做 deprecation 期 — readers 永遠多版本相容
```

### 9.5 Naming convention

| 類型 | 規則 | 範例 |
|---|---|---|
| event_type | `snake_case` | `tool_lifecycle` / `memory_op` |
| 子類型 / phase | `ALL_CAPS` | `BEGIN` / `STATE_CHANGE` |
| payload field | `snake_case` | `tool_name` / `predecessor_memory_id` |
| schema_version | `integer` | `1` / `2` / ...，不用語意化版號 |
| **時間欄位** | **統一 UTC Unix epoch (REAL)，名稱永遠用 `timestamp`** | 不要混用 `created_at` / `event_time` / `ts` 等別名 |

純文件規定，不做 lint 強制。

### 9.6 Schema Versions

| event_type | v1（PR #330 / Phase 2 Step 2） | v2 (TBD) |
|---|---|---|
| `turn_start` | `prompt_stack_hash`(sha256:) + `prompt_stack_components`{persona, tool_catalog_size} + `full_text`=None | — |
| `turn_end` | `outcome` ∈ {clean,abandoned,error}, `duration_ms`, `token_usage`={} placeholder | — |
| `thought` | (deferred — schema 已定義於 §7.2) | — |
| `model_event` | (deferred — schema 已定義於 §7.2) | — |
| `tool_lifecycle` | phase ∈ {BEGIN, END}（見 §3.1 簡化說明）；END 帶完整 state_history | — |
| `permission_decision` | decision ∈ {grant, deny}；scope 子類型未 emit | — |
| `memory_op` | operation ∈ {read, write}；compact / batch_read 暫無 emitter | — |
| `task_mutation` | operation ∈ {write}（見 §3.1 簡化說明）；task_state 帶完整 status_summary snapshot | — |
| `judge_verdict` | (deferred) | — |
| `artifact_emit` | (deferred) | — |
| `env_observation` | observation_type ∈ {timer, external, anomaly}；notification/contradiction 暫無 emitter | — |

所有 v1 payload 帶 `schema_version: 1`。Phase 2 實作期間若 schema 演進，於本表追加 v2 行並標 breaking 點（§9.4 流程）。

---

## 10. v1 Non-goals（G）

### 10.1 功能層級非目標

| 項目 | Phase 2 reference |
|---|---|
| **Fork primitive API** (`ledger.replay.fork()`) | v1 不做。`branch_id` 已預留為 v2 入口；v2 對著真實 operator replay 需求設計 fork API 形狀 |
| **Turn Graph Branching 實作** | v1 預留 `branch_id` 欄位，實作 v2+ |
| **Operator Replay 工具** | non-deterministic LLM replay 本身是研究題 |
| **Long-thought structured segmentation** | v1 單一 blob。autonomy deep reasoning「嘗試→轉向→成功」分段 replay 需求不明確；v2 有足夠 corpus 再 design。`compaction_chain` (§5.7) 可覆蓋同義「演化軌跡」需求 |
| **Schema evolution 自動化工具** | v1 手動 version branching；v2 加 generated type stubs / Literal 升級工具 |
| **Backfill old session_log 進 ledger** | leave alone（§11.3）。未來真需要寫 explicit retro converter，明標 low trust tier |
| **跨 session UI 視圖** | schema 支援，v1 不主動暴露 UI |
| **Type stubs（Literal / StrEnum）** | v1 plain strings + doc 列舉；v2 schema evolution tool 帶 |
| **Aggregate ORM 完整體** | v1 fluent 只覆蓋簡單 aggregate，複雜走 raw SQL |
| **Rotation policy** | v1 不刪，留 maintenance hook |

### 10.2 部署層級非目標

| 項目 | 緣由 |
|---|---|
| Ledger.db 複製 / HA | single-file SQLite，本機應用，無 HA 需求 |
| Ledger.db 加密 | 依賴 filesystem-level encryption（FileVault / LUKS） |
| Remote access / network protocol | 本機 only，server mode 是獨立議題 |
| Multi-tenant ledger | 單一 Loom Agent，不跨 user 共享 |
| Real-time external observer streaming | subscriber 是內部 process 用 |
| Distributed ledger / multi-process write | 單 process 寫入，sqlite WAL 即可 |

### 10.3 觀察 / 監控層級非目標

| 項目 | 緣由 |
|---|---|
| Lag indicator 可見化（統一 UI） | ledger 提供 is_live / lag_events，platform 自己 render |
| Ledger.db size dashboard | v1 不做監控 |
| Per-event 結構化 audit log（額外於 ledger） | ledger 本身就是 audit log |

### 10.4 進入 v1 但僅以最小形式存在

| 項目 | v1 形式 |
|---|---|
| `branch_id` 欄位 | 永遠 `"main"`，schema 內 comment 標註 v2 fork primitive 會用到 |
| Maintenance hook | 介面存在（`LedgerStore.maintenance()`），v1 只做 `PRAGMA optimize` + `VACUUM` |
| Cross-session pull | API 接受 `session_id=None` 但無 UI 暴露 |
| **External blob storage** | `.loom/ledger_blobs/{turn_id}/{event_id}.txt`（本機檔案系統，未來可置換 S3/object store）。**§3.3：thought_full_text > `THOUGHT_EXTERNAL_THRESHOLD` (50KB / 50,000 bytes) 時寫入 blob + digest，blob path 顯式存 payload，digest 用於 replay 驗證** |

---

## 11. Implementation Migration（E）

### 11.1 Emit 位置 — Layered

每層只 emit 它天然產生的事件，不跨層偷看：

| 出產層 | 負責 emit 的事件 |
|---|---|
| **Middleware pipeline** | `tool_lifecycle` (BEGIN/STATE_CHANGE/END/ROLLBACK)、`permission_decision` |
| **Session (`stream_turn`)** | `turn_start`、`turn_end`、`thought`、`model_event`、`judge_verdict`、`artifact_emit` |
| **MemoryFacade** | `memory_op` (write / batch_read / compact) |
| **TaskScheduler** | `task_mutation` |
| **AutonomyDaemon** | `env_observation` |

每個 subsystem 多一個 ledger import，但 ledger 是純 sink、無反向依賴 → 健康的單向耦合。

### 11.2 Phase 2 六步驟線性

```
Step 1. LedgerStore (schema + emit API + 6+1 indexes)
        └ 純儲存層，可獨立測試

Step 2. emit calls 全鋪（按 §11.1 layered 分布）
        └ dev branch 內 dual-emit（envelope + ledger 同時寫）
        └ 對拍「envelope 看到的 lifecycle 序列 vs ledger 看到的」應一致

Step 3. Replay primitive Layer 1+2
        └ raw events / TurnSnapshot
        └ 同時驗證 §8.2 各欄位 aggregation 實作完整度

Step 4. Push subscriber API + Pull fluent API
        └ async iter + bounded buffer + is_live + replay_from
        └ fluent + raw SQL escape

Step 5. ExecutionEnvelope / SessionLog 改為 ledger projection (cutover)
        └ 不刪舊 code，內部改成從 ledger 讀
        └ 驗證所有 envelope consumer (TUI / Discord) 行為一致

Step 6. 測試全綠 → merge → 切換 Loom Agent 啟新版開新 session
```

性質：Step 1-4 不影響運行系統；Step 5-6 是切版時刻。

### 11.2.1 Phase 2 實作狀態

| Event type | Step 2 狀態 | 備註 |
|---|---|---|
| `turn_start` | ✅ emit | PromptStack snapshot 含 persona + tool_catalog_size；`memory_layers` / `context_token_count` 未追蹤所以省略（不放 placeholder 誤導 reader） |
| `turn_end` | ✅ emit | `outcome` 從 `sys.exc_info()` 推導；`token_usage` 由 `_emit_ledger_model_event` 累計（#334） |
| `tool_lifecycle` BEGIN/END | ✅ emit | STATE_CHANGE / ROLLBACK 簡化見 §3.1 註記 |
| `permission_decision` | ✅ emit | grant / deny；scope 子類型未 emit（需 reason 字串解析，違反 `feedback_avoid_regex_on_llm_output`） |
| `memory_op` read / write | ✅ emit | search / get_fact / query_relations / memorize / relate 五路徑 |
| `memory_op` compact | ⚠️ schema ready, no emitter | Loom v0.3 無 compaction caller；resolve_memory_id store-side helper 已就緒 |
| `memory_op` batch_read | ⚠️ schema ready, no emitter | MemoryPulse 落地時補 |
| `task_mutation` | ✅ emit | operation 簡化見 §3.1 註記 |
| `env_observation` timer / external / anomaly | ✅ emit | TriggerEvaluator 三類 trigger |
| `env_observation` notification / contradiction | ⚠️ schema ready, no emitter | MemoryPulse / ContradictionDetector 自己的 commit |
| `thought` | ✅ emit（#334） | §3.3 signal accumulator：judge fail/uncertain、outcome abandoned/error、artifact >10KB 任一觸發 commit；inline ≤50KB / blob > 50KB |
| `model_event` | ✅ emit（#334） | stream_turn 主 loop、`run_judge`（sync + async）、subagent 三條 router 路徑都 emit；`_turn_token_usage` 餵 turn_end |
| `judge_verdict` | ✅ emit（#334） | `_maybe_run_judge` / `_run_judge_async` 兩條路徑；判定 pass/fail/uncertain → PASS/FAIL/CONCERN，`error` 設值 → ERROR |
| `artifact_emit` | ✅ emit（#334） | LifecycleMiddleware END 後檢查 artifact extractor registry（如 `write_file`, `image_generate`）；rolled_back 或 failed 不 emit |

`三類 exception 自開新 correlation_id`（§4.2）：
- `env_observation` ✅ 實作（PR #330 commit 4）
- `memory_op.compact` ⚠️ deferred（無 emitter）
- `turn_end.outcome=error` ⚠️ schema 就緒；目前 turn_end 用 stream_turn 主 correlation 不另開新 corr — 待 follow-up issue 評估是否要切

Deferred 事件類型不阻擋 Step 5 cutover：它們是 additive event types，不是替換既有 observability 信號。Follow-up issue 在 Step 2 merge 後另開。**#334（feat/ledger-deferred-events）落地後，原本 ❌ 的四個 event types 全部轉 ✅；後續仍 deferred 的是 `memory_op` compact / batch_read 與 `env_observation` notification / contradiction，等對應 caller 出現再 emit。**

**Step 3-5 進度補記**（PR #331 / #332 / Step 5 PR）：

| 工作 | 狀態 | 備註 |
|---|---|---|
| Step 3 — Replay primitive (events_for_* + TurnSnapshot) | ✅ 完成（#331） | `LedgerStore.replay` lazy property，§8.2 trivial+medium 欄位 reconstruct 全綠 |
| Step 4 — Push subscriber + Pull fluent + raw SQL | ✅ 完成（#332） | `subscribe(...)` async ctx、`events.where().since().all()` 等、`execute_sql`；`is_live` monotonic 語意（一旦 drop 永久 False，`re-subscribe` 重置） |
| Step 5 — ExecutionEnvelope 投影切換 | ✅ 完成（#333 + #337） | `LedgerEnvelopeProjector` + `_build_envelope_view` async 委派；#337 移除 `_live_record_for` / `envelope.records` transitional bridge — projector 純從 ledger 讀取，`ExecutionEnvelope` 退成 thin marker。`[ledger].enabled=false` 改 graceful empty-view fallback：EnvelopeStarted/Completed 仍 fire（shape 一致），但 nodes 為空 — tool detail 改由 `ToolBegin` / `ToolEnd` stream 提供，不保證 full envelope UI parity。把 ledger disable 視為 safety/diagnostic opt-out，不是支援 tier |
| Step 5 — SessionLog 投影 | ✗ wontfix（#335） | 重新審視後決定不做純投影。理由見下方 §11.2.2。#335 改去處理可獨立分離的痛點：secret redaction 從寫入時搬到讀取時 |
| Step 5 — Memory compaction subscribe `turn_end` 觸發 | ✅ 完成（#336） | `LoomSession._compaction_subscriber_loop` 訂閱 `turn_end` events for own session_id；`_compaction_lock` 序列化並發呼叫；compaction 不再阻塞 turn return，`CompressDone` 從 stream_turn 改為 buffer 在 `_pending_compactions` 由下一個 stream_turn 開頭 yield。Race：新 turn 與 in-flight compaction 並發時，compaction 已在開始時讀過 episodic snapshot，新寫入留待下次觸發；MemoryGovernor 自身的 admission gate 處理並發 semantic upsert |

### 11.2.2 SessionLog 與 ledger 的邊界（#335 決策）

#335 原本要把 SessionLog 改寫為 ledger pure projection。survey 完現況後改變決策——**不做投影，劃清邊界**。

**為什麼不做：**

ledger 設計初衷是「事件流動的東西」（§2 / §3.2），存的是 turn / tool / memory / judge / artifact 等 transition 訊號。SessionLog 存的是 OpenAI-canonical raw text（user input、assistant raw_message 含 tool_use blocks、tool result 全文），是**內容性質**，不是事件性質。要讓 ledger 能完整投影 SessionLog，需要：

- 在 `turn_start.payload` 塞 `user_input` 全文 — 違反 §3.4 PromptStack snapshot 的設計（只放 hash，不放原文）
- 開新 event type `assistant_message`、或讓 `thought` 一律 commit（丟掉 §3.3 capture signal）— 失去 thought 的篩選價值
- 擴 `tool_lifecycle.END.result_summary` 從 200 字到全文 — 把 high-frequency 事件變成大 blob 容器

每一條都把 ledger 拖向「儲存層」而非「事件層」。Loom 該記錄的內容服務 agent 為主，user 訊息原文沒必要在 ledger 再存一份。

**SessionLog 與 ledger 的職責切分（v0.3 起鎖定）：**

| 用途 | 來源 |
|---|---|
| Resume / TUI 重播 / Discord 重連的 message history | SessionLog（`session_log` + `sessions` 兩張表） |
| 事件流 / replay primitive / TurnSnapshot / 投影出 ExecutionEnvelopeView | AgentLedger |
| 跨子系統的觀測（judge / memory / task / artifact） | AgentLedger |
| OpenAI-canonical raw text 持久化 | SessionLog |
| Secret redaction（API keys、Bearer tokens） | SessionLog 讀取時做（#335）；ledger 端不重複 |

**#335 實際做的事**：把 SessionLog 的 secret redaction 從寫入時搬到讀取時。raw text 在磁碟上保留原貌、未來 regex 改進仍能即時對舊資料生效；同時實作了 `_redact_in_place(node)`，在 parse JSON 後對 leaf string 做 redaction，避開 regex 吞掉 escape quote 破壞 JSON 結構的舊問題（write-time 路徑剛好沒踩到、但 read-time naive 套用會炸）。

**SECURITY — threat model 變更需明確承認**：這不是 Issue #92 的等價實現。#92 處理的是 **at-rest** 洩漏（DB 備份、lost laptop、file permission 設錯），write-time redaction 確保磁碟上看不到 secret。#335 改成 read-time 後：

| 防護面 | #92 write-time | #335 read-time |
|---|---|---|
| `load_messages()` 回傳值 | ✅ redacted | ✅ redacted |
| TUI / Discord / resume 重播 | ✅ redacted | ✅ redacted |
| `~/.loom/memory.db` 檔案被竊取 | ✅ redacted | ❌ **plaintext** |
| 跨備份系統洩漏（iCloud / Dropbox sync） | ✅ redacted | ❌ **plaintext** |

換取的是「regex 可逆、未來改進仍能套用到舊資料」。這個取捨在「私人開發環境、單機使用」假設下成立；若未來 Loom 進入多用戶 / 共用主機 / cloud-synced 場景，需要重新評估。

at-rest 防護分三階段在 **#342** 收：

| 階段 | 狀態 | 內容 |
|---|---|---|
| Baseline — chmod 0700/0600 | ✅ 已 ship | `~/.loom/` 目錄收 0700、`memory.db` / `ledger.db`（含 WAL/SHM）收 0600；helper 在 `loom/core/infra/file_permissions.py`，`SQLiteStore.initialize` 與 `LedgerStore.open` 接線。Block 同主機其他 user，但**擋不住**檔案被搬走（備份外流、lost laptop） |
| Retention pruning（opt-in） | ⚠️ deferred | 由 user 在 `loom.toml` 設定 `[session_log] retention_days=N`，超期 row 由背景 prune。處理「老備份外流」場景 |
| SQLCipher | ⚠️ deferred | 真 at-rest encryption，需要 key management / 額外 dependency；等實際需求訊號 |

在後兩階段落地前，使用 Loom 仍應視同把 secret 寫進 `~/.loom/memory.db` plaintext —— baseline 只擋同機其他 user，不擋備份/實體竊取。

未來真需要把 raw text 也納入事件流時（例如 Quest D 想做 corpus 訓練），開新 event types 而不是改 SessionLog 投影方向；那時 ledger 會明確扮演「事件流 + opt-in raw text 副本」雙角色，而不是把 SessionLog 拆掉。

### 11.3 舊資料 — Leave alone

舊 session_log 留 `memory.db`、舊 `ExecutionEnvelope` artifact 留原處；新 session 從 `ledger.db` 開始。

**紀律明文**：`memory.db` 內 session_log 表保留為歷史只讀區，不主動清除。未來真需要拉舊資料當 corpus（如 Quest D），那時寫 explicit retro converter，明標 low trust tier。

舊資料變相成為 Loom 的成長軌跡 / 地層 — 之後可能有研究價值。

---

## 12. 文件版本與相關讀物

- **本文件版本**：v1.3（2026-05-09，Phase 2 Step 5 cutover 完成 — envelope 投影切換；SessionLog / compaction-subscribe 標 deferred follow-up）
  - v1.2（2026-05-08，Phase 2 Step 4 實作回填 — PR #332 `is_live` monotonic 語意）
  - v1.1（2026-05-08，Phase 2 Step 2 實作回填 — PR #330 review feedback）
  - v1.0（2026-05-08，Phase 1 鎖定版）
- **共識來源**：#316 Round 1-6 comments
- **關係文件**：
  - `doc/52-主線與支線.md` §1.4（Quest B 定位）、§3.3（Turn Graph Branching 預留）、§5（開發順序）
  - `doc/50-未來改善路線圖.md` §AgentLedger 統一事件流（顧問版啟發來源）
  - `doc/51-Agent-能力評級系統.md`（#314 Capability Sheet 自動投影下游需求）
- **memory references**：
  - `feedback_vibe_coding_code_style`（agent-friendly 設計判準）
  - `project_memory_system_v2`（Memory v2 emit 下游）
  - `feedback_avoid_regex_on_llm_output`（payload 結構化的紀律）

---

## 13. 簽結

A-G 共七題設計問題在 #316 Round 1-6 達成共識，全部整合於本文件。Phase 1 完成條件已滿足：

- [x] 本設計文件
- [x] 七題決策回填
- [x] schema dataclass 純宣告（§7）
- [x] TurnSnapshot 各欄位 aggregation 複雜度標記（§8）
- [x] migration plan（§11）

下一步：開 Phase 2 實作 issue，按 §11.2 六步驟線性推進。
