# Task Scheduler — Async Jobs（JobStore + Scratchpad）

> 架構演進（#154 / v0.3.2.1）：舊版的 `TaskScheduler` / `ExecutionPlan` / Kahn's topological sort 已隨 #153 一併移除。本檔描述現行的**工具層並行機制**：agent 透過 `async_mode=True` 把 IO 下放到 JobStore，結果寫進 Scratchpad，harness 在 turn 邊界以事件注入通報完成/進行中的 job。推理層的平坦清單見 [17-Task-Engine.md](17-Task-Engine.md)。

---

## 為什麼把並行搬到工具層？

v0.3.2 的 DAG scheduler 把「同層節點並行」包在推理層：`asyncio.gather(level)` 並行執行多個 `TaskNode`，每個節點內部自己跑 LLM 推理。但實戰上：

1. **推理並行會撕裂上下文**——兩個節點同時推理時看不到彼此的中間結論，反而比串行更糟
2. **真正想並行的是 IO**——抓 4 個 URL、跑 3 個 shell command，這些本來就不需要推理介入
3. **推理層串行 + IO 層並行**才是符合直覺的劃分——agent 自己決策順序，IO 交給工具層背景跑

所以現在的設計把並行「向下推」到 `fetch_url` / `run_bash` 的 `async_mode=True` 路徑，由 `JobStore` 管理生命週期，`Scratchpad` 承接輸出，agent 用 `jobs_await` / `jobs_status` 收斂結果。

---

## 核心組件

### JobStore（`loom/core/jobs/store.py`）

Session 範圍的背景任務註冊表。

```python
class JobState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class Job:
    id: str                    # job_<8hex>
    fn_name: str               # 送出此 job 的工具名（fetch_url / run_bash 等）
    args: dict                 # submit 時的原始參數
    state: JobState
    result_ref: str | None     # Scratchpad key
    result_summary: str | None # 簡短摘要（size / first line 等）
    error: str | None
    cancel_reason: str | None
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def is_terminal(self) -> bool: ...   # DONE / FAILED / CANCELLED
    @property
    def elapsed_seconds(self) -> float | None: ...
```

```python
class JobStore:
    def submit(self, fn_name: str, args: dict,
               coro_factory: Callable[[], Awaitable[tuple]]) -> str: ...
    # coro_factory 必須回傳 (result_ref, summary, error) 三元組
    # error 非 None 即視為 soft failure，狀態轉 FAILED

    def get(self, job_id: str) -> Job | None: ...
    def list_all(self) -> list[Job]: ...
    def list_active(self) -> list[Job]: ...   # PENDING + RUNNING

    def reap_since_last(self) -> tuple[list[Job], list[Job]]:
        """回傳 (新完成, 仍在跑)。同一 job 只會出現在一次的『新完成』結果中。"""

    def cancel(self, job_id: str, reason: str) -> None:
        """reason 不可為空——trace 會留在 Job.cancel_reason"""

    async def cancel_all(self, reason: str) -> None: ...
    async def await_jobs(self, ids: list[str], timeout: float
        ) -> tuple[list[Job], list[Job]]:
        """不會 raise timeout——呼叫方檢查 running list 自己決定要不要 cancel。"""
```

**設計重點**：
- **`create_task`，不 threadpool**——所有 job 是 coroutine，統一在 event loop 上跑
- **`reap_since_last()` 冪等**——內部 `_reaped_ids: set[str]` 保證同一 job 只向 harness 報告一次
- **Cancel 強制留 reason**——CANCELLED 節點仍在 `list_all()`，agent 翻 trace 就看得到為什麼停
- **Terminal 不可被 cancel**——已 DONE/FAILED 的 `cancel()` 是 silent no-op，不覆蓋結果

### Scratchpad（`loom/core/jobs/scratchpad.py`）

Session 範圍的 `ref → bytes` 儲存。job 的輸出寫這裡，agent 用 `scratchpad_read` 取用。

```python
class Scratchpad:
    def write(self, ref: str, content: str | bytes) -> str:
        """回傳 scratchpad://<ref>。ref 必須不含 /、不以 . 起頭、非空。"""
    def read(self, ref: str,
             section: str | None = None,
             max_bytes: int | None = None) -> str: ...
    def size(self, ref: str) -> int: ...
    def list_refs(self) -> list[str]: ...   # sorted
    def clear(self) -> None: ...            # session stop 時呼叫
    def __contains__(self, ref: str) -> bool: ...
```

`section` 支援 `"head"` / `"tail"` / `"N-M"` / 關鍵字（grep 語義）——與 `task_read` 一致，避免 agent 每次都讀完整 blob。

`max_bytes` 在 section filter 之**前**截斷原始 bytes，超過時附加 `[scratchpad_read: output truncated at N bytes]`。預設 200_000 bytes。

**為什麼 Scratchpad 獨立於記憶系統？** — Scratchpad 存的是**過程產物**（抓到的 HTML、build log），只對當前 session 有意義。真正要進長期記憶的應該是 agent 讀完後**自己決定的結論**，透過 `memorize` 寫入 semantic store。session stop 時 Scratchpad 會 `clear()`，任何沒被主動撈出來的內容就丟了。

---

## async_mode 工具協定

`fetch_url` 與 `run_bash` 的 tool schema 都有一個選填的 `async_mode: bool`，預設 `False`：

```
fetch_url(url=..., async_mode=False)   # 傳統 blocking：立即回 body
fetch_url(url=..., async_mode=True)    # 立即回 {"job_id": "job_xxxx"}
run_bash(command=..., async_mode=False)
run_bash(command=..., async_mode=True)
```

`async_mode=True` 路徑：
1. Tool 向 JobStore `submit(fn_name, args, factory)`——`factory` 是一個閉包，包含實際的 httpx / subprocess 呼叫
2. `submit` 回傳 `job_id`；Tool 立刻回 `ToolResult(output='{"job_id": "..."}', metadata={"async": True, "job_id": ...})`
3. 背景 coroutine 跑完後把 body 寫進 Scratchpad，更新 Job 的 `result_ref` / `result_summary`

**Fallback**：若 session 沒接 jobstore/scratchpad（測試、殘缺環境），`async_mode=True` 會**靜默退化**成 sync mode，不會 raise——讓單元測試寫起來比較乾淨。

---

## 查詢與收斂：5 個輔助工具

| Tool | 用途 |
|------|------|
| `jobs_list(state?)` | 列所有 job，選填 `state` 篩 active/done/failed/cancelled |
| `jobs_status(job_id)` | 單個 job 的完整狀態（含 elapsed、args、error） |
| `jobs_await(job_ids, timeout)` | 等到全部 terminal 或 timeout。**不 raise**——回傳 `{timeout_hit, finished, still_running}` 讓 agent 自己決定 cancel 或再等 |
| `jobs_cancel(job_id, reason)` | `reason` 必填——schema 層就擋掉沒帶理由的呼叫 |
| `scratchpad_read(ref?, section?, max_bytes?)` | 省略 `ref` 時列出可用 refs；`section` 與 `task_read` 同義 |

所有這 5 個工具 trust level 都是 `SAFE`——純讀操作，不需要確認。

---

## 事件注入：turn 邊界的 jobs update

Harness 不讓 agent 去輪詢——而是在 `stream_turn()` 的 `end_turn` 分支，先跑 TaskList self-check，再跑 jobs self-check：

```python
# session.py stream_turn() end_turn 分支
if not self._jobs_inject_done:
    jobs_msg = _build_jobs_inject_message(self._jobstore)
    if jobs_msg:
        inject(f"<system-reminder>\n{jobs_msg}\n</system-reminder>")
        self._jobs_inject_done = True
        continue   # loop back into LLM
```

`_build_jobs_inject_message(store)` 的輸出形如：

```
[Jobs update]

Completed since last turn:
- job_a1b2c3d4 (fetch_url) → done, result_ref=scratchpad://fetch_a1b2c3d4, elapsed=1.2s
- job_deadbeef (run_bash)  → failed, error="exit code 1: ..."

Still running:
- job_9f8e7d6c (fetch_url) → running, elapsed=3.4s
```

`reap_since_last()` 的冪等保證這段只會**在 job 新完成的那一個 turn**注入一次；之後不再嘮叨。Still running 每個 turn 都會再報一次，提醒 agent 它還在跑。

`_jobs_inject_done` 旗標在每個 turn 初始化時重置，讓同一 session 的後續 turn 可以再注入。

---

## Session 生命週期掛鉤

```python
# session.py __init__
self._jobstore = JobStore()
self._scratchpad = Scratchpad()
# 必須在 make_run_bash_tool / make_fetch_url_tool 之前建好，工廠才能 capture

# session.py start() — 註冊 5 個工具
make_jobs_list_tool(self._jobstore)
make_jobs_status_tool(self._jobstore)
make_jobs_await_tool(self._jobstore)
make_jobs_cancel_tool(self._jobstore)
make_scratchpad_read_tool(self._scratchpad)

# session.py stop()
await self._jobstore.cancel_all(reason="session_ended")
self._scratchpad.clear()
# 然後才關 DB
```

Session 結束時仍在跑的 job 會被 `cancel_all()` 中斷——對 autonomy 這種長期跑的情境，重要資料應該在 job 完成後立即透過 `memorize` / `write_file` 持久化，而不是依賴 Scratchpad。

---

## Autonomy 預設：`async_mode=False`

Autonomy 不缺時間缺的是可預期性。Skill genome（`skills/async_jobs/SKILL.md`）明確說明：

> **Autonomy defaults to `async_mode=False`.** Autonomy has time; stability and predictability matter more than wall-clock wins.

只有當 agent 能明確受益於 IO 並行（例如一次要爬 10 個 URL），且**有 `jobs_await` 收斂**，才應該開 `async_mode=True`。沒收斂就開 async = 把結果隨便擺在 Scratchpad 不管它 = 資源浪費。

---

## 檔案定位

| 檔案 | 職責 |
|------|------|
| `loom/core/jobs/store.py` | `JobState` / `Job` / `JobStore` |
| `loom/core/jobs/scratchpad.py` | `Scratchpad` |
| `loom/core/session.py` | 生命週期、工具註冊、turn-end 事件注入 |
| `loom/platform/cli/tools.py` | `make_run_bash_tool` / `make_fetch_url_tool` 的 async_mode 分支 + 5 個輔助工具工廠 |
| `skills/async_jobs/SKILL.md` | Agent 可見的 Tier-1 skill genome |

相關文件：
- [17-Task-Engine.md](17-Task-Engine.md) — TaskList（推理層清單）
