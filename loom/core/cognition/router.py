"""
LLM Router — single entry point for all model interactions.

Selects the correct provider based on model name prefix,
then delegates to that provider's chat() method.

Model routing rules
-------------------
"MiniMax-*"        → MiniMaxProvider
"claude-*"         → AnthropicProvider
(default)          → first registered provider

Usage
-----
    router = LLMRouter()
    router.register(MiniMaxProvider(api_key="...", model="MiniMax-M2.7"))
    router.register(AnthropicProvider(api_key="..."))

    response = await router.chat(
        model="MiniMax-M2.7",
        messages=[...],
        tools=[...],
    )
"""

from collections.abc import AsyncIterator

from .providers import LLMProvider, LLMResponse


class LLMRouter:
    """Routes chat requests to the correct provider by model name."""

    # Model-name prefix → provider name
    _ROUTING: list[tuple[str, str]] = [
        ("MiniMax-", "minimax"),
        ("minimax-", "minimax"),
        ("claude-",  "anthropic"),
        ("gpt-",     "openai"),
    ]

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._default: str | None = None

    def register(self, provider: LLMProvider, default: bool = False) -> "LLMRouter":
        self._providers[provider.name] = provider
        if default or self._default is None:
            self._default = provider.name
        return self

    def get_provider(self, model: str) -> LLMProvider:
        for prefix, provider_name in self._ROUTING:
            if model.startswith(prefix):
                p = self._providers.get(provider_name)
                if p:
                    return p
        # Fall back to default
        if self._default:
            return self._providers[self._default]
        raise RuntimeError(
            f"No provider registered for model '{model}'. "
            f"Registered providers: {list(self._providers)}"
        )

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        provider = self.get_provider(model)
        return await provider.chat(messages=messages, tools=tools, max_tokens=max_tokens)

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        """Yield ``(chunk, None)`` text fragments then ``("", LLMResponse)``."""
        provider = self.get_provider(model)
        async for item in provider.stream_chat(
            messages=messages, tools=tools, max_tokens=max_tokens
        ):
            yield item

    def format_tool_result(
        self,
        model: str,
        tool_use_id: str,
        content: str,
        success: bool = True,
    ) -> dict:
        provider = self.get_provider(model)
        return provider.format_tool_result(tool_use_id, content, success)

    @property
    def providers(self) -> list[str]:
        return list(self._providers)
