# LLM Router

LLM Router 是 Loom 的模型選擇引擎。它根據請求的「意圖」自動將請求路由到最適合的模型，既保證回應品質，又控制成本。

---

## 核心設計：前綴路由

### 為什麼用前綴？

傳統的做法是讓 LLM 自己判斷「這個問題該用什麼模型」。缺點：
- 不準確（模型不了解自己的優缺點）
- 增加 token 浪費
- 延遲增加

Loom 採用**前綴路由**：每個用戶訊息在進入處理管道前，會被分析並加上意圖前綴。

### 內建前綴

| 前綴 | 用途 | 預設模型 |
|------|------|----------|
| `/general` | 日常對話、問答 | MiniMax-M2.7 |
| `/reasoning` | 複雜推理、規劃、分析 | o4-mini |
| `/tools` | 需要工具調用的請求 | GPT-4o |
| `/creative` | 創意寫作、翻譯 | MiniMax-M2.7 |
| `/code` | 代碼生成、解釋 | o4-mini |

### 前綴偵測邏輯

```python
# loom/core/cognition/router.py
class IntentClassifier:
    """簡單的關鍵詞/語境分類器"""
    
    PATTERNS = {
        "/reasoning": ["分析", "解釋", "為什麼", "如何實現", "規劃", "設計", "評估"],
        "/tools": ["查詢", "搜尋", "讀取", "寫入", "執行", "計算"],
        "/creative": ["翻譯", "寫作", "創作", "編寫", "生成故事"],
        "/code": ["代碼", "函數", "class", "def ", "function", "bug"],
    }
    
    def classify(self, text: str) -> str:
        text_lower = text.lower()
        
        # 1. 檢查明確前綴
        for prefix in ["/general", "/reasoning", "/tools", "/creative", "/code"]:
            if text_lower.startswith(prefix):
                return prefix
        
        # 2. 關鍵詞匹配
        for prefix, keywords in self.PATTERNS.items():
            if any(kw in text_lower for kw in keywords):
                return prefix
        
        # 3. 預設
        return "/general"
```

---

## 多 Provider 支援

### Provider 抽象

```python
# loom/core/cognition/providers/base.py
class LLMProvider(ABC):
    """LLM Provider 抽象介面"""
    
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        **kwargs
    ) -> str:
        """生成回應"""
        pass
    
    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """計算 embedding（可選）"""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    def supports_tools(self) -> bool:
        return False
```

### MiniMax Provider

```python
# loom/core/cognition/providers/minimax.py
class MiniMaxProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.minimax.io/v1",
        )
        self.model = "MiniMax-M2.7"
    
    @property
    def name(self) -> str:
        return "minimax"
    
    async def complete(self, messages: list[dict], **kwargs) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content
```

### OpenAI Provider

```python
# loom/core/cognition/providers/openai.py
class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = "gpt-4o"
    
    @property
    def name(self) -> str:
        return "openai"
    
    @property
    def supports_tools(self) -> bool:
        return True  # OpenAI 原生支援 tool calling
    
    async def complete(self, messages: list[dict], **kwargs) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content
```

---

## Router 核心邏輯

### 配置

```python
# loom/core/cognition/router.py
@dataclass
class RouterConfig:
    """Router 設定"""
    
    # 前綴 → 模型映射
    prefix_routes: dict[str, str] = field(default_factory=lambda: {
        "/general": "minimax",
        "/reasoning": "reasoning",
        "/tools": "openai",
        "/creative": "minimax",
        "/code": "reasoning",
    })
    
    # 模型名稱 → provider 名稱
    model_providers: dict[str, str] = field(default_factory=lambda: {
        "minimax": "minimax",
        "openai": "openai",
        "reasoning": "openai",  # o4-mini 也是 OpenAI 的
        "gpt-4o": "openai",
        "MiniMax-M2.7": "minimax",
        "o4-mini": "openai",
    })
```

### 路由選擇

```python
# loom/core/cognition/router.py
class LLMRouter:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        config: RouterConfig,
    ):
        self.providers = providers
        self.config = config
    
    async def route(
        self,
        messages: list[dict],
        intent_prefix: str = "/general",
        **kwargs
    ) -> str:
        # 1. 根據 intent 前綴選擇模型
        provider_name = self._get_provider(intent_prefix)
        
        # 2. 取得 provider
        provider = self.providers.get(provider_name)
        if not provider:
            raise ValueError(f"Unknown provider: {provider_name}")
        
        # 3. 發送請求
        return await provider.complete(messages, **kwargs)
    
    def _get_provider(self, intent_prefix: str) -> str:
        # 從 prefix_routes 取得模型名稱
        model = self.config.prefix_routes.get(
            intent_prefix,
            "minimax"  # 預設
        )
        
        # 從 model_providers 取得 provider 名稱
        return self.config.model_providers.get(model, "minimax")
```

---

## 工具調用的特殊處理

### 為什麼工具調用需要特殊路由？

並非所有模型都支援 tool calling。MiniMax-M2.7 不支援，所以所有需要工具的請求都會路由到 GPT-4o。

### Forced Provider

對於需要明確 tool calling 的場景，Router 提供 `force_provider`：

```python
# loom/core/cognition/router.py
async def route_with_tools(
    self,
    messages: list[dict],
    tools: list[dict],  # OpenAI 格式的工具定義
    **kwargs
) -> str:
    """工具調用的專用路由（強制使用 OpenAI）"""
    
    # 工具調用必須用支援 tool calling 的 provider
    provider = self.providers.get("openai")
    if not provider:
        raise ValueError("OpenAI provider required for tool calling")
    
    return await provider.complete(
        messages,
        tools=tools,
        **kwargs
    )
```

---

## loom.toml 配置

```toml
[cognition.router]

# 預設模型
default_model = "MiniMax-M2.7"

# 前綴路由表
[rognition.router.routes]
"/general" = "minimax"
"/reasoning" = "openai:o4-mini"
"/tools" = "openai:gpt-4o"
"/creative" = "minimax"
"/code" = "openai:o4-mini"

# Provider 設定
[cognition.providers.minimax]
type = "minimax"
model = "MiniMax-M2.7"

[cognition.providers.openai]
type = "openai"
model = "gpt-4o"
```

---

## Token 計算

Router 在路由時會同時計算 token 數量，供 Context Budget 使用：

```python
# loom/core/cognition/router.py
async def route_with_tracking(
    self,
    messages: list[dict],
    budget: ContextBudget,
    **kwargs
) -> str:
    # 1. 計算 input tokens
    input_tokens = self._count_tokens(messages)
    
    # 2. 檢查預估輸出是否超出預算
    estimated_output = 500  # 預設估計值
    if not budget.can_fit(input_tokens + estimated_output):
        raise BudgetExceededError(
            f"Request too large: {input_tokens} tokens "
            f"(budget remaining: {budget.remaining})"
        )
    
    # 3. 執行路由
    response = await self.route(messages, **kwargs)
    
    # 4. 更新預算
    output_tokens = self._count_tokens(response)
    budget.consume(input_tokens + output_tokens)
    
    return response
```

---

## 錯誤處理與 Fallback

### Provider 失敗時的 Fallback

```python
async def route_with_fallback(
    self,
    messages: list[dict],
    intent_prefix: str = "/general",
    **kwargs
) -> str:
    # 嘗試主 provider
    try:
        return await self.route(messages, intent_prefix, **kwargs)
    except ProviderError as e:
        # Fallback 到 MiniMax（最便宜、最穩定）
        logger.warning(f"Primary provider failed: {e}, falling back to minimax")
        return await self.providers["minimax"].complete(messages, **kwargs)
```

### 模型降級策略

```python
# 當 /reasoning 模型失敗時，降級到 /general
FALLBACK_ROUTES = {
    "openai:o4-mini": "minimax",
    "openai:gpt-4o": "minimax",
}
```

---

## 總結

LLM Router 的設計哲學：**讓對的請求去對的模型**。

| 設計 | 優點 |
|------|------|
| 前綴路由 | 簡單、可預測、低延遲 |
| Provider 抽象 | 支援多 provider，無縫切換 |
| Forced Provider | tool calling 自動使用正確模型 |
| Fallback | 一個 provider 失敗時自動降級 |
