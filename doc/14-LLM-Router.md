# LLM Router（更新版）

> 依據實際 `loom/core/cognition/router.py` 重寫。

---

## ⚠️ 與舊版文件的差異

舊版文件描述的是**不存在的架構**（Intent-based routing、`RouterConfig`、`route_with_tools()`）。實作中：

- **無 Intent 分類**。Router 只做前綴匹配，無意圖分析。
- **無 RouterConfig dataclass**。路由表是純 Python 常數。
- **無 `route()` 方法**。主要方法是 `get_provider()` + `chat()` / `stream_chat()`。
- **無 tool calling 特殊路由**。Tool calling 是 provider 自己的能力。

---

## 實際路由表

```python
_ROUTING: list[tuple[str, str]] = [
    ("MiniMax-",   "minimax"),    # MiniMax-M2.7, MiniMax-Sirius 等
    ("minimax-",   "minimax"),    # 小寫前綴
    ("claude-",    "anthropic"),  # Anthropic claude-3.5-sonnet 等
    ("gpt-",       "openai"),     # GPT-4o 等
    ("ollama/",    "ollama"),     # 本地 Ollama 模型
    ("lmstudio/",  "lmstudio"),   # 本地 LM Studio 模型
]
```

**沒有匹配的 model 名稱** → 使用 `default_model`（從 `loom.toml` 讀取，預設 `MiniMax-M2.7`）。

---

## 核心 API

### LLMRouter

```python
class LLMRouter:
    def register(self, provider: LLMProvider, default: bool = False) -> "LLMRouter":
        """註冊 provider，支援鏈式呼叫"""
        ...

    def get_provider(self, model: str) -> LLMProvider:
        """根據 model 名稱前綴找到對應的 provider"""
        for prefix, provider_name in self._ROUTING:
            if model.startswith(prefix):
                return self._providers[provider_name]
        return self._providers[self._default]

    async def chat(self, model, messages, tools=None, max_tokens=8096) -> LLMResponse:
        """非 streaming 呼叫"""
        provider = self.get_provider(model)
        return await provider.chat(messages=messages, tools=tools, max_tokens=max_tokens)

    async def stream_chat(self, model, messages, tools=None, max_tokens=8096, *, abort_signal=None):
        """Streaming 呼叫，回傳 AsyncIterator"""
        provider = self.get_provider(model)
        async for item in provider.stream_chat(...):
            yield item

    def switch_model(self, model: str) -> bool:
        """動態切換 provider 的 model"""
        # 找到 prefix 對應的 provider，更新其 model 屬性
        # 成功 → True，prefix 不認識 → False
```

### 路由流程圖

```
LLMRouter.chat(model="ollama/llama3.2", messages=[...])
    │
    ├─ get_provider("ollama/llama3.2")
    │     ├─ "ollama/" 前綴匹配 → provider_name="ollama"
    │     └─ return self._providers["ollama"]
    │
    └─ provider.chat(messages=[...])
          └─ ollama_client.chat(model="llama3.2", ...)
```

---

## 初始化（在 LoomSession.start() 內）

```python
from loom.core.cognition.providers import (
    MiniMaxProvider, AnthropicProvider, OllamaProvider, LMStudioProvider,
)

router = LLMRouter()
router.register(MiniMaxProvider(api_key=..., model="MiniMax-M2.7"), default=True)
router.register(AnthropicProvider(api_key=...))
router.register(OllamaProvider(base_url="http://localhost:11434"))
```

---

## switch_model() — 動態模型切換

```python
# 使用者要求切換到 claude
result = router.switch_model("claude-3.5-sonnet")
# → 找到 "claude-" prefix → 更新 anthropic provider 的 model 屬性
# → return True

# 無效前綴
result = router.switch_model("unknown-model")
# → 沒有匹配 → return False
```

---

## 與舊文件的橋接

| 舊版描述 | 實作差異 |
|---------|---------|
| Intent 分類前綴（`/reasoning` 等）| 不存在，Router 只做字首匹配 |
| `RouterConfig` dataclass | 不存在 |
| `route_with_tools()` | 不存在，tool calling 是各 provider 的 native 能力 |
| `route_with_tracking()` | 不存在，token 追蹤由 `ContextBudget` 單獨處理 |
| Fallback 策略 | 不存在，各 provider 自己處理錯誤 |
| 模型降級策略 | 不存在 |

loom.toml 中的 provider 配置說明見 [37-loom-toml-參考.md](37-loom-toml-參考.md)。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*