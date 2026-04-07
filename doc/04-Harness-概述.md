# Harness Layer 概述

> **「工具是公民，不是附屬品。」**

Harness Layer 是 Loom 的骨幹。所有工具的呼叫——無論來自 Agent、Task Engine、還是 Autonomy Engine——都必須經過 Harness 的 Middleware Pipeline。

---

## 設計動機

傳統 agent 框架將工具視為「功能」：定義函數 → 直接呼叫 → 完成。

Loom 的觀點不同：**工具是有生命週期的對象**，從「被定義」到「被執行」到「被審計」，每一個環節都需要被管理。

Middleware Pipeline 就是這個管理機制的實現。

---

## 與 Claude Code 的差異

Claude Code 只有兩個 hooks：
```
PreToolUse → 工具執行 → PostToolUse
```

Loom 改為全管道 Middleware Chain：
```
Middleware_1 → Middleware_2 → ... → Middleware_N → 實際工具執行
```

每個 Middleware 都是一個裝飾器，可以在工具執行前後附加任意邏輯，而且可以堆疊、組合、替換。

---

## Pipeline 流程

```
┌─────────────────────────────────────────────────────────────┐
│  Agent 發起工具呼叫                                          │
│  harness.execute(name, arguments, session_state)            │
└─────────────────────────────────────────────────────────────┘
                           ↓
              ┌─────────────────────────────┐
              │  MiddlewarePipeline.run()   │
              └─────────────────────────────┘
                           ↓
    ┌───────────────────┐
    │LifecycleMiddleware│  ← 最外層：DECLARED + post-OBSERVED + MEMORIALIZED
    └───────────────────┘
              ↓
    ┌──────────┐    ┌──────────┐
    │   Log    │ →  │  Trace   │  ← 計時 + EpisodicMemory 寫入
    │Middleware│    │Middleware│
    └──────────┘    └──────────┘
              ↓
    ┌──────────────────────────────────┐
    │  SchemaValidationMiddleware      │  ← JSON Schema 參數驗證
    └──────────────────────────────────┘
              ↓
    ┌──────────────────────────────────┐
    │  BlastRadiusMiddleware           │  ← Trust Level 判斷 + 用戶確認
    │                                  │    寫入 LifecycleContext.authorization_result
    │  SAFE     → 放行（已預授權）      │
    │  GUARDED  → 首次確認，session免  │
    │  CRITICAL → 每次都確認           │
    └──────────────────────────────────┘
              ↓
    ┌──────────────────────────────────┐
    │  LifecycleGateMiddleware         │  ← 最內層：即時控制閘門
    │                                  │
    │  AUTHORIZED ← ctx 授權結果      │
    │  PREPARED   ← precondition_checks│
    │  EXECUTING  ← 精確時刻 + abort   │
    │  OBSERVED   ← handler 回傳       │
    └──────────────────────────────────┘
              ↓
    ┌──────────────────────────────────┐
    │  工具 handler 實際執行           │
    └──────────────────────────────────┘
```

---

## 已實作的 Middleware

| Middleware | 位置 | 職責 |
|------------|------|------|
| `LifecycleMiddleware` | 最外層 | DECLARED + LifecycleContext 注入；DENIED / TIMED_OUT 處理；post_validator + rollback_fn；MEMORIALIZED 保證 |
| `LogMiddleware` | 外層 | Rich 格式化輸出每次工具調用與結果到終端 |
| `TraceMiddleware` | 外層 | 計時 + 每次 tool call/write 寫入 EpisodicMemory |
| `SchemaValidationMiddleware` | 中層 | 工具參數 JSON Schema 驗證；string→int/float/bool 安全強制轉換 |
| `BlastRadiusMiddleware` | 中層 | Trust Level 判斷 + 人類確認請求（含 `exec_auto` 模式）；寫入 `LifecycleContext.authorization_result` |
| `LifecycleGateMiddleware` | 最內層 | AUTHORIZED → PREPARED（precondition_checks）→ EXECUTING（abort racing）→ OBSERVED |

### SchemaValidationMiddleware（v0.2.5.2）

`SchemaValidationMiddleware` 在工具執行前，依據工具定義中的 JSON Schema 驗證參數：

```python
# loom/core/harness/validation.py
class SchemaValidationMiddleware(Middleware):
    """工具參數 JSON Schema 驗證"""

    name = "schema_validation"

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        tool_def = call.tool_def

        if not tool_def.parameters:
            return await next(call)

        # 安全類型強制轉換（string → int/float/bool）
        coerced = self._coerce(call.args, tool_def.parameters)

        # JSON Schema 驗證
        errors = validate(coerced, tool_def.parameters)
        if errors:
            return ToolResult(
                success=False,
                error=f"Schema validation failed: {errors}",
            )

        call.args = coerced
        return await next(call)
```

驗證失敗時，工具**不會執行**，直接返回錯誤。這能防止 LLM 幻觉参数（hallucinated parameters）在送達工具前就被攔截。

---

## AbortController 基礎設施（v0.2.5.1）

`loom/core/infra/` 提供標準的跨任務取消訊號，用於整個 async pipeline：

```python
# loom/core/infra/abort.py
class AbortController:
    """包裝 asyncio.Event，支援跨任務取消"""
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True
        self._event.set()

    async def wait_aborted(self) -> None:
        """等待 abort 信號；收到 CancelledError 時正常傳播，不會被吞掉"""
        await self._event.wait()

    def bind(self):
        """返回綁定方法，__closure__=None，避免閉包捕獲記憶體洩漏"""
        return self._event.wait

# 非 Loom 原生，用於 httpx tool calls
def wait_aborted(controller: AbortController):
    return controller._event.wait()
```

> Issue #16：目前 Step 1（infra）已完成；Step 2–3（接入 `stream_turn()` 和 httpx tool calls）在追蹤中。

---

## 與其他層的關係

```
Memory Layer ──提供──▶ TraceMiddleware（寫入 Episodic）
Cognition Layer ──提供──▶ 工具結果消費（Reflection API）
Autonomy Engine ──依賴──▶ Harness.execute()（執行決策）
Task Engine ──依賴──▶ Harness.execute()（並行工具執行）
Platform CLI ──依賴──▶ ToolRegistry（輸出可用工具列表）
```

---

## Trust Level 是什麼？

每一個工具都有一個 Trust Level，決定它需要多少人類審批：

| 等級 | 定義 | 行為 |
|------|------|------|
| **SAFE** | 唯讀、本地、可逆 | 自動執行，session 開始時預授權 |
| **GUARDED** | 寫入、網路、有副作用 | 首次需確認，session 內授權後免再詢問；`exec_auto=true` 時支援白名單免確認 |
| **CRITICAL** | 破壞性、跨系統、不可逆 | 每次強制確認，不可 session 授權 |

`BlastRadiusMiddleware` 在 `exec_auto` 模式（v0.2.5.1）下，攜帶 `EXEC` capability 且工作區範圍內的工具跳過逐次確認。

詳細說明：請參閱 [05-Trust-Level.md](05-Trust-Level.md)

---

## Middleware 擴展

Loom 的 Middleware 是可堆疊的。你可以在 Plugin 中實作新的 Middleware 並透過 `PluginRegistry.install_into()` 注入。

```python
class MyMiddleware(Middleware):
    name = "my_middleware"

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        # 工具執行前的邏輯（例如：計時、驗證）
        result = await next(call)
        # 工具執行後的邏輯（例如：日誌、後處理）
        return result
```

所有 Middleware 只需實現一個方法：`process(call, next)`。`next` 是鏈中下一個 handler（另一個 Middleware 或最終的工具執行器）。呼叫 `await next(call)` 將控制權向下傳遞，並取得 `ToolResult` 後再執行清理邏輯。

---

## 什麼不該在 Harness 做

- **不應**在 Middleware 內直接執行耗時 I/O（如大量 DB 寫入）——交給 async task
- **不應**在 Middleware 內改變工具參數——在 `await next(call)` 之前修飾 call
- **不應**在 Middleware 內存取其他工具的內部狀態——每個 Middleware 應該無狀態或只讀 session state
