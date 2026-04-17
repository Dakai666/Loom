# Task Engine — TaskList

> 架構演進（#153 / v0.3.2.1）：Task Engine 自 v0.3.2 的 DAG 執行層退回到「認知外骨骼」定位。主 agent 自己讀清單、自己決定順序、自己動手；harness 不再做拓撲排序，也不再驅動節點執行。先前 TaskGraph 的 `compile()` / `TaskScheduler` / `ExecutionPlan` 已全數移除（參見 #152 autonomy stall 根因分析）。

---

## 為什麼不再需要 DAG 執行層？

實戰觀察：agent 在面對複雜任務時，本來就會自然形成「先做 A、拿 A 結果再做 B」的線性推理。原本的 DAG 結構試圖把這條線性推理「編譯」成分層並行的執行計畫，但：

1. 主 agent 面對圖結構時變成**動口不動手**的協調者——規劃完之後反而不自己執行（#152 的 `graph 66859851` 就是這個模式，4 個 L0 節點全部 stall）
2. 真正的並行需求是 **IO 層**的（同時抓 4 個 URL），不是**推理層**的——推理一但並行就失去跨節點的累積上下文
3. DAG 的 `compile()` / `scheduler.asyncio.gather()` 在 v0.3.2 實際上**從未被接線**——2000 行代碼供著一個沒被觸發的執行框架

結論：
- **推理層**改用 TaskList（本文件）——平坦清單、agent 自己驅動
- **IO 層**用 JobStore + Scratchpad（見 [18-Task-Scheduler.md](18-Task-Scheduler.md)）——`async_mode=True` 下放並行到 tool layer

---

## TaskList 結構（`loom/core/tasks/tasklist.py`）

### TaskNode

```python
@dataclass
class TaskNode:
    id: str
    content: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)  # documentation only
    result: str | None = None
    result_summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool: ...
    @property
    def is_active(self) -> bool: ...
    def complete(self, result: str | None = None) -> None: ...
    def fail(self, error: str) -> None: ...
```

### TaskStatus

```python
class TaskStatus(Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"
```

`SKIPPED` 隨 DAG 執行層一併移除——agent 自主決定是否跳過，透過 `task_done(error=...)` 顯式記錄即可。

### TaskList

```python
class TaskList:
    def add(self, node_id: str, content: str,
            depends_on: list[str] | None = None,
            metadata: dict | None = None) -> TaskNode: ...
    def remove(self, node_id: str) -> None: ...      # 僅限 PENDING 節點
    def update(self, node_id: str, ...) -> TaskNode: ...  # 僅限 PENDING 節點
    def get(self, node_id: str) -> TaskNode | None: ...

    @property
    def nodes(self) -> list[TaskNode]: ...
    def pending(self) -> list[TaskNode]: ...
    def active(self) -> list[TaskNode]: ...
    def ready(self) -> list[TaskNode]: ...   # depends_on 全 COMPLETED 的 PENDING
    def validate(self) -> None: ...          # DFS 三色 cycle detection
    def status_summary(self) -> dict: ...
```

**關鍵設計**：
- `depends_on` 僅為**文件**——agent 自己閱讀並判斷順序，harness 不據此排程
- `ready()` 是方便 agent 查詢的**視圖**，非排程指令；agent 可自由跳過依賴
- `validate()` 仍做 DFS cycle detection——環是 plan bug，即使依賴是 advisory 也應抓出來
- `update()` / `remove()` 只能作用在 `PENDING` 節點——已啟動/完成節點不允許修改歷史

---

## 工作流程（agent 視角）

```
1. task_plan     — 建立清單（開始一個複雜任務前）
2. task_status   — 隨時查清單狀態、反漂移
3. task_modify   — 計畫需調整時增刪改未啟動節點
4. 逐節點執行（直接用其他 tools，不派給 sub-agent）
5. task_done     — 每完成一個節點：傳 result 或 error
6. task_read     — 需要上游完整結果時按需取用（Pull Model）
```

**無 `next_ready` 自動推進**——agent 自己看 `ready()` 決定下一步。

---

## Pre-Final-Response Self-Check（stream_turn hook）

防止 agent 做到一半靜默結束回應：

```python
# session.py stream_turn() end_turn 分支
if has_active_nodes():
    inject("<system-reminder>\n" + build_self_check_message() + "\n</system-reminder>")
    continue   # loop back into LLM
```

觸發時機：`response.stop_reason == "end_turn"` 且 TaskList 仍有 PENDING / IN_PROGRESS 節點。注入一次後設 flag，同 turn 內不重複。

Agent 收到 reminder 的三條出路：
1. 繼續執行下一個節點
2. `task_done(node_id=..., error="原因")` 明確標記放棄
3. 如果真的已完成，用 `task_done(result=...)` 補上——然後自然 end_turn

---

## Result 硬截斷

`task_done(result=...)` 超過 `HARD_RESULT_CAP = 5000` chars 會在 Manager 層截斷並附通知，引導 agent 改用 `write_file` 把大型產出寫到磁碟、TaskList 只存摘要與路徑。

**為什麼硬截斷而不自動進 Scratchpad？** — Scratchpad 是 IO 過程產物的暫存區；`task_done` 的 result 屬於 agent 的**最終交付**，應該顯式決定去處（磁碟、記憶系統、或壓在摘要裡），而不是偷偷寄放。

---

## 與 #128 / v0.3.2 TaskGraph 的差異

| 項目 | v0.3.2 TaskGraph | v0.3.2.1 TaskList |
|------|------------------|-------------------|
| 結構 | DAG with compile() | 平坦清單 |
| `depends_on` | 驅動排程 | 僅文件 |
| 並行執行 | levels + asyncio.gather | 無（並行下放到 async_jobs） |
| 跨 session 持久化 | `~/.loom/task_graphs/` | 無（交給記憶系統） |
| 結果溢出 | `~/.loom/artifacts/` | Scratchpad（見 #18） |
| SKIPPED 狀態 | 有 | 無（改用 `task_done(error=)`） |
| Tool API | 相同 5 個 | 相同 5 個（無 breaking change） |

---

## 檔案定位

| 檔案 | 職責 |
|------|------|
| `loom/core/tasks/tasklist.py` | `TaskNode` / `TaskStatus` / `TaskList` 資料結構 |
| `loom/core/tasks/manager.py` | `TaskListManager` — tool 呼叫邊界、result 截斷、self-check message |
| `skills/task_list/SKILL.md` | Agent 可見的 Tier-1 skill genome |

相關文件：
- [18-Task-Scheduler.md](18-Task-Scheduler.md) — Async Jobs（JobStore + Scratchpad），IO 並行層
