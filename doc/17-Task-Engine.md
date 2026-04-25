# Task Engine — TaskList

> 架構演進：
> - v0.3.2（#128）：DAG 執行層，後因從未被接線而退回（#152 stall 根因）
> - v0.3.2.1（#153）：5 個 task_* 工具的「認知外骨骼」
> - v0.3.3（#205）：收斂為單一 `task_write`，廢除 `task_done` 的「動詞」語意造成的假完成幻覺
>
> 本文件描述 v0.3.3。先前版本見 git history。

---

## 為什麼從 5 個工具收斂成 1 個？

`task_done(node_id, result=...)` 的「動詞」語意是 issue #205 觀察到的失敗根源：

- 呼叫它感覺像是「向框架回報完成」
- 即使 result 是空的、artifact 不存在，呼叫這個動作本身就帶著「往前推進」的儀式感
- 在 7+ 節點的長任務裡，agent 心裡想的是「讓框架前進」而不是「確認產出真的存在」
- **儀式感 = 認知置換的源頭**

對照 Claude Code 的 `TodoWrite`：沒有 `done` 動詞，agent **重寫整張清單**把那一格從 `pending` 改成 `completed`。改的是自己桌上的便利貼，沒有對象、沒有觀眾、沒有儀式感——所以也沒有「報告了 = 做了」的幻覺。

同時：
- `result` 欄位徹底消失 → 所有產出**強制走檔案**（`write_file` → `tmp/*.md`）
- `depends_on` + `ready_nodes` 拿掉 → 順序由 agent 自己讀清單決定
- artifact 驗證問題（issue #205 P0）**結構上不可能發生**——因為清單根本不存產出

---

## 為什麼也不再需要 DAG 執行層？

實戰觀察：agent 在面對複雜任務時，本來就會自然形成「先做 A、拿 A 結果再做 B」的線性推理。原本的 DAG 結構試圖把這條線性推理「編譯」成分層並行的執行計畫，但：

1. 主 agent 面對圖結構時變成**動口不動手**的協調者——規劃完之後反而不自己執行（#152 的 `graph 66859851` 就是這個模式，4 個 L0 節點全部 stall）
2. 真正的並行需求是 **IO 層**的（同時抓 4 個 URL），不是**推理層**的——推理一旦並行就失去跨節點的累積上下文
3. DAG 的 `compile()` / `scheduler.asyncio.gather()` 在 v0.3.2 實際上**從未被接線**

結論：
- **推理層**用 TaskList（本文件）——平坦清單、agent 自己驅動
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

    @property
    def is_active(self) -> bool: ...   # PENDING or IN_PROGRESS
```

只有 `id`、`content`、`status`。沒有 `result`、沒有 `depends_on`、沒有 `error`、沒有 `metadata`。

### TaskStatus

```python
class TaskStatus(Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
```

`FAILED` 也拿掉了——放棄就把 status 設 `completed` 並在 content 註明 `[放棄] 原因`。失敗與成功是 agent 決策範疇，不是框架狀態。

### TaskList

```python
class TaskList:
    def replace(self, todos: list[dict]) -> None:
        """Replace entire list. todos = [{id, content, status?}]."""

    @property
    def nodes(self) -> list[TaskNode]: ...
    def active(self) -> list[TaskNode]: ...
    def status_summary(self) -> dict: ...
```

只有 replace 語意——沒有 add / remove / update / get_ready。每次 agent 想改就傳完整意圖狀態，框架負責 mirror。

---

## 工作流程（agent 視角）

```python
# 1. 開始任務 — 建立清單
task_write(todos=[
    {"id": "research", "content": "蒐集論文", "status": "pending"},
    {"id": "draft",    "content": "寫初稿到 tmp/draft.md", "status": "pending"},
    {"id": "audit",    "content": "審核", "status": "pending"},
])

# 2. 開始第一個節點 — 重寫清單把 status 改 in_progress
task_write(todos=[
    {"id": "research", "content": "...", "status": "in_progress"},
    {"id": "draft",    "content": "...", "status": "pending"},
    {"id": "audit",    "content": "...", "status": "pending"},
])

# 3. 完成 research、產出寫到 tmp/ — 重寫清單
write_file("tmp/research_summary.md", findings)   # 產出 → 磁碟
task_write(todos=[
    {"id": "research", "content": "...", "status": "completed"},
    {"id": "draft",    "content": "...", "status": "in_progress"},
    {"id": "audit",    "content": "...", "status": "pending"},
])

# ... 一直到全部 completed
```

**沒有 `next_ready` 自動推進**——agent 自己看清單決定下一步。

---

## Pre-Final-Response Self-Check（stream_turn middleware）

防止 agent 做到一半靜默結束回應：

```python
# session.py stream_turn() end_turn 分支
if has_active_nodes():
    inject("<system-reminder>\n" + build_self_check_message() + "\n</system-reminder>")
    continue   # loop back into LLM
```

觸發時機：`response.stop_reason == "end_turn"` 且 TaskList 仍有 PENDING / IN_PROGRESS 節點。注入一次後設 flag，同 turn 內不重複。

Agent 收到 reminder 的兩條出路：
1. 繼續執行未完成的節點
2. 用 `task_write` 重寫清單，把放棄的節點 status 改 `completed` 並在 content 註明放棄原因

---

## File-as-State

TaskList 只記「我有沒有忘記做這步」，**從來不存產出**。所有實際資料都走檔案：

```python
# ✅ 正確
write_file("tmp/dim_a.md", report_body)
task_write(todos=[..., {"id": "dim_a", "content": "...", "status": "completed"}, ...])

# ❌ 錯誤（無欄位可塞）
task_write(todos=[..., {"id": "dim_a", "content": report_body, "status": "completed"}, ...])
```

跨節點傳資料就用約定好的檔名（`tmp/dim_a.md` → 下游 `read_file` 取回）。中斷恢復也很自然：檔案存在就是「做過了」，檔案不存在就是「還沒做」，TaskList 的 status 只是輔助提示。

---

## 與先前版本的差異

| 項目 | v0.3.2 TaskGraph (#128) | v0.3.2.1 TaskList (#153) | v0.3.3 task_write (#205) |
|------|-------------------------|--------------------------|--------------------------|
| 結構 | DAG with compile() | 平坦清單 + depends_on | 平坦清單 |
| `depends_on` | 驅動排程 | 僅文件 + ready() 視圖 | 不存在 |
| `result` 欄位 | 有，溢出到 artifacts/ | 有，5000 char 截斷 | **不存在** |
| `task_done` | 有 | 有（含 verifier） | **不存在** |
| 並行執行 | levels + asyncio.gather | 無 | 無 |
| Tool 數量 | 5 個 | 5 個 | **1 個（task_write）** |
| 狀態載體 | TaskGraph + artifacts/ | TaskList + result 欄位 | **檔案系統（tmp/）** |

---

## 檔案定位

| 檔案 | 職責 |
|------|------|
| `loom/core/tasks/tasklist.py` | `TaskNode` / `TaskStatus` / `TaskList` 資料結構（~100 行） |
| `loom/core/tasks/manager.py` | `TaskListManager` — write 邊界、self-check message（~80 行） |
| `loom/platform/cli/tools.py` | `make_task_write_tool` — 唯一的 tool factory |
| `skills/task_list/SKILL.md` | Agent 可見的 Tier-1 skill genome |

相關文件：
- [18-Task-Scheduler.md](18-Task-Scheduler.md) — Async Jobs（JobStore + Scratchpad），IO 並行層
- Issue #205 — 收斂的根因分析與設計討論
