# LoomSession 生命週期詳解

> 最核心構建塊的完整生命週期文件。

---

## 定位

`LoomSession`（`loom/core/session.py`）是 Loom 最核心的類，幾乎所有模組都與它互動：
- Harness（middleware pipeline、registry）
- Memory（semantic、episodic、procedural、relational、jobstore、scratchpad）
- Cognition（router、reflection）
- Platform（cli、tui、api、discord）
- Autonomy（daemon）

理解 Session 的生命週期，是理解整個 Loom 架構的前提。

---

## 建構流程

### 1. Constructor

```python
def __init__(self, config: LoomConfig, db: aiosqlite.Connection, ...):
    # 1. 設定基本屬性
    self._config = config
    self._db = db
    self._session_id = session_id or str(uuid.uuid4())
    self._turn_index = 0

    # 2. 建 PermissionContext
    self._perm = PermissionContext(session_id=self._session_id)
    if config.harness.strict_sandbox:
        self._perm.enable_exec_auto()  # 注入 workspace exec grant

    # 3. 建 Memory 實例
    self._semantic  = SemanticMemory(db)
    self._episodic  = EpisodicMemory(db)
    self._procedural = ProceduralMemory(db, config_dir)
    self._relational = RelationalMemory(db)

    # 4. 建 Governor（v0.2.9.0）
    self._governor = MemoryGovernor(
        semantic=self._semantic,
        procedural=self._procedural,
        relational=self._relational,
        episodic=self._episodic,
        db=db,
        config=config.memory.governance,
    )

    # 5. 建 JobStore + Scratchpad
    self._jobstore = JobStore()
    self._scratchpad = Scratchpad()

    # 6. 建 AbortController
    self._abort = AbortController()

    # 7. 建 MiddlewarePipeline（尚未插入工具）
    self._pipeline: MiddlewarePipeline | None = None

    # 8. 建 PromptStack
    self._prompt_stack = PromptStack.from_config(config.raw, base_dir=...)
```

**注意**：JobStore / Scratchpad 在建構函式內建立，但工具（run_bash、fetch_url 等）工廠需要這些實例作為閉包捕獲的變數——因此 `make_run_bash_tool()` 等在 `start()` 內才呼叫，不在 constructor 內。

---

### 2. start() — 初始化完成

```python
async def start(self) -> None:
    # 1. 註冊內建工具
    from loom.platform.cli import tools as cli_tools
    self._registry = cli_tools.build_registry(
        workspace=self._workspace,
        confirm_fn=self._make_confirm_fn(),
        jobstore=self._jobstore,
        scratchpad=self._scratchpad,
    )

    # 2. 註冊 session 工具（memorize、relate 等）
    self._register_session_tools()

    # 3. 建 MiddlewarePipeline
    self._pipeline = MiddlewarePipeline([
        LifecycleMiddleware(
            registry=self._registry,
            on_lifecycle=self._on_action_lifecycle,    # → episodic memory
            on_state_change=self._on_state_change,    # → TUI 更新
        ),
        TraceMiddleware(
            on_trace=self._on_tool_trace,            # → episodic memory
        ),
        SchemaValidationMiddleware(registry=self._registry),
        BlastRadiusMiddleware(
            perm_ctx=self._perm,
            confirm_fn=self._make_confirm_fn(),
            scope_resolvers=self._build_scope_resolvers(),
        ),
        LifecycleGateMiddleware(registry=self._registry),
        # 工具 executor 在最內層
    ])

    # 4. 安裝 Plugin
    plugin_registry = PluginRegistry()
    plugin_registry.install_into(self)

    # 5. 安裝內建 Extension（DreamingPlugin、MCP client 等）
    self._install_builtin_extensions()

    # 6. 建 Cognition 實例
    self._router = LLMRouter(config=config.llm, ...)
    self._reflection = ReflectionService(
        semantic=self._semantic,
        episodic=self._episodic,
        llm_fn=self._router.chat,
    )

    # 7. 初始化 PromptStack
    self._prompt_stack.load()

    # 8. 初始化 TaskList（平坦清單，認知外骨骼）
    self._tasklist = tasklist_module.TaskList()

    # 9. 初始化 Autonomy 整合（若已配置）
    if self._autonomy_daemon:
        self._autonomy_daemon.set_session(self)
```

---

### 3. stream_turn() — 主要互動迴圈

`stream_turn()` 是 Session 與 LLM 互動的核心迴圈：

```python
async def stream_turn(
    self,
    message: str,
    origin: ToolCallOrigin = "interactive",
    **kwargs,
) -> AsyncIterator[ResponseEvent]:
    self._turn_index += 1
    self._abort.reset()     # 每個 turn 復用 AbortController

    # Phase 1: Build messages
    messages = self._build_messages(message)

    # Phase 2: Streaming LLM call
    async for chunk in self._router.stream(messages):
        yield chunk

    # Phase 3: Collect tool calls from LLM response
    tool_calls = self._extract_tool_calls(response)

    # Phase 4: Execute all tool calls via middleware pipeline
    for call in tool_calls:
        call.origin = origin
        call.metadata["session_id"] = self._session_id
        call.metadata["abort_signal"] = self._abort.signal

        result = await self._pipeline.process(call, self._execute_tool)
        results.append(result)

    # Phase 5: end_turn — TaskList self-check + Jobs update
    await self._end_turn()
```

#### end_turn 的兩個注入（turn boundary）

```python
async def _end_turn(self) -> None:
    # 1. TaskList self-check（pre-final-response self-check）
    pending = self._tasklist.pending_items()
    if pending:
        self._inject_reminder(f"[Pending tasks] {pending}")

    # 2. Jobs update
    if not self._jobs_inject_done:
        jobs_msg = _build_jobs_inject_message(self._jobstore)
        if jobs_msg:
            self._inject_reminder(f"[Jobs update]\n{jobs_msg}")
            self._jobs_inject_done = True
```

`_jobs_inject_done` 在每個 turn 開始時重置，讓後續 turn 可以再次注入。

---

## Tool 執行路徑

```
Pipeline.process(call)
  ↓
LifecycleMiddleware.process()
  ├─ 建立 ActionRecord（DECLARED）
  ├─ 建立 LifecycleContext
  ├─ await next(call) → 內層 pipeline
  │
  │  TraceMiddleware → SchemaValidation → BlastRadius → LifecycleGate
  │                                                    ↓
  │                                              工具 handler
  │                                                    ↓
  │                                            ToolResult
  │                                                    ↓
  │  ← return to LifecycleMiddleware
  │
  ├─ transition(DENIED/COMMITTED/REVERTED…) → MEMORIALIZED
  └─ return ToolResult
```

---

## 狀態回調（Callbacks）

### on_action_lifecycle

每個工具執行完成（MEMORIALIZED）時觸發，寫入 EpisodicMemory：

```python
async def _on_action_lifecycle(self, record: ActionRecord) -> None:
    await self._episodic.append(
        SessionLogEntry(
            session_id=self._session_id,
            turn=self._turn_index,
            action=record.tool_name,
            state=record.final_state,
            duration_ms=record.elapsed_ms,
            result_summary=record.result.output if record.result else None,
        )
    )
```

### on_state_change

每次狀態轉換時觸發，TUI 用來更新工具狀態顯示：

```python
async def _on_state_change(
    self, record: ActionRecord, old: str, new: str,
) -> None:
    # 發送事件讓 TUI widget 更新
    for handler in self._state_change_handlers:
        await handler(record, old, new)
```

---

## stop() — 優雅關閉

```python
async def stop(self) -> None:
    # 1. 停止 Autonomy daemon（如果有的話）
    if self._autonomy_daemon:
        self._autonomy_daemon.stop()

    # 2. 取消所有進行中的 jobs
    await self._jobstore.cancel_all(reason="session_ended")
    self._scratchpad.clear()

    # 3. 觸發 AbortController（通知還在等的 task 收攤）
    self._abort.abort()

    # 4. 執行 Decay Cycle
    await self._governor.run_decay_cycle()

    # 5. 關閉 DB 連線
    await self._db.close()

    # 6. 通知 plugin hooks
    for plugin in self._plugin_registry.all():
        plugin.on_session_stop(self)
```

**重要**：AbortController 的 abort 在 `cancel_all()` 之後——確保取消 job 時沒有 task 仍在等待。

---

## Tool Registry 的建立時機

`build_registry()` 在 `start()` 內呼叫，而非 constructor。這是因為 `make_run_bash_tool` 等工廠需要捕獲 `self._jobstore`、`self._scratchpad`、`self._abort` 等已經初始化的實例：

```python
# start() 內
registry = ToolRegistry()
registry.register(make_run_bash_tool(
    workspace=self._workspace,
    abort_signal=self._abort.signal,      # closure captured here
    command_scanner=CommandScanner(),
    jobstore=self._jobstore,
))
registry.register(make_fetch_url_tool(
    jobstore=self._jobstore,
    scratchpad=self._scratchpad,
))
# ... 其他工具
```

---

## Session 與 Memory 的關係

```
LoomSession
  ├─ _semantic   → SemanticMemory（長期事實，with Governance）
  ├─ _episodic   → EpisodicMemory（Session Log，turn-by-turn）
  ├─ _procedural → ProceduralMemory（SkillGenome，skills/）
  ├─ _relational → RelationalMemory（三元組，偏好/關係）
  ├─ _jobstore   → JobStore（背景 IO 任務）
  └─ _scratchpad → Scratchpad（過程產物，session-scoped）
```

`MemoryGovernor` 包裝所有寫入路徑，always-on，提供矛盾偵測、Admission Gate、Decay Cycle。

---

## 設計原則

1. **建構與初始化分離**：Constructor 只建立核心實例，`start()` 完成所有實際初始化（工具註冊、pipeline 建構、plugin 安裝）
2. **JobStore/Scratchpad 先於工具工廠**：jobstore 和 scratchpad 在 constructor 建立，工廠閉包捕獲它們
3. **AbortController 每 turn reset**：同一個 controller 在每個 turn 開始時 `reset()`，避免上一 turn 的取消影響下一 turn
4. **Lifetime scope**：所有需要 session 壽命的資源（DB、jobstore、scratchpad）都在 constructor 建立；所有需要 session 結束時清理的資源都在 `stop()` 清理
5. **Callback 永遠是 async**：所有 `on_lifecycle` / `on_state_change` 都是 `async def`，不可使用 sync callback

---

*文件草稿 | 2026-04-26 03:10 Asia/Taipei*