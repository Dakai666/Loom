"""
LLM Router — single entry point for all model interactions.

Selects the correct provider based on model name prefix,
then delegates to that provider's chat() method.

Model routing rules
-------------------
"MiniMax-*"    → MiniMaxProvider
"minimax-*"    → MiniMaxProvider
"claude-*"     → AnthropicProvider
"gpt-*"        → OpenAIProvider
"openai/*"     → OpenAIProvider
"codex/*"      → CodexResponsesProvider
"chatgpt/*"    → ChatGPTResponsesProvider (official ChatGPT plan usage)
"xai/*"        → XAIResponsesProvider
"ollama/*"     → OllamaProvider    (local Ollama server)
"lmstudio/*"   → LMStudioProvider  (local LM Studio server)
(default)      → first registered provider

Usage
-----
    router = LLMRouter()
    router.register(MiniMaxProvider(api_key="...", model="MiniMax-M3.1-Flash"))
    router.register(AnthropicProvider(api_key="..."))
    router.register(OllamaProvider())   # no key needed

    response = await router.chat(
        model="ollama/llama3.2",
        messages=[...],
        tools=[...],
    )
"""

import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from loom.core.config import load_loom_config

from .providers import LLMProvider, LLMResponse
from .responses_sse import QUOTA_FAILURE_TYPE, ResponsesProviderError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Quota fallback — process-wide state
# ---------------------------------------------------------------------------
# A plan quota is account-wide, so the cooldown is shared by every router in
# the process (each session builds its own router). Keyed by provider name.

_clock: Callable[[], float] = time.monotonic
DEFAULT_QUOTA_COOLDOWN_SECONDS = 1800.0


@dataclass(frozen=True)
class FailoverEvent:
    kind: str                  # "failover" | "restored"
    provider: str              # provider whose quota ran out
    primary_model: str
    fallback_model: str
    detail: str = ""
    cooldown_seconds: float = 0.0


_cooldown_until: dict[str, float] = {}
_listeners: list[Callable[[FailoverEvent], None]] = []


def add_failover_listener(listener: Callable[[FailoverEvent], None]) -> None:
    """Register a process-wide callback for failover / restored events.

    Called synchronously inside the request's coroutine; listeners that need
    I/O should schedule a task. A listener that raises is logged and ignored.
    """
    if listener not in _listeners:
        _listeners.append(listener)


def remove_failover_listener(listener: Callable[[FailoverEvent], None]) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def reset_failover_state() -> None:
    """Clear cooldowns and listeners (tests)."""
    _cooldown_until.clear()
    _listeners.clear()


def _emit(event: FailoverEvent) -> None:
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:
            logger.exception("quota fallback listener failed")


def get_default_model() -> str:
    """
    Return the default model from loom.toml [cognition]default_model,
    falling back to "MiniMax-M3.1-Flash".
    """
    config = load_loom_config()
    model = config.get("cognition", {}).get("default_model", "")
    return model if model else "MiniMax-M3.1-Flash"


class LLMRouter:
    """Routes chat requests to the correct provider by model name."""

    # Model-name prefix → provider name
    _ROUTING: list[tuple[str, str]] = [
        ("MiniMax-",   "minimax"),
        ("minimax-",   "minimax"),
        ("claude-",    "anthropic"),
        ("gpt-",       "openai"),
        ("openai/",    "openai"),
        ("codex/",     "codex"),
        ("chatgpt/",   "chatgpt"),
        ("xai/",       "xai"),
        ("openrouter/", "openrouter"),
        ("deepseek-",  "deepseek"),
        ("ollama/",    "ollama"),
        ("lmstudio/",  "lmstudio"),
    ]

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._default: str | None = None
        self._quota_fallback: str | None = None
        self._quota_cooldown = DEFAULT_QUOTA_COOLDOWN_SECONDS
        self._max_tokens_resolver: Callable[[str], int] | None = None

    # ------------------------------------------------------------------
    # Quota fallback
    # ------------------------------------------------------------------

    def set_quota_fallback(
        self,
        model: str | None,
        *,
        cooldown_seconds: float = DEFAULT_QUOTA_COOLDOWN_SECONDS,
        max_tokens_resolver: Callable[[str], int] | None = None,
    ) -> None:
        """Serve *model* when a provider reports its quota is exhausted.

        Opt-in (``[cognition] fallback_model``). Only ``entitlement_or_quota``
        failures raised before any output trigger it; the exhausted provider
        is then skipped for ``Retry-After`` seconds (or *cooldown_seconds*).
        """
        self._quota_fallback = model or None
        self._quota_cooldown = cooldown_seconds
        self._max_tokens_resolver = max_tokens_resolver

    def _fallback_for(self, model: str) -> str | None:
        """Fallback model usable for *model*, or None (unset, same provider,
        or fallback provider not registered)."""
        fallback = self._quota_fallback
        if not fallback:
            return None
        primary, target = self._provider_name_for_model(model), self._provider_name_for_model(fallback)
        if target is None or target not in self._providers or primary == target:
            return None
        return fallback

    def _cooling(self, model: str) -> str | None:
        """Fallback to use right now because *model*'s provider is cooling down."""
        provider = self._provider_name_for_model(model)
        until = _cooldown_until.get(provider or "")
        if until is None:
            return None
        if _clock() >= until:
            return None  # expired — next call probes the primary again
        return self._fallback_for(model)

    def effective_model(self, model: str) -> str:
        """The model that will actually serve a call for *model* right now."""
        return self._cooling(model) or model

    def _quota_failover(self, model: str, exc: ResponsesProviderError) -> str | None:
        if exc.failure_type != QUOTA_FAILURE_TYPE:
            return None
        fallback = self._fallback_for(model)
        if fallback is None:
            return None
        provider = self._provider_name_for_model(model) or ""
        cooldown = exc.retry_after_seconds or self._quota_cooldown
        _cooldown_until[provider] = _clock() + cooldown
        logger.warning(
            "quota exhausted on %s (%s); serving %s for %.0fs",
            model, exc.failure_type, fallback, cooldown,
        )
        _emit(FailoverEvent("failover", provider, model, fallback, exc.detail[:500], cooldown))
        return fallback

    def _note_primary_served(self, model: str) -> None:
        provider = self._provider_name_for_model(model) or ""
        if provider in _cooldown_until:
            del _cooldown_until[provider]
            _emit(FailoverEvent("restored", provider, model, self._quota_fallback or ""))

    def _fallback_max_tokens(self, fallback: str, max_tokens: int) -> int:
        if self._max_tokens_resolver is None:
            return max_tokens
        try:
            return int(self._max_tokens_resolver(fallback))
        except Exception:
            logger.exception("fallback max_tokens resolution failed; reusing primary budget")
            return max_tokens

    def register(
        self,
        provider: LLMProvider,
        default: bool = False,
        fallback: bool = True,
    ) -> "LLMRouter":
        self._providers[provider.name] = provider
        if default or (fallback and self._default is None):
            self._default = provider.name
        return self

    def _provider_name_for_model(self, model: str) -> str | None:
        for prefix, provider_name in self._ROUTING:
            if model.startswith(prefix):
                return provider_name
        return None

    def get_provider(self, model: str) -> LLMProvider:
        provider_name = self._provider_name_for_model(model)
        if provider_name:
            p = self._providers.get(provider_name)
            if p:
                return p
            # Recognized prefix but the provider isn't registered (OAuth login
            # missing, key absent, or provider disabled). Do NOT silently fall
            # back to the default provider — that would serve a *different*
            # model under the requested name (e.g. ``xai/grok-4.3`` answered by
            # MiniMax), and every display surface would keep showing the
            # requested name. Surface it loudly instead. See issue #471.
            raise RuntimeError(
                f"Model '{model}' routes to provider '{provider_name}', which is "
                f"not registered (OAuth/credential missing or provider disabled?). "
                f"Registered providers: {list(self._providers)}"
            )
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

    def provider_instances(self) -> list[LLMProvider]:
        """All registered provider objects (for session-wide settings such as
        reasoning effort). Per-call routing still goes through ``get_provider``."""
        return list(self._providers.values())

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
        if (cooling := self._cooling(model)) is not None:
            return await self._chat_on(cooling, messages, tools, self._fallback_max_tokens(cooling, max_tokens))
        try:
            response = await self._chat_on(model, messages, tools, max_tokens)
        except ResponsesProviderError as exc:
            fallback = self._quota_failover(model, exc)
            if fallback is None:
                raise
            return await self._chat_on(fallback, messages, tools, self._fallback_max_tokens(fallback, max_tokens))
        self._note_primary_served(model)
        return response

    async def _chat_on(self, model: str, messages: list[dict], tools: list[dict] | None,
                       max_tokens: int) -> LLMResponse:
        provider = self.get_provider(model).for_model(model)
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
        cooling = self._cooling(model)
        if cooling is not None:
            async for item in self._stream_on(cooling, messages, tools,
                                              self._fallback_max_tokens(cooling, max_tokens), abort_signal):
                yield item
            return
        yielded = False
        try:
            async for item in self._stream_on(model, messages, tools, max_tokens, abort_signal):
                yielded = True
                yield item
        except ResponsesProviderError as exc:
            # Never replay on the fallback once output reached the caller —
            # that would duplicate text the user already saw.
            fallback = None if yielded else self._quota_failover(model, exc)
            if fallback is None:
                raise
            async for item in self._stream_on(fallback, messages, tools,
                                              self._fallback_max_tokens(fallback, max_tokens), abort_signal):
                yield item
            return
        self._note_primary_served(model)

    async def _stream_on(self, model: str, messages: list[dict], tools: list[dict] | None,
                         max_tokens: int, abort_signal: Any) -> AsyncIterator[tuple[str, LLMResponse | None]]:
        provider = self.get_provider(model).for_model(model)
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
        provider_name = self._provider_name_for_model(model)
        if provider_name:
            provider = self._providers.get(provider_name)
            if provider:
                provider.model = model
                return True
            # Provider registered in routing table but not in registry
            # (e.g. key not set) — still a valid prefix, just unavailable.
            return False
        return False
