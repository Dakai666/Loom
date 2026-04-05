# Middleware 詳解

Loom 的 Middleware 系統受到 Web 框架中介軟體的啟發，但專為 LLM Agent 的 tool call 生命週期設計。

---

## 核心資料結構

### ToolCall

工具呼叫的請求封裝，於進入 Pipeline 前創建：

```python
@dataclass
class ToolCall:
    tool_name: str
    args: dict[str, Any]
    trust_level: TrustLevel
    session_id: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
```

### ToolResult

工具執行的結果封裝，所有工具執行路徑（成功或失敗）皆透過此結構回傳，**不拋出异常**：

```python
@dataclass
class ToolResult:
    call_id: str                        # 對應的 ToolCall.id
    tool_name: str                      # 工具名稱
    success: bool                       # 執行是否成功
    output: Any = None                  # 正常輸出
    error: str | None = None            # 失敗時的錯誤訊息
    failure_type: str | None = None    # 失敗類型（見下方枚舉）
    duration_ms: float = 0.0            # 執行耗時（毫秒）
    metadata: dict[str, Any] = field(default_factory=dict)
```

### FAILURE_TYPES 枚舉

當 `success=False` 時，`failure_type` 為以下值之一：

| 值 | 意義 |
|----|------|
| `tool_not_found` | 工具名稱不在 registry 中 |
| `permission_denied` | 信任等級不足或用戶拒絕 |
| `timeout` | 執行超過時間限制 |
| `execution_error` | 工具執行期拋出异常 |
| `validation_error` | 參數校驗失敗 |
| `model_error` | LLM API 呼叫失敗 |

---

## 核心抽象

每個 Middleware 實現一個 `process` 方法：

```python
class Middleware(ABC):
    name: str  # 識別名稱，用於日誌

    @abstractmethod
    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        """
        攔截工具呼叫。
        - call: 當前工具呼叫請求
        - next: 鏈中下一個處理者（另一個 Middleware 或最終的 tool handler）
        呼叫 await next(call) 繼續往下傳遞。
        """
        ...
```

`ToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]`

---

## MiddlewarePipeline

Pipeline 是 Middleware 的容器與執行器：

```python
pipeline = MiddlewarePipeline([
    LogMiddleware(console),
    TraceMiddleware(on_trace=episodic_memory.append),
    BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=confirm),
])
```

### 執行流程

Pipeline 使用遞迴鏈構建（ outermost → innermost ）：

```
ToolCall 請求
    ↓
LogMiddleware.process() → TraceMiddleware.process() → BlastRadiusMiddleware.process()
                                                                                  ↓
                                                                     工具實際執行
                                                                                  ↓
                                                              ToolResult 回傳
                                                                                  ↓
LogMiddleware.process() ← TraceMiddleware.process() ← BlastRadiusMiddleware.process()
```

---

## LogMiddleware

### 職責

將每次工具呼叫以 Rich 格式化輸出到終端。

### 輸出內容

```python
# Tool 開始
~> tool read_file SAFE

# Tool 結果
ok read_file 12ms
```

### 建構函數參數

```python
LogMiddleware(
    console: Console,          # Rich Console 實例
)
```

---

## TraceMiddleware

### 職責

- 計時（記錄工具執行耗時，寫入 `result.duration_ms`）
- 觸發 `_on_trace` callback（用於連接記憶層）

### 建構函數參數

```python
TraceMiddleware(
    on_trace: Callable[[ToolCall, ToolResult], Awaitable[None]] | None = None,
)
```

> `on_trace` 是連接 Harness Layer 與 Memory Layer 的橋樑——每次工具結果自動被記錄，工具作者無需關心記憶邏輯。

---

## BlastRadiusMiddleware

### 職責

依 Trust Level 決定工具是否需要人類確認。實作使用 `PermissionContext` 抽象授權狀態：

```python
class BlastRadiusMiddleware:
    def __init__(
        self,
        perm_ctx: Any,                          # PermissionContext 實例
        confirm_fn: Callable[[ToolCall], Awaitable[bool]],  # 確認提示回調
    ):
        self._perm = perm_ctx
        self._confirm = confirm_fn

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        # 已獲授權（SAFE 等級或先前已確認）→ 直接放行
        if self._perm.is_authorized(call.tool_name, call.trust_level):
            return await next(call)

        # 請求用戶確認
        allowed = await self._confirm(call)
        if not allowed:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="User denied tool execution.",
                failure_type="permission_denied",
            )

        # GUARDED 等級： session 內後續呼叫無需再確認
        if call.trust_level == TrustLevel.GUARDED:
            self._perm.authorize(call.tool_name)

        return await next(call)
```

### Trust Level 決策表

| Trust Level | 行為 |
|-------------|------|
| SAFE | 直接放行（`is_authorized` 回傳 True）|
| GUARDED | 未授權時請求確認，確認後 session 內同一工具免確認 |
| CRITICAL | 每次都請求確認，無 session 免確認捷徑 |

### 確認訊息格式（平台無關）

```python
# confirm_fn 接收 ToolCall，回傳 True/False
# 平台的 UI 層負責實際的提示格式（CLI / Telegram / Discord 等）
```

---

## 自定義 Middleware 範例

### RateLimitMiddleware（頻率限制）

```python
class RateLimitMiddleware(Middleware):
    name = "rate_limit"

    def __init__(self, max_calls_per_minute: int = 30):
        self.max_calls = max_calls_per_minute
        self.calls: deque[datetime] = deque()

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        now = datetime.now()
        self.calls.append(now)

        # 清除 1 分鐘前的記錄
        while self.calls and (now - self.calls[0]).seconds > 60:
            self.calls.popleft()

        if len(self.calls) > self.max_calls:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="Rate limit exceeded.",
                failure_type="tool_not_found",  # 借用類型，非精確
            )

        return await next(call)
```

### RetryMiddleware（自動重試）

```python
class RetryMiddleware(Middleware):
    name = "retry"

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        for attempt in range(self.max_retries):
            result = await next(call)
            if result.success:
                return result
            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 指數退避
        return result  # 最後一次結果
```

---

## Middleware 與 Plugin 的結合

Plugin 可以透過 `install_into(session)` 注入額外的 Middleware：

```python
class MyPlugin(LoomPlugin):
    def middleware(self) -> list[Middleware]:
        return [RateLimitMiddleware(), RetryMiddleware()]

# 在 PluginRegistry.install_into() 中：
# plugin.middleware() → session.middleware_pipeline.use()
```
