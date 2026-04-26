# AbortController — 標準取消訊號

> Issue #111 — 統一路徑取消訊號，memory-leak safety。

---

## 定位

`AbortController` 是 Loom 生命週期的標準取消訊號（cancellation signal）。它的定位是整個非同步管線的統一中斷機制，適用於：
- 工具執行逾時（`run_bash` 跑了太久）
- 使用者中斷（按 Escape / Ctrl+C）
- Autonomy Daemon 的排程中止
- Session 結束前的資源清理

---

## 核心設計

### 為何用 asyncio.Event 而非 signal？

Loom 是 **async-only** 架構。Python stdlib 的 `signal.AbortSignal` 是同步的，設計給 sync 程式用。`asyncio.Event` 是最接近的 async 等價物，且支援多 task 同時等待同一訊號。

```python
class AbortController:
    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    @property
    def signal(self) -> asyncio.Event:
        return self._cancelled

    def abort(self) -> None:
        self._cancelled.set()

    def reset(self) -> None:
        self._cancelled.clear()

    @property
    def aborted(self) -> bool:
        return self._cancelled.is_set()
```

### 重要設計細節：Memory-Leak Safety

OpenClaw 發現（[#7174](https://github.com/openclaw/openclaw/issues/7174)）：將 controller 包在 closure 內當作 callback，會造成長 process 的記憶體洩漏。

```
# BAD — closure 捕捉周圍作用域，累積在 GC
timer = setTimeout(() => { controller.abort(); }, 1000)

# GOOD — bound function，無 closure scope
timer = setTimeout(controller._abort_bound(), 1000)
```

**Loom 永遠使用 `_abort_bound()` 或 `bind()`**，從不在 callback 中使用 `lambda: self.abort()`。

---

## API 一覽

| 方法/屬性 | 回傳 | 說明 |
|-----------|------|------|
| `signal` | `asyncio.Event` | 取消訊號，等待者以此作為 await 目標 |
| `abort()` | `None` | 發送取消給所有等待中的 task |
| `reset()` | `None` | 清除訊號，讓 controller 可復用於新 turn |
| `aborted` | `bool` | 當前是否已被 abort |
| `_abort_bound()` | `Callable[[], None]` | bound method，供 callback 使用 |
| `bind()` | `Callable[[], None]` | 等同 `_abort_bound()`，工廠方法 |

---

## 使用範例

### 工具逾時

```python
async def run_bash_with_timeout(call: ToolCall, abort_signal: asyncio.Event, timeout: float):
    exec_task = asyncio.create_task(_real_bash(call))
    abort_task = asyncio.create_task(wait_aborted(abort_signal))

    done, pending = await asyncio.wait(
        {exec_task, abort_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=timeout,
    )

    if exec_task in done:
        return await exec_task
    else:
        exec_task.cancel()
        return ToolResult(success=False, failure_type="timeout")
```

### LifecycleGateMiddleware 中的 abort racing

```python
exec_task = asyncio.create_task(next(call))   # 工具 handler
abort_task = asyncio.create_task(wait_aborted(call.abort_signal))

done, pending = await asyncio.wait(
    {exec_task, abort_task},
    return_when=asyncio.FIRST_COMPLETED,
)

if exec_task in done:
    result = await exec_task          # 正常完成
else:
    exec_task.cancel()                # 中止執行
    result = ToolResult(success=False, failure_type="aborted")
```

### Session stop 時的 cleanup

```python
# session.py stop()
await self._jobstore.cancel_all(reason="session_ended")
self._scratchpad.clear()
self._abort.abort()          # 通知所有還在等的 task 收攤
await self._db.close()
```

### AutonomyDaemon 的 runtime control

```python
async def start(self, poll_interval: float = 60.0) -> None:
    run_task = asyncio.ensure_future(self._evaluator.run_forever(...))
    abort_task = asyncio.ensure_future(wait_aborted(self._abort.signal))
    done, pending = await asyncio.wait(
        [run_task, abort_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

def stop(self) -> None:
    self._abort.abort()
```

---

## 輔助函數

### `wait_aborted(signal)`

```python
async def wait_aborted(signal: asyncio.Event) -> None:
    if signal.is_set():
        return
    await signal.wait()
```

等待直到訊號被設定。若訊號已設定則立即返回。
`asyncio.CancelledError` 從外部 task cancellation **不會被捕捉**，正常向上傳播。

### `abort_bound(controller)`

Standalone factory，效果等同 `controller.bind()`。用於需要明確工廠的 call site：

```python
later = loop.call_later(60_000, abort_bound(self._abort))
```

---

## 與 Lifecycle 的整合

```
AbortController.abort()
  ↓ signal.set()
  ↓
LifecycleGateMiddleware.executing
  ↓ asyncio.wait() racing
  ↓ exec_task 被 cancel
  ↓ result = ToolResult(failure_type="aborted")
  ↓
LifecycleMiddleware.end
  ↓ transition → ABORTED → MEMORIALIZED
  ↓
session._abort.reset()  ← 為下一個 turn 復用 controller
```

Session 結束時的 cleanup 順序：
1. `jobstore.cancel_all(reason="session_ended")`
2. `scratchpad.clear()`
3. `abort_controller.abort()` — 通知還在等的 task 收攤
4. `db.close()`

---

## 設計原則

1. **永不直接用 lambda**：所有需要傳入 callback 的地方，一律用 `_abort_bound()` 或 `bind()`
2. **每個 turn reset**：Session 每個 turn 開始前，controller 應該 `reset()`，讓同一個 controller 能在新 turn 被复用了
3. **不依賴外部狀態**：AbortController 本身是 stateless 的，狀態存在 `_cancelled` Event 裡
4. **可重入**：同一個 controller 可以多次 `abort()` / `reset()` 交替，不會出問題

---

*文件草稿 | 2026-04-26 03:10 Asia/Taipei*