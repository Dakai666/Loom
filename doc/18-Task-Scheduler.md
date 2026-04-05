# Task Scheduler

Task Scheduler 是 Task Engine 的調度層。它負責實際執行任務、管理並行、控制超時，並與 Autonomy Engine 整合以支援定期任務。

---

## 與 Task Engine 的關係

```
Task Engine（定義）
    │
    ├── 定義 Task 結構
    ├── 計算 DAG（依賴關係）
    └── 拓撲排序
            │
            ▼
Task Scheduler（執行）
    │
    ├── asyncio.gather 執行
    ├── 並行控制
    ├── 超時管理
    └── 結果收集
```

---

## Scheduler 核心

### 基本結構

```python
# loom/core/task/scheduler.py
class TaskScheduler:
    """Task 調度器"""
    
    def __init__(
        self,
        max_concurrency: int = 5,
        default_timeout: float = 300.0,
    ):
        self.max_concurrency = max_concurrency
        self.default_timeout = default_timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
    
    async def schedule(
        self,
        tasks: list[Task],
        parallel: bool = True,
    ) -> list[Task]:
        """排程並執行任務"""
        
        if parallel:
            return await self._schedule_parallel(tasks)
        else:
            return await self._schedule_sequential(tasks)
```

---

## asyncio.gather 並行執行

### 為什麼用 gather？

`asyncio.gather()` 是 Python 異步並行執行的標準方式：

```python
async def _schedule_parallel(self, tasks: list[Task]) -> list[Task]:
    """使用 gather 並行執行所有無依賴的任務"""
    
    # 1. 拓撲排序
    sorted_ids = topological_sort(tasks)
    id_to_task = {t.id: t for t in tasks}
    sorted_tasks = [id_to_task[tid] for tid in sorted_ids]
    
    # 2. 按「層」分組
    layers = self._compute_layers(sorted_tasks)
    
    # 3. 每層並行執行
    all_completed = []
    
    for layer in layers:
        # 這層的任務可以並行執行
        layer_tasks = [id_to_task[tid] for tid in layer]
        
        # gather 併發執行
        results = await asyncio.gather(
            *[self._run_with_semaphore(t) for t in layer_tasks],
            return_exceptions=True  # 不讓一個失敗影響其他
        )
        
        # 處理結果
        for task, result in zip(layer_tasks, results):
            if isinstance(result, Exception):
                task.status = TaskStatus.FAILED
                task.error = result
            all_completed.append(task)
        
        # 4. 檢查是否有失敗
        if self._any_failed(all_completed) and self.stop_on_failure:
            # 取消後續層
            remaining_layers = layers[len(layers):]
            for layer_tasks in remaining_layers:
                for tid in layer_tasks:
                    id_to_task[tid].status = TaskStatus.CANCELLED
            break
    
    return all_completed

def _compute_layers(self, sorted_task_ids: list[str]) -> list[list[str]]:
    """計算 DAG 的層次（同一層內的任務可以並行）"""
    
    # 簡化版本：只做粗糙的分層
    # 完整版本需要更複雜的圖演算法
    
    task_map = {t.id: t for t in self.tasks}
    layers = []
    remaining = set(sorted_task_ids)
    
    while remaining:
        # 找出所有依賴都已完成的任務
        ready = [
            tid for tid in remaining
            if task_map[tid].dependencies <= remaining - {tid}
        ]
        
        if not ready:
            raise CycleDetectedError("Graph contains cycle")
        
        layers.append(ready)
        remaining -= set(ready)
    
    return layers
```

---

## Concurrency 控制

### 信號量（Semaphore）

```python
async def _run_with_semaphore(self, task: Task) -> Task:
    """使用信號量控制並發數"""
    
    async with self._semaphore:
        return await self._execute_task(task)
```

這樣即使有 100 個任務，最多只會同時執行 5 個（`max_concurrency = 5`）。

### 為什麼需要控制並發？

| 原因 | 說明 |
|------|------|
| API 限流 | 避免同時發送過多請求被限流 |
| 資源限制 | 避免記憶體或連接數過多 |
| 穩定性 | 降低系統負載，提高成功率 |

---

## 超時管理

### Timeout 實現

```python
async def _execute_task(self, task: Task) -> Task:
    """執行單個任務，支援超時"""
    
    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now()
    
    try:
        result = await asyncio.wait_for(
            task.fn(**task.args),
            timeout=task.timeout or self.default_timeout
        )
        
        task.result = result
        task.status = TaskStatus.COMPLETED
        
    except asyncio.TimeoutError:
        task.status = TaskStatus.TIMEOUT
        task.error = TimeoutError(
            f"Task {task.id} timed out after {task.timeout}s"
        )
    
    task.completed_at = datetime.now()
    return task
```

### 任務級別超時

```python
# loom/core/task/engine.py
@dataclass
class Task:
    # ... 其他欄位 ...
    
    timeout: float | None = None  # 任務特定超時（覆蓋預設）
```

---

## stop_on_failure 選項

### 行為

```python
class TaskScheduler:
    def __init__(self, stop_on_failure: bool = True):
        self.stop_on_failure = stop_on_failure
        self._failures = 0
    
    async def _schedule_parallel(self, tasks: list[Task]) -> list[Task]:
        # ... 執行邏輯 ...
        
        # 每當有任務失敗
        for task in completed:
            if task.status == TaskStatus.FAILED:
                self._failures += 1
                
                if self.stop_on_failure:
                    # 取消所有尚未執行的任務
                    await self._cancel_pending()
                    break
```

### 使用場景

| 設定 | 適用場景 |
|------|----------|
| `stop_on_failure = True` | 嚴格順序執行的任務（如部署流水線） |
| `stop_on_failure = False` | 寬鬆並行任務（如批量資料處理） |

---

## Task Result 收集

### 結果聚合

```python
@dataclass
class TaskResult:
    """任務執行結果"""
    
    task_id: str
    status: TaskStatus
    result: Any = None
    error: str | None = None
    duration_ms: float
    metadata: dict = field(default_factory=dict)

class ExecutionReport:
    """執行報告"""
    
    total_tasks: int
    completed: int
    failed: int
    cancelled: int
    timed_out: int
    
    total_duration_ms: float
    results: list[TaskResult]
    
    @property
    def success_rate(self) -> float:
        return self.completed / self.total_tasks if self.total_tasks > 0 else 0.0
    
    @property
    def summary(self) -> str:
        return (
            f"Tasks: {self.total_tasks}, "
            f"Completed: {self.completed}, "
            f"Failed: {self.failed}, "
            f"Duration: {self.total_duration_ms:.0f}ms"
        )
```

### 生成報告

```python
async def _generate_report(
    self,
    completed_tasks: list[Task],
) -> ExecutionReport:
    """生成執行報告"""
    
    results = []
    total_duration = 0
    
    for task in completed_tasks:
        duration = 0.0
        if task.started_at and task.completed_at:
            duration = (task.completed_at - task.started_at).total_seconds() * 1000
        
        total_duration += duration
        
        results.append(TaskResult(
            task_id=task.id,
            status=task.status,
            result=task.result,
            error=str(task.error) if task.error else None,
            duration_ms=duration,
        ))
    
    return ExecutionReport(
        total_tasks=len(completed_tasks),
        completed=sum(1 for t in completed_tasks if t.status == TaskStatus.COMPLETED),
        failed=sum(1 for t in completed_tasks if t.status == TaskStatus.FAILED),
        cancelled=sum(1 for t in completed_tasks if t.status == TaskStatus.CANCELLED),
        timed_out=sum(1 for t in completed_tasks if t.status == TaskStatus.TIMEOUT),
        total_duration_ms=total_duration,
        results=results,
    )
```

---

## loom.toml 配置

```toml
[task_scheduler]

# 最大並發數
max_concurrency = 5

# 預設超時（秒）
default_timeout = 300

# 失敗時停止
stop_on_failure = true

# 任務完成後的通知
notify_on_complete = false
notify_on_failure = true
```

---

## 與 Autonomy Engine 的整合

### 定期任務調度

Task Scheduler 被 Autonomy Daemon 使用以支援 cron 任務：

```python
# loom/core/autonomy/daemon.py
class AutonomyDaemon:
    def __init__(self, scheduler: TaskScheduler):
        self.scheduler = scheduler
        self._cron_jobs: dict[str, CronJob] = {}
    
    def register_cron(self, cron_expr: str, task: Task):
        """註冊定時任務"""
        self._cron_jobs[cron_expr] = CronJob(
            expr=cron_expr,
            task=task,
            scheduler=self.scheduler,
        )
    
    async def run(self):
        """主循環"""
        while True:
            now = datetime.now()
            
            # 檢查所有 cron job
            for cron_expr, job in self._cron_jobs.items():
                if job.should_run(now):
                    asyncio.create_task(job.execute())
            
            await asyncio.sleep(60)  # 每分鐘檢查一次
```

---

## 總結

Task Scheduler 是 Task Engine 的「執行引擎」：

| 功能 | 說明 |
|------|------|
| asyncio.gather | 同層任務並行執行 |
| Semaphore | 控制最大並發數 |
| Timeout | 任務級別的超時控制 |
| stop_on_failure | 失敗時可選停止後續 |
| Execution Report | 完整的執行統計 |
| Autonomy 整合 | 支援 cron 定時任務 |
