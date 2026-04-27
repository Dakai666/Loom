"""
LLM Router — single entry point for all model interactions.

Selects the correct provider based on model name prefix,
then delegates to that provider's chat() method.

Model routing rules
-------------------
"MiniMax-*"    → MiniMaxProvider
"minimax-*"    → MiniMaxProvider
"claude-*"     → AnthropicProvider
"ollama/*"     → OllamaProvider    (local Ollama server)
"lmstudio/*"   → LMStudioProvider  (local LM Studio server)
(default)      → first registered provider

Usage
-----
    router = LLMRouter()
    router.register(MiniMaxProvider(api_key="...", model="MiniMax-M2.7"))
    router.register(AnthropicProvider(api_key="..."))
    router.register(OllamaProvider())   # no key needed

    response = await router.chat(
        model="ollama/llama3.2",
        messages=[...],
        tools=[...],
    )
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from .providers import LLMProvider, LLMResponse


def _load_loom_config() -> dict:
    """Load loom.toml from cwd or the package root; return {} on miss."""
    candidates = [
        Path.cwd() / "loom.toml",
        Path(__file__).parents[3] / "loom.toml",
    ]
    for path in candidates:
        if path.exists():
            with open(path, "rb") as fh:
                return tomllib.load(fh)
    return {}


def get_default_model() -> str:
    """
    Return the default model from loom.toml [cognition]default_model,
    falling back to "MiniMax-M2.7".
    """
    config = _load_loom_config()
    model = config.get("cognition", {}).get("default_model", "")
    return model if model else "MiniMax-M2.7"


class LLMRouter:
    """Routes chat requests to the correct provider by model name."""

    # Model-name prefix → provider name
    _ROUTING: list[tuple[str, str]] = [
        ("MiniMax-",   "minimax"),
        ("minimax-",   "minimax"),
        ("claude-",    "anthropic"),
        ("gpt-",       "openai"),
        ("openrouter/", "openrouter"),
        ("deepseek-",  "deepseek"),
        ("ollama/",    "ollama"),
        ("lmstudio/",  "lmstudio"),
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
        if self._default:
            return self._providers[self._default]
        raise RuntimeError(
            f"No provider registered for model '{model}'. "
            f"Registered providers: {list(self._providers)}"
        )

    @property
    def providers(self) -> list[str]:
        """Names of all registered providers."""
        return list(self._providers)

    def registered_models(self) -> list[str]:
        """List known model names from all registered providers."""
        models = []
        for provider in self._providers.values():
            if hasattr(provider, "model"):
                models.append(provider.model)
        return models

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
        *,
        abort_signal: Any = None,
    ) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        provider = self.get_provider(model)
        async for item in provider.stream_chat(
            messages=messages, tools=tools, max_tokens=max_tokens,
            abort_signal=abort_signal,
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

    def switch_model(self, model: str) -> bool:
        """
        Switch to a different model/provider.

        Searches the routing table to find the provider that handles the
        given model name prefix, then updates that provider's model attribute.
        Returns True only if a matching provider was found and updated.
        Returns False if no provider recognises the model prefix — the caller
        should surface an error rather than silently applying the name to the
        default provider.
        """
        for prefix, provider_name in self._ROUTING:
            if model.startswith(prefix):
                provider = self._providers.get(provider_name)
                if provider:
                    provider.model = model
                    return True
                # Provider registered in routing table but not in registry
                # (e.g. key not set) — still a valid prefix, just unavailable.
                return False
        return False
