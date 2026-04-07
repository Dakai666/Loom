# Action Lifecycle 詳解

> v0.2.8.0 引入 Control-first 架構，工具呼叫的每個生命週期狀態從「事後補標籤」升級為「即時控制閘門」。

---

## 為什麼需要 Action Lifecycle？

### v0.2.6.3 之前：事後補標籤

```
工具呼叫 → 執行 → 結果出來 → 把 DECLARED/AUTHORIZED/EXECUTING 全部補上
```

狀態機存在，但它描述的是「已經發生的事情」，對執行過程沒有影響力。就像看完電影才貼上分級標籤——標籤是真實的，但它沒有阻止任何人進場。

### v0.2.8.0：即時控制閘門

```
DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED → ...
                ↓ 閘門          ↓ 閘門       ↓ 閘門
           （不通過就停）   （不通過就停）  （這才是真正執行的時刻）
```

每個狀態轉換都是一道閘門。閘門關閉，執行就不會繼續。框架在每個 checkpoint 都有機會問「這一步該不該繼續」。

---

## 完整狀態機

```
                    ┌─────────────────────────────────────────────────┐
                    │              Action Lifecycle                    │
                    └─────────────────────────────────────────────────┘

  DECLARED ──┬──→ AUTHORIZED ──→ PREPARED ──→ EXECUTING ──→ OBSERVED
             │         │              │             │             │
             │     (→ DENIED)    (→ ABORTED)   (→ ABORTED)      │
             │                                (→ TIMED_OUT)      │
             │                                                    ▼
             │                                            VALIDATED ──┬──→ COMMITTED
             │                                                         │         │
             │                                                    REVERTING      │
             │                                                         │         │
             │                                                    REVERTED       │
             │                                                         │         │
             └───────────────────────────────────────────────→ MEMORIALIZED ←───┘

  所有路徑最終都到 MEMORIALIZED（保證）
```

### 終止狀態

每個 terminal state 都會銜接到 MEMORIALIZED：

| 終止狀態 | 意義 |
|----------|------|
| `DENIED` | BlastRadius 拒絕（用戶拒絕或 Schema 驗證失敗） |
| `ABORTED` | precondition 失敗、abort 信號、執行前主動終止 |
| `TIMED_OUT` | 工具執行超時（由外層 LifecycleMiddleware 處理） |
| `REVERTED` | post_validator 失敗 + rollback_fn 執行完畢 |
| `COMMITTED` | 正常完成，無 post_validator 或驗證通過 |

---

## 兩層 Middleware 架構

單一 Middleware 無法解決一個根本問題：`next(call)` 是一個不透明呼叫，外層在呼叫它之前無法注入「執行的那一刻」。

因此 v0.2.8.0 將 lifecycle 拆成兩層：

```
LifecycleMiddleware（最外層）
  ↓ 建立 ActionRecord，注入 LifecycleContext
  → TraceMiddleware
    → SchemaValidationMiddleware
      → BlastRadiusMiddleware（寫入 ctx.authorization_result）
        → LifecycleGateMiddleware（最內層）
            ↓ AUTHORIZED → PREPARED → EXECUTING → OBSERVED
            → 工具 handler 實際執行
        ↑ OBSERVED 已設定
      ↑
    ↑
  ↑ VALIDATED → COMMITTED / REVERTING → REVERTED → MEMORIALIZED
```

### 職責分工

| 層次 | 位置 | 職責 |
|------|------|------|
| `LifecycleMiddleware` | 最外層 | DECLARED、LifecycleContext 注入；失敗路徑處理（DENIED、TIMED_OUT）；post-OBSERVED 狀態（VALIDATED → COMMITTED / REVERTED）；MEMORIALIZED |
| `LifecycleGateMiddleware` | 最內層（handler 前） | AUTHORIZED 確認；PREPARED（precondition_checks 執行）；EXECUTING（精確時刻 + abort racing）；OBSERVED |

### LifecycleContext：共享狀態橋

兩層 middleware 透過 `call.metadata["_lifecycle_ctx"]` 共享一個 `LifecycleContext` 物件：

```python
@dataclass
class LifecycleContext:
    record: ActionRecord              # 正在追蹤的 ActionRecord（引用）
    authorization_result: bool | None # BlastRadius 寫入（True/False/None）
    authorization_reason: str | None  # 授權原因（"pre-authorized"/"user confirmed"等）
    _on_state_change: ...             # 狀態變更 callback（UI 用）
    _on_lifecycle: ...                # MEMORIALIZED callback（記憶層用）
```

`BlastRadiusMiddleware` 把決定寫入 `ctx.authorization_result`，`LifecycleGateMiddleware` 讀取它來驅動 AUTHORIZED 轉換。不需要直接耦合。

---

## 各狀態詳解與具體場景

### DECLARED

**時機：** `LifecycleMiddleware` 建立 `ActionRecord` 的瞬間，LLM 的工具請求剛進入 pipeline。

**場景：**
- Agent 請求 `run_bash("python test.py")` → DECLARED 立即觸發
- 這個狀態本身不是閘門，只是記錄「有這個請求」

---

### AUTHORIZED / DENIED

**時機：** `BlastRadiusMiddleware` 做出授權決定的瞬間（不是事後補的）。

**通過的常規情況：**
```
SAFE 工具（read_file、recall）
  → is_authorized() = True → 自動通過，reason = "pre-authorized"

GUARDED 工具，session 內已授權（write_file 第二次）
  → is_authorized() = True → 通過，reason = "pre-authorized"

GUARDED 工具，exec_auto 模式下的 run_bash（workspace 範圍內）
  → exec_auto_approved() = True → 通過，reason = "exec_auto"

GUARDED 工具，用戶確認
  → confirm_fn() = True → 通過，reason = "user confirmed"
```

**失敗（→ DENIED）：**
```
GUARDED 工具，用戶點擊「否」
  → confirm_fn() = False → DENIED → MEMORIALIZED
  → BlastRadius 立刻回傳 permission_denied ToolResult
  → LifecycleGateMiddleware 從未執行

GUARDED 工具，exec_auto 但絕對路徑逃逸 workspace
  → exec_escape_fn() = True → 跳過 exec_auto，要求確認
  → 用戶拒絕 → DENIED
```

---

### PREPARED / ABORTED（precondition 失敗）

**時機：** `LifecycleGateMiddleware` 在 AUTHORIZED 後、EXECUTING 前，依序執行所有 `precondition_checks`。

**通過：**
```
ToolDefinition 沒有 precondition_checks → 直接通過
所有 checks 回傳 True → PREPARED
```

**失敗（→ ABORTED）的各種情況：**

**常規失敗 — 條件不滿足，明確回傳 False：**
```python
# 例：寫入鎖未取得
async def require_write_lock(call: ToolCall) -> bool:
    return await lock_manager.is_held(call.session_id)
    # 其他 session 持有鎖時回傳 False → ABORTED
    # 工具 handler 永遠不會被呼叫
```

```python
# 例：staging 健康檢查
async def require_staging_green(call: ToolCall) -> bool:
    status = await staging_api.health()
    return status == "healthy"
    # CI 紅燈時 → ABORTED，不讓 deploy 工具執行
```

```python
# 例：時間窗口限制
async def require_maintenance_window(call: ToolCall) -> bool:
    now = datetime.now(tz=TZ_TAIPEI)
    return now.weekday() < 5 and 9 <= now.hour < 18
    # 下班後或週末 → ABORTED
```

```python
# 例：速率限制（session 內呼叫次數）
async def require_rate_limit(call: ToolCall) -> bool:
    count = await redis.incr(f"rate:{call.session_id}:{call.tool_name}")
    return count <= 10
    # 同一 session 超過 10 次 → ABORTED
```

**非常規失敗 — 例外被捕捉，視為 False：**
```python
async def require_db_ready(call: ToolCall) -> bool:
    conn = await db.connect()   # 連線失敗拋 ConnectionError
    return await conn.ping()    # → ConnectionError 被捕捉
                                # → passed = False → ABORTED
                                # → log.warning 會記錄例外
```

**隱性陷阱 1 — 回傳 None（忘記 return True）：**
```python
async def bad_check(call: ToolCall) -> bool:
    if not some_condition:
        return False
    # 隱性地回傳 None ← Python 函數沒有 return 預設為 None
    # None 是 falsy → if not passed → True → ABORTED
    # 靜默失敗，沒有任何例外提示
```

**隱性陷阱 2 — 回傳 coroutine 物件（忘記 await）：**
```python
async def bad_check(call: ToolCall) -> bool:
    return some_async_fn(call)  # 沒有 await！
    # some_async_fn(call) 是一個 coroutine 物件
    # coroutine 物件是 truthy → if not passed → False → 靜默通過
    # ⚠️ 這個 check 實際上什麼都沒有檢查
```

**短路行為：**
```
precondition_checks = [check_A, check_B, check_C]
check_A → True
check_B → False → ABORTED，check_C 永遠不執行
```

---

### EXECUTING

**時機：** `LifecycleGateMiddleware` 呼叫 `next(call)`（工具 handler）的精確瞬間。

**有 abort_signal，且信號已設定（執行前中止）：**
```
abort_signal.is_set() = True
→ 立即 ABORTED，handler 永遠不被呼叫
→ MEMORIALIZED（保證）
```

**有 abort_signal，信號在執行期間觸發（asyncio.wait racing）：**
```python
exec_task = asyncio.create_task(next(call))   # 工具 handler
abort_task = asyncio.create_task(signal.wait())

done, pending = await asyncio.wait(
    {exec_task, abort_task},
    return_when=FIRST_COMPLETED,
)

if exec_task in done:    → 工具先完成 → 走正常路徑
else:                    → 中止信號先觸發
    exec_task.cancel()   → handler 被取消
    → ABORTED → MEMORIALIZED
```

**具體場景：**
```
用戶在 run_bash 執行 pip install（耗時 30 秒）的過程中按 Escape
→ abort_signal.set()
→ asyncio.wait() 的 abort_task 完成
→ exec_task.cancel() → run_bash subprocess 被終止
→ ABORTED → MEMORIALIZED（完整記錄）
```

**工具 handler 拋出例外（v0.2.8.0 新增保護）：**
```python
try:
    result = await next(call)
except Exception as exc:
    # handler 拋出例外而不是回傳 ToolResult
    log.warning("tool %r raised: %s", call.tool_name, exc)
    result = ToolResult(success=False, error=str(exc), failure_type="execution_error")
    # 繼續走正常路徑 → OBSERVED → COMMITTED → MEMORIALIZED
    # 不讓例外逃逸框架
```

---

### OBSERVED

**時機：** 工具 handler 回傳結果的瞬間。

**特殊情況 — timeout result 不觸發 OBSERVED：**
```
handler 回傳 ToolResult(failure_type="timeout")
→ LifecycleGateMiddleware 留在 EXECUTING
→ 不轉 OBSERVED
→ 等外層 LifecycleMiddleware 看到 "timeout" → 驅動 EXECUTING → TIMED_OUT
```

這個設計讓 timeout 可以被識別為 TIMED_OUT 而不是 COMMITTED。

---

### VALIDATED / COMMITTED / REVERTING / REVERTED

**時機：** 工具執行完成後，外層 `LifecycleMiddleware` 執行 post_validator。

**無 post_validator（大多數工具）：**
```
OBSERVED → COMMITTED → MEMORIALIZED（跳過 VALIDATED）
```

**有 post_validator，驗證通過：**
```python
async def verify_file_written(call: ToolCall, result: ToolResult) -> bool:
    path = call.args["path"]
    return Path(path).exists() and Path(path).stat().st_size > 0

# 驗證通過
OBSERVED → VALIDATED → COMMITTED → MEMORIALIZED
```

**有 post_validator，驗證失敗，無 rollback_fn：**
```
驗證失敗但沒有辦法撤銷 → 只能接受現狀
OBSERVED → VALIDATED → COMMITTED → MEMORIALIZED
（result 標記失敗，但狀態仍走到 COMMITTED）
```

**有 post_validator，驗證失敗，有 rollback_fn：**
```python
async def rollback_deployment(call: ToolCall, result: ToolResult) -> ToolResult:
    await k8s.rollback(deployment=call.args["name"])
    return ToolResult(success=True, output="rolled back")

# 驗證失敗 + rollback
OBSERVED → VALIDATED → REVERTING → REVERTED → MEMORIALIZED
回傳給 LLM 的 result.metadata["rolled_back"] = True
```

---

### MEMORIALIZED

**時機：** 所有路徑的最終狀態，無一例外。

**保證：** 無論工具成功、失敗、被中止、超時、或拋出例外，都會觸發 `on_lifecycle` callback，由 `TraceMiddleware` 寫入 EpisodicMemory。

**v0.2.8.0 修正前的問題：**
- 若工具 handler 拋出例外（而不是回傳 ToolResult），例外會逃逸 LifecycleGateMiddleware
- MEMORIALIZED 不會觸發
- ActionRecord 永遠停在 EXECUTING 狀態，memory 中沒有記錄

**v0.2.8.0 修正後：** 兩條執行路徑都有 try/except，例外被轉為 failed ToolResult，MEMORIALIZED 保證觸發。

---

## Pipeline 中的完整順序

```
LifecycleMiddleware.process() 開始
  │
  ├─ 建立 ActionRecord（DECLARED）
  ├─ 建立 LifecycleContext，注入 call.metadata
  │
  ▼ await next(call) → 進入內層 pipeline
  │
  │  TraceMiddleware（計時開始）
  │    SchemaValidationMiddleware（參數驗證）
  │      BlastRadiusMiddleware
  │        ├─ 決定授權 → 寫入 ctx.authorization_result
  │        │
  │        ▼ await next(call) → LifecycleGateMiddleware
  │        │
  │        │  [AUTHORIZED] ← 讀取 ctx.authorization_result
  │        │  [PREPARED]   ← 執行 precondition_checks[]
  │        │  [EXECUTING]  ← 精確時刻，abort signal racing
  │        │  → await next(call) → 工具 handler
  │        │  ← handler 回傳
  │        │  [OBSERVED]   ← 結果已知
  │        │
  │      ← LifecycleGateMiddleware 回傳
  │    ← BlastRadiusMiddleware 回傳
  │  ← SchemaValidationMiddleware 回傳
  │  ← TraceMiddleware（計時結束，寫 episodic）
  │
  ▼ 回到 LifecycleMiddleware.process()
  │
  ├─ record.is_terminal? → 是（ABORTED / DENIED） → return
  ├─ 處理 permission_denied / tool_not_found / validation_error / timeout
  ├─ [VALIDATED] → post_validator
  ├─ [COMMITTED] 或 [REVERTING] → [REVERTED]
  └─ [MEMORIALIZED] ← 保證觸發
```

---

## 與 Issue #64 的關係

目前 `precondition_checks` 只能由工具定義的開發者設定（`ToolDefinition` 層）。技能（`SkillGenome`）無法聲明自己的執行前置條件。

Issue #64 探索讓技能透過 `SKILL.md` 聲明 `precondition_check_refs`，在 `load_skill()` 時動態掛載到對應工具，使技能從「指令集」升級成「有條件的行動承諾」。

詳見：[GitHub Issue #64](https://github.com/Dakai666/Loom/issues/64)
