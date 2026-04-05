# Task Engine

Task Engine 是 Loom 的任務執行引擎。它負責將用戶的複雜請求拆解為 DAG（有向無環圖），然後調度執行。

---

## 為什麼需要 Task Engine？

用戶的請求往往是多步驟的：

```
用戶：「幫我準備下週的會議」
  │
  ├─▶ 讀取日曆（取得下週會議時間）
  │
  ├─▶ 讀取相關文件（準備會議資料）
  │
  ├─▶ 查詢專案進度（準備進度報告）
  │
  └─▶ 生成會議議程（彙整以上資訊）
```

Task Engine 負責：
1. **拆解** — 將請求拆分為可執行的任務
2. **排序** — 確定任務的執行順序（DAG）
3. **調度** — 按順序或並行執行任務
4. **錯誤處理** — 任務失敗時決定是否繼續

---

## DAG 結構

### 什麼是 DAG？

DAG（Directed Acyclic Graph）是一種沒有環路的圖結構。每個節點是一個任務，邊表示依賴關係。

```
┌──────────────┐
│  Task A      │  沒有依賴，最先執行
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Task B      │  依賴 Task A
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Task C      │  依賴 Task B
└──────────────┘
```

### 任務定義

```python
# loom/core/task/engine.py
@dataclass
class Task:
    """任務定義"""
    
    id: str                           # 唯一標識
    name: str                         # 任務名稱（人類可讀）
    description: str                  # 任務描述
    
    # 依賴
    dependencies: set[str] = field(default_factory=set)  # 依賴的任務 ID
    
    # 執行
    fn: Callable[..., Coroutine]      # 執行的異步函數
    args: dict = field(default_factory=dict)            # 傳給 fn 的參數
    
    # 狀態追蹤
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: Exception | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    
    # 錯誤處理
    retry_count: int = 0
    max_retries: int = 3
```

### 狀態機

```
                    ┌─────────────┐
                    │   PENDING   │  等待執行
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
         ┌─────────│  RUNNING    │  執行中
         │         └──────┬──────┘
         │                │
         ▼                ▼
┌─────────────┐    ┌─────────────┐
│   FAILED    │    │  COMPLETED  │
│ (after max  │    │             │
│  retries)   │    └─────────────┘
└─────────────┘
```

---

## Kahn's Topological Sort

### 為什麼需要拓撲排序？

DAG 的邊表示「必須先完成」的關係。拓撲排序找出任務的執行順序，確保每個任務在其依賴完成後才執行。

### Kahn's Algorithm 實現

```python
# loom/core/task/scheduler.py
def topological_sort(tasks: list[Task]) -> list[Task]:
    """
    Kahn's Topological Sort
    
    1. 計算每個節點的 in-degree（有多少邊指向它）
    2. 將 in-degree 為 0 的節點加入隊列（這些可以最先執行）
    3. 從隊列取出節點，將其加入結果
    4. 從圖中移除該節點，更新相關節點的 in-degree
    5. 重複 2-4 直到隊列為空
    """
    
    # 1. 建立鄰接表和 in-degree
    graph: dict[str, set[str]] = {}  # task_id -> 依賴它的任務
    in_degree: dict[str, int] = {}   # task_id -> in-degree
    
    for task in tasks:
        graph[task.id] = set()
        in_degree[task.id] = 0
    
    for task in tasks:
        for dep_id in task.dependencies:
            if dep_id not in graph:
                raise ValueError(f"Unknown dependency: {dep_id} for task {task.id}")
            graph[dep_id].add(task.id)
            in_degree[task.id] += 1
    
    # 2. 將 in-degree 為 0 的節點加入隊列
    queue = deque([
        task_id for task_id, degree in in_degree.items() if degree == 0
    ])
    
    result = []
    
    # 3. 處理隊列
    while queue:
        task_id = queue.popleft()
        result.append(task_id)
        
        # 4. 更新 in-degree
        for dependent in graph[task_id]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
    
    # 5. 檢查是否有環（如果 result 數量不等於任務數量，說明有環）
    if len(result) != len(tasks):
        raise CycleDetectedError("Task graph contains a cycle")
    
    return result
```

### 範例

```
Tasks:
  A: dependencies = []
  B: dependencies = [A]
  C: dependencies = [A]
  D: dependencies = [B, C]

In-degree:
  A: 0
  B: 1
  C: 1
  D: 2

Execution order: A → B,C（可並行）→ D
```

---

## 任務執行器

### Sequential vs Parallel

```python
# loom/core/task/executor.py
class TaskExecutor:
    def __init__(self, stop_on_failure: bool = True):
        self.stop_on_failure = stop_on_failure
    
    async def execute(
        self,
        tasks: list[Task],
        parallel: bool = False,
    ) -> list[Task]:
        """
        執行任務
        
        Args:
            tasks: 任務列表
            parallel: 是否允許並行執行
        
        Returns:
            執行完成的任務列表（按完成順序）
        """
        # 1. 拓撲排序
        sorted_ids = topological_sort(tasks)
        id_to_task = {t.id: t for t in tasks}
        
        # 2. 轉換為有序列表
        sorted_tasks = [id_to_task[tid] for tid in sorted_ids]
        
        if parallel:
            return await self._execute_parallel(sorted_tasks)
        else:
            return await self._execute_sequential(sorted_tasks)
    
    async def _execute_sequential(
        self,
        tasks: list[Task],
    ) -> list[Task]:
        """順序執行"""
        completed = []
        
        for task in tasks:
            # 等待依賴完成
            await self._wait_for_dependencies(task, completed)
            
            # 執行
            result_task = await self._run_task(task)
            completed.append(result_task)
            
            # 檢查是否失敗
            if result_task.status == TaskStatus.FAILED and self.stop_on_failure:
                # 取消後續任務（標記為 CANCELLED）
                for remaining in tasks[len(completed):]:
                    remaining.status = TaskStatus.CANCELLED
                break
        
        return completed
    
    async def _execute_parallel(
        self,
        tasks: list[Task],
    ) -> list[Task]:
        """並行執行（asyncio.gather）"""
        id_to_task = {t.id: t for t in tasks}
        completed = []
        running: set[asyncio.Task] = {}
        
        # 初始：找出沒有依賴的任務
        ready = [t for t in tasks if not t.dependencies]
        
        for task in ready:
            running.add(asyncio.create_task(self._run_task(task)))
        
        # 事件循環
        while running:
            done, _ = await asyncio.wait(
                running,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for d in done:
                result_task = d.result()
                completed.append(result_task)
                running.remove(d)
                
                # 如果失敗且 stop_on_failure
                if result_task.status == TaskStatus.FAILED and self.stop_on_failure:
                    # 取消其他任務
                    for r in running:
                        r.cancel()
                    running.clear()
                    break
                
                # 找出依賴此任務的新 ready 任務
                newly_ready = [
                    t for t in tasks
                    if t.id not in {rt.id for rt in completed}
                    and t.status == TaskStatus.PENDING
                    and result_task.id in t.dependencies
                    and all(dep_id in {rt.id for rt in completed}
                            for dep_id in t.dependencies)
                ]
                
                for task in newly_ready:
                    running.add(asyncio.create_task(self._run_task(task)))
        
        return completed
```

---

## 錯誤處理與重試

### 重試邏輯

```python
async def _run_task(self, task: Task) -> Task:
    """執行單個任務，支援重試"""
    
    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now()
    
    for attempt in range(task.max_retries + 1):
        try:
            # 解析依賴的結果
            args = self._resolve_args(task.args, task.dependencies)
            
            # 執行
            result = await task.fn(**args)
            
            task.result = result
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            return task
            
        except Exception as e:
            task.retry_count = attempt + 1
            task.error = e
            
            if attempt < task.max_retries:
                # 重試前等待（指数退避）
                wait_time = 2 ** attempt
                logger.warning(
                    f"Task {task.id} failed (attempt {attempt + 1}), "
                    f"retrying in {wait_time}s: {e}"
                )
                await asyncio.sleep(wait_time)
            else:
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                logger.error(f"Task {task.id} failed after {attempt + 1} attempts")
    
    return task
```

### 錯誤傳播

```python
class TaskGraphError(Exception):
    """Task Engine 相關錯誤"""
    pass

class CycleDetectedError(TaskGraphError):
    """檢測到循環依賴"""
    pass

class DependencyNotFoundError(TaskGraphError):
    """依賴的任務不存在"""
    pass
```

---

## loom.toml 配置

```toml
[task_engine]

# 預設並行度
default_parallel = false

# 失敗時停止後續
stop_on_failure = true

# 重試設定
default_max_retries = 3
retry_backoff_base = 2  # 指數退避 base

# 逾時設定
default_timeout_seconds = 300
```

---

## 與其他模組的整合

```
┌─────────────────────────────────────────────────────────────┐
│                      Task Engine 流程                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. 用戶請求                                                │
│     └──▶ NaturalLanguageRequest                            │
│                                                             │
│  2. 意圖識別                                                │
│     └──▶ LLM 拆解為 Task 列表                               │
│                                                             │
│  3. DAG 建構                                                │
│     └──▶ 設定 Task.dependencies                            │
│                                                             │
│  4. 拓撲排序                                                │
│     └──▶ Kahn's Algorithm → 執行順序                       │
│                                                             │
│  5. 執行                                                    │
│     └──▶ asyncio.gather（並行）或 順序執行                  │
│                                                             │
│  6. 結果彙整                                                │
│     └──▶ 將 Task.result 組合成最終回覆                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 總結

Task Engine 是 Loom 的「任務規劃師」：

| 功能 | 說明 |
|------|------|
| DAG 拆解 | 將複雜請求拆分為有依賴關係的任務圖 |
| 拓撲排序 | Kahn's Algorithm 確保正確的執行順序 |
| 並行執行 | asyncio.gather 支援並行加速 |
| 錯誤處理 | 重試機制、指數退避、stop_on_failure |
| 狀態追蹤 | PENDING → RUNNING → COMPLETED/FAILED |
