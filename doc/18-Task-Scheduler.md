# Task Scheduler

Task Scheduler 是 Task Engine 的執行層。它接收 `ExecutionPlan`（Kahn's sort 產出的分層節點列表），按 level 調度執行。

---

## 與 Task Engine 的關係

```
TaskGraph.compile()
    │
    ├── TaskNode A ─┐
    ├── TaskNode B ─┼─→ ExecutionPlan.levels = [[A], [B, C], [D]]
    └── TaskNode C ─┘                         │
                                              ▼
                                  TaskScheduler.run(plan)
                                              │
                                              ├── Level 0: asyncio.gather([A])
                                              ├── Level 1: asyncio.gather([B, C])
                                              └── Level 2: asyncio.gather([D])
```

---

## Scheduler 核心（loom/core/tasks/scheduler.py）

### TaskScheduler

```python
ExecutorFn = Callable[[TaskNode], Awaitable[Any]]

class TaskScheduler:
    def __init__(
        self,
        executor: ExecutorFn,           # 外部傳入的執行函數
        stop_on_failure: bool = False,
    ) -> None:
        self._executor = executor
        self._stop_on_failure = stop_on_failure
```

### 執行方法

```python
async def run(self, plan: ExecutionPlan) -> list[TaskNode]:
    """
    執行所有 levels。
    Returns plan.all_nodes（帶最終 status）。
    """
    failed_any = False

    for level in plan.levels:
        if failed_any and self._stop_on_failure:
            for node in level:
                node.skip()
            continue

        results = await asyncio.gather(
            *[self._run_node(node) for node in level],
            return_exceptions=True,
        )

        for node, result in zip(level, results):
            if isinstance(result, Exception):
                node.fail(str(result))
                failed_any = True

    return plan.all_nodes

async def _run_node(self, node: TaskNode) -> Any:
    node.status = TaskStatus.IN_PROGRESS
    try:
        result = await self._executor(node)
        node.complete(result)
        return result
    except Exception as exc:
        node.fail(str(exc))
        raise
```

---

## stop_on_failure 選項

| 設定 | 行為 |
|------|------|
| `stop_on_failure = False`（預設）| 一個節點失敗不影響其他 level，繼續執行 |
| `stop_on_failure = True` | 某節點失敗後，剩餘 levels 的節點全部標記 `SKIPPED` |

> `TaskScheduler` 不自己拋异常——失敗節點的 `error` 寫入 `TaskNode.error`，讓 caller 自行決定後續。

---

## asyncio.gather 的並行單位

`asyncio.gather` 作用於**同一 level 內的所有節點**：

```
ExecutionPlan.levels[0] = [A]         → gather([_run_node(A)])
ExecutionPlan.levels[1] = [B, C]      → gather([_run_node(B), _run_node(C)])  ← B/C 真正並行
ExecutionPlan.levels[2] = [D]         → gather([_run_node(D)])
```

`parallel_groups` 屬性可快速取得所有真正可並行的 groups：

```python
for group in plan.parallel_groups:
    print(f"並行: {[n.content[:20] for n in group]}")
```

---

## 與 Autonomy Engine 的整合

`AutonomyDaemon` 並不直接持有 `TaskScheduler` 實例。整合方式是：

1. `ActionPlanner.handle()` 接收 fired trigger，組裝 `PlannedAction`
2. `AutonomyDaemon._run_agent(plan)` 呼叫 `self._session.stream_turn(plan.prompt, ...)`
3. Session 內部的 `_dispatch_parallel()` 接收 `TaskGraph`，內部調用 `TaskScheduler` 執行

> **並非** `AutonomyDaemon` 直接持有 `TaskScheduler`。整合點在 Session 層，而非 Daemon 層。

---

## loom.toml 配置

TaskScheduler 的行為由呼叫方（Session 或測試）控制，`loom.toml` 中無獨立 `[task_scheduler]` 區段。

Session 層級的並行相關設定見 `37-loom-toml-參考.md` 中的 `[session]` 區段。

---

## 總結

| 功能 | 說明 |
|------|------|
| asyncio.gather | 同 level 內的 TaskNode 真正並行 |
| stop_on_failure | 失敗後可選跳過剩餘 levels |
| 回傳 all_nodes | caller 自行遍歷結果、讀取 `node.result` / `node.error` |
| 整合點在 Session | Autonomy 不直接持有 Scheduler，透過 Session 間接使用 |
