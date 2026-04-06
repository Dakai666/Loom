# Task Engine

Task Engine 是 Loom 的任務執行引擎。它負責將用戶的複雜請求拆解為 DAG（有向無環圖），然後由 TaskScheduler 調度執行。

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
1. **拆解** — 將請求拆分為可執行的任務節點
2. **排序** — Kahn's topological sort 確定執行順序
3. **調度** — TaskScheduler 按 level 並行或順序執行
4. **錯誤處理** — 失敗時決定是否繼續下游節點

---

## DAG 結構（loom/core/tasks/graph.py）

### TaskNode

```python
# loom/core/tasks/graph.py
@dataclass
class TaskNode:
    content: str                           # 任務描述文字（人類可讀）
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    depends_on: list[str] = field(default_factory=list)   # 依賴的節點 ID
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED)

    def complete(self, result: Any = None) -> None: ...
    def fail(self, error: str) -> None: ...
    def skip(self) -> None: ...
```

### TaskGraph

```python
class TaskGraph:
    def add(self, content: str, depends_on: list[TaskNode] | None = None,
            metadata: dict | None = None) -> TaskNode: ...
    def get(self, node_id: str) -> TaskNode | None: ...
    @property
    def nodes(self) -> list[TaskNode]: ...
    def pending(self) -> list[TaskNode]: ...
    def compile(self) -> ExecutionPlan: ...  # Kahn's topological sort
    def reset(self) -> None: ...             # 重置所有節點為 PENDING
```

### TaskStatus

```python
class TaskStatus(Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"
    SKIPPED     = "skipped"
```

### ExecutionPlan

```python
@dataclass
class ExecutionPlan:
    levels: list[list[TaskNode]]   # Kahn's sort 產出的分層列表

    @property
    def parallel_groups(self) -> list[list[TaskNode]]:
        """僅返回有多個節點的層（真正的並行組）"""
        return [lvl for lvl in self.levels if len(lvl) > 1]

    @property
    def all_nodes(self) -> list[TaskNode]:
        return [node for level in self.levels for node in level]
```

---

## Kahn's Topological Sort

### 演算法

```python
# TaskGraph.compile() — Kahn's algorithm
def compile(self) -> ExecutionPlan:
    nodes = self._nodes
    in_degree: dict[str, int] = {nid: 0 for nid in nodes}
    children: dict[str, list[str]] = defaultdict(list)

    for nid, node in nodes.items():
        for dep_id in node.depends_on:
            if dep_id not in nodes:
                raise ValueError(f"Node '{nid}' depends on unknown node '{dep_id}'")
            in_degree[nid] += 1
            children[dep_id].append(nid)

    queue: deque[str] = deque(
        nid for nid, deg in in_degree.items() if deg == 0
    )
    levels: list[list[TaskNode]] = []
    visited = 0

    while queue:
        level_size = len(queue)
        level: list[TaskNode] = []
        for _ in range(level_size):
            nid = queue.popleft()
            level.append(nodes[nid])
            visited += 1
            for child_id in children[nid]:
                in_degree[child_id] -= 1
                if in_degree[child_id] == 0:
                    queue.append(child_id)
        levels.append(level)

    if visited != len(nodes):
        raise ValueError("Cycle detected in TaskGraph — cannot produce a valid execution plan.")

    return ExecutionPlan(levels=levels)
```

### 範例

```
TaskNodes:
  A: depends_on = []
  B: depends_on = [A]
  C: depends_on = [A]
  D: depends_on = [B, C]

Kahn's sort → levels:
  Level 0: [A]        (in_degree = 0)
  Level 1: [B, C]      (in_degree = 0 after A done, 可並行)
  Level 2: [D]         (等待 B+C)
```

---

## 狀態機

```
PENDING → IN_PROGRESS → COMPLETED
                       → FAILED
                       → SKIPPED（被 stop_on_failure 跳過）
```

---

## 與 TaskScheduler 的關係

| 元件 | 檔案 | 職責 |
|------|------|------|
| `TaskNode` | `graph.py` | 單一任務節點的資料結構 |
| `TaskGraph` | `graph.py` | DAG 建構 + Kahn's sort → `ExecutionPlan` |
| `ExecutionPlan` | `graph.py` | 分層執行序列 |
| `TaskScheduler` | `scheduler.py` | 接收 `ExecutionPlan`，執行 asyncio.gather |

---

## 總結

Task Engine 是 Loom 的「任務規劃師」：

| 功能 | 說明 |
|------|------|
| DAG 拆解 | 將複雜請求拆分為有依賴關係的任務圖 |
| 拓撲排序 | Kahn's Algorithm 確保正確的執行順序 |
| 分層輸出 | `ExecutionPlan.levels` 告知同層可並行 |
| 循環檢測 | 有環時拋出 `ValueError` |
