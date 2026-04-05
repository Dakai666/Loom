# Lens 系統

Lens 是工具的「包裝器」。它位於工具和 Harness Layer 之間，用於在工具執行前後添加額外功能。

---

## 為什麼需要 Lens？

傳統的 Middleware 只在框架層面運作，無法針對特定工具進行客製化。Lens 填補了這個空白：

```
┌─────────────────────────────────────────────────────────────┐
│                      工具呼叫流程                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Agent 呼叫工具                                            │
│         │                                                   │
│         ▼                                                   │
│   ┌─────────────┐                                          │
│   │   Harness   │  ← Middleware（Log、Trace、BlastRadius） │
│   └──────┬──────┘                                          │
│          │                                                  │
│          ▼                                                  │
│   ┌─────────────┐                                          │
│   │    Lens    │  ← 工具專屬包裝                           │
│   └──────┬──────┘                                          │
│          │                                                  │
│          ▼                                                  │
│   ┌─────────────┐                                          │
│   │   Tool     │  ← 實際工具執行                           │
│   └─────────────┘                                          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Lens 結構

```python
# loom/core/harness/lens.py
class BaseLens(ABC):
    """Lens 抽象基類"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Lens 名稱"""
        pass
    
    @property
    def target_tools(self) -> list[str] | None:
        """
        目標工具列表。
        如果為 None，則應用於所有工具。
        """
        return None
    
    async def before_call(
        self,
        tool: Tool,
        args: dict,
        context: dict,
    ) -> dict:
        """
        工具呼叫前鉤子
        
        Args:
            tool: 目標工具
            args: 工具參數
            context: 執行上下文
        
        Returns:
            可能修改後的 args
        """
        return args
    
    async def after_call(
        self,
        tool: Tool,
        args: dict,
        result: Any,
        error: Exception | None,
        context: dict,
    ) -> Any:
        """
        工具呼叫後鉤子
        
        Args:
            tool: 目標工具
            args: 工具參數
            result: 執行結果
            error: 執行錯誤（如果有的話）
            context: 執行上下文
        
        Returns:
            可能修改後的 result
        """
        return result
```

---

## 內建 Lens

### HermesLens

日誌和追蹤 Lens。

```python
# loom/core/harness/lenses/hermes.py
class HermesLens(BaseLens):
    """Hermes Lens：日誌和追蹤"""
    
    def __init__(
        self,
        log_level: str = "INFO",
        trace_calls: bool = True,
    ):
        self.name = "hermes"
        self.log_level = log_level
        self.trace_calls = trace_calls
    
    @property
    def target_tools(self) -> list[str] | None:
        return None  # 應用於所有工具
    
    async def before_call(
        self,
        tool: Tool,
        args: dict,
        context: dict,
    ) -> dict:
        """記錄呼叫開始"""
        
        call_id = context.get("call_id", generate_id())
        context["call_id"] = call_id
        
        logger.log(
            self.log_level,
            f"Tool call started: {tool.name} (id={call_id})",
            extra={"args": args, "call_id": call_id}
        )
        
        # 記錄開始時間
        context["_lens_start_time"] = datetime.now()
        
        return args
    
    async def after_call(
        self,
        tool: Tool,
        args: dict,
        result: Any,
        error: Exception | None,
        context: dict,
    ) -> Any:
        """記錄呼叫結束"""
        
        call_id = context.get("call_id")
        duration = (datetime.now() - context.get("_lens_start_time")).total_seconds()
        
        if error:
            logger.error(
                f"Tool call failed: {tool.name} (id={call_id}) after {duration:.3f}s",
                extra={"error": str(error), "call_id": call_id}
            )
        else:
            logger.log(
                self.log_level,
                f"Tool call completed: {tool.name} (id={call_id}) in {duration:.3f}s",
                extra={"call_id": call_id}
            )
        
        return result
```

### OpenAIToolsLens

為 OpenAI 格式的工具調用添加參數驗證。

```python
# loom/core/harness/lenses/openai_tools.py
class OpenAIToolsLens(BaseLens):
    """OpenAI Tools Lens：參數驗證和轉換"""
    
    def __init__(
        self,
        strict_validation: bool = True,
        auto_coerce_types: bool = True,
    ):
        self.name = "openai_tools"
        self.strict_validation = strict_validation
        self.auto_coerce_types = auto_coerce_types
    
    @property
    def target_tools(self) -> list[str] | None:
        return None  # 應用於所有工具
    
    async def before_call(
        self,
        tool: Tool,
        args: dict,
        context: dict,
    ) -> dict:
        """驗證和轉換參數"""
        
        # 獲取工具的 schema
        schema = tool.get_openai_schema()
        
        # 驗證必填參數
        required = schema.get("parameters", {}).get("required", [])
        for param in required:
            if param not in args:
                if self.strict_validation:
                    raise ToolArgumentError(
                        f"Missing required parameter: {param}",
                        tool=tool.name,
                        parameter=param,
                    )
        
        # 類型轉換
        if self.auto_coerce_types:
            args = self._coerce_types(args, schema)
        
        return args
    
    def _coerce_types(self, args: dict, schema: dict) -> dict:
        """自動轉換參數類型"""
        
        properties = schema.get("parameters", {}).get("properties", {})
        coerced = args.copy()
        
        for param, value in args.items():
            if param in properties:
                expected_type = properties[param].get("type")
                coerced[param] = self._coerce_value(value, expected_type)
        
        return coerced
    
    def _coerce_value(self, value: Any, expected_type: str) -> Any:
        """轉換單個值"""
        
        if expected_type == "integer" and isinstance(value, float):
            return int(value)
        elif expected_type == "number" and isinstance(value, str):
            return float(value)
        elif expected_type == "boolean" and isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        
        return value
```

---

## 自訂 Lens

### 創建 CacheLens

```python
# 自訂 Lens：結果快取
class CacheLens(BaseLens):
    """Cache Lens：工具結果快取"""
    
    def __init__(
        self,
        cache_store: CacheStore,
        ttl: float = 300.0,  # 5 分鐘
        key_generator: Callable[[Tool, dict], str] | None = None,
    ):
        self.name = "cache"
        self.cache = cache_store
        self.ttl = ttl
        self.key_generator = key_generator or self._default_key_generator
    
    @property
    def target_tools(self) -> list[str] | None:
        # 只快取讀取類工具
        return ["read_file", "fetch_url", "web_search"]
    
    async def before_call(
        self,
        tool: Tool,
        args: dict,
        context: dict,
    ) -> dict:
        """檢查快取"""
        
        cache_key = self.key_generator(tool, args)
        
        cached = await self.cache.get(cache_key)
        if cached:
            # 直接返回快取的結果，跳過工具執行
            context["_cache_hit"] = True
            context["_cached_result"] = cached
        
        return args
    
    async def after_call(
        self,
        tool: Tool,
        args: dict,
        result: Any,
        error: Exception | None,
        context: dict,
    ) -> Any:
        """寫入快取"""
        
        # 只有成功且未命中快取才寫入
        if error or context.get("_cache_hit"):
            return result
        
        cache_key = self.key_generator(tool, args)
        await self.cache.set(cache_key, result, ttl=self.ttl)
        
        return result
    
    def _default_key_generator(self, tool: Tool, args: dict) -> str:
        """預設的快取 key 生成器"""
        args_str = json.dumps(args, sort_keys=True)
        return f"tool_cache:{tool.name}:{hash(args_str)}"
```

### 創建 RateLimitLens

```python
# 自訂 Lens：速率限制
class RateLimitLens(BaseLens):
    """Rate Limit Lens：工具呼叫速率限制"""
    
    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
    ):
        self.name = "rate_limit"
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: deque[datetime] = deque()
    
    @property
    def target_tools(self) -> list[str] | None:
        return None  # 應用於所有工具
    
    async def before_call(
        self,
        tool: Tool,
        args: dict,
        context: dict,
    ) -> dict:
        """檢查速率限制"""
        
        now = datetime.now()
        
        # 清理過期的記錄
        cutoff = now - timedelta(seconds=self.window)
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        
        # 檢查是否超限
        if len(self._calls) >= self.max_calls:
            oldest = self._calls[0]
            wait_time = (oldest + timedelta(seconds=self.window) - now).total_seconds()
            
            raise RateLimitError(
                f"Rate limit exceeded for {tool.name}. "
                f"Retry in {wait_time:.1f}s.",
                tool=tool.name,
                retry_after=wait_time,
            )
        
        # 記錄這次呼叫
        self._calls.append(now)
        
        return args
```

---

## Lens 註冊與使用

### loom.toml 配置

```toml
[harness.lenses]

# 內建 Lens
[harness.lenses.hermes]
enabled = true
log_level = "INFO"
trace_calls = true

[harness.lenses.openai_tools]
enabled = true
strict_validation = true
auto_coerce_types = true

# 自訂 Lens
[harness.lenses.cache]
enabled = true
ttl = 300

[harness.lenses.rate_limit]
enabled = false
max_calls = 10
window_seconds = 60
```

### 程式化配置

```python
# 創建 Lens
hermes = HermesLens(log_level="DEBUG")
cache = CacheLens(cache_store=redis_cache, ttl=600)

# 註冊到 Tool
tool.register_lens(hermes)
tool.register_lens(cache)

# 或在 Harness 層面配置
harness.add_global_lens(hermes)
harness.add_tool_lens("web_search", cache)
```

---

## Lens 順序

Lens 按順序執行：

```python
# 執行順序
lens_order = [
    HermesLens.before_call,     # 1. 日誌記錄
    RateLimitLens.before_call,   # 2. 速率限制
    OpenAIToolsLens.before_call, # 3. 參數驗證
    CacheLens.before_call,       # 4. 檢查快取
    # ... 工具執行 ...
    CacheLens.after_call,        # 4. 寫入快取
    OpenAIToolsLens.after_call,  # 3. 結果處理
    RateLimitLens.after_call,    # 2. 速率限制後處理
    HermesLens.after_call,       # 1. 日誌記錄
]
```

---

## 總結

Lens 是工具的「包裝器」，提供細粒度的工具增強：

| Lens | 功能 |
|------|------|
| HermesLens | 日誌追蹤 |
| OpenAIToolsLens | 參數驗證 |
| CacheLens | 結果快取 |
| RateLimitLens | 速率限制 |

Lens 與 Middleware 的區別：

| | Middleware | Lens |
|---|-----------|------|
| 作用範圍 | 所有工具 | 特定工具 |
| 執行順序 | 固定 | 可配置 |
| 用途 | 框架功能 | 工具專屬功能 |
