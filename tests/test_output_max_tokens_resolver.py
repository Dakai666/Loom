"""
Tests for ``_resolve_output_max_tokens`` provider-aware resolution (Issue #272).

Resolution order:
  1. ``[cognition.output_max_tokens_overrides][<model>]`` — user explicit
  2. ``[cognition].output_max_tokens`` — global user-set
  3. Provider's ``native_max_tokens(model)`` — provider-declared truth
  4. ``_DEFAULT_OUTPUT_MAX_TOKENS`` — last-resort constant

Step 3 is the new layer that lets users drop the overrides table.
"""

from __future__ import annotations

from types import SimpleNamespace

from loom.core.cognition.providers import AnthropicProvider, LLMProvider
from loom.core.session import (
    _DEFAULT_OUTPUT_MAX_TOKENS,
    _resolve_output_max_tokens,
)


class _StubProvider(LLMProvider):
    """Minimal provider double for resolver tests — does nothing real."""

    name = "stub"
    NATIVE_OUTPUT_LIMITS = {"stub-large": 100000, "stub-small": 4096}

    async def chat(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def format_tool_result(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _Router:
    """Just enough surface for ``_resolve_output_max_tokens``."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def get_provider(self, model: str) -> LLMProvider:
        return self._provider


class TestPrecedenceOrder:
    """The four-step ladder must be honoured top-to-bottom."""

    def test_explicit_override_wins(self) -> None:
        cfg = {
            "cognition": {
                "output_max_tokens": 8192,
                "output_max_tokens_overrides": {"stub-large": 1024},
            }
        }
        router = _Router(_StubProvider())
        # Override (1024) beats global (8192), provider (100000), and default
        assert _resolve_output_max_tokens(cfg, "stub-large", router) == 1024

    def test_global_default_used_when_no_override(self) -> None:
        cfg = {"cognition": {"output_max_tokens": 16384}}
        router = _Router(_StubProvider())
        # Global (16384) beats provider native (100000) and constant default
        assert _resolve_output_max_tokens(cfg, "stub-large", router) == 16384

    def test_provider_native_used_when_config_silent(self) -> None:
        cfg: dict = {}
        router = _Router(_StubProvider())
        # No user config → provider's table wins for known models
        assert _resolve_output_max_tokens(cfg, "stub-large", router) == 100000
        assert _resolve_output_max_tokens(cfg, "stub-small", router) == 4096

    def test_constant_fallback_when_provider_unknown(self) -> None:
        cfg: dict = {}
        router = _Router(_StubProvider())
        # Model not in provider's table → fall through to constant
        assert (
            _resolve_output_max_tokens(cfg, "stub-mystery", router)
            == _DEFAULT_OUTPUT_MAX_TOKENS
        )


class TestRouterOptional:
    """The router parameter is optional for backward compat."""

    def test_no_router_skips_provider_layer(self) -> None:
        cfg: dict = {}
        # Without router, step 3 is skipped → constant default
        assert (
            _resolve_output_max_tokens(cfg, "stub-large")
            == _DEFAULT_OUTPUT_MAX_TOKENS
        )

    def test_no_router_with_global_default(self) -> None:
        cfg = {"cognition": {"output_max_tokens": 12345}}
        assert _resolve_output_max_tokens(cfg, "anything") == 12345


class TestRouterFailureSafe:
    """Routing exceptions must not break the resolver — fall through silently."""

    def test_router_raising_falls_through_to_default(self) -> None:
        class _BrokenRouter:
            def get_provider(self, model):
                raise RuntimeError("provider not registered")

        cfg: dict = {}
        # No exception leaks; we get the constant default
        assert (
            _resolve_output_max_tokens(cfg, "anything", _BrokenRouter())
            == _DEFAULT_OUTPUT_MAX_TOKENS
        )

    def test_provider_returning_none_falls_through(self) -> None:
        class _NullProvider(LLMProvider):
            name = "null"

            async def chat(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

            def format_tool_result(self, *a, **k):  # pragma: no cover
                raise NotImplementedError

        cfg: dict = {}
        assert (
            _resolve_output_max_tokens(cfg, "anything", _Router(_NullProvider()))
            == _DEFAULT_OUTPUT_MAX_TOKENS
        )


class TestAnthropicProviderTable:
    """The shipped table for Claude / MiniMax / DeepSeek must cover the
    models the project actually uses (regression guard against accidental
    deletions / typos in NATIVE_OUTPUT_LIMITS)."""

    def test_known_anthropic_models_have_entries(self) -> None:
        p = object.__new__(AnthropicProvider)  # bypass __init__ (needs API key)
        for model, expected in [
            ("claude-opus-4-7", 32768),
            ("claude-sonnet-4-6", 65536),
            ("claude-haiku-4-5", 8192),
        ]:
            assert p.native_max_tokens(model) == expected, model

    def test_minimax_lookup_is_case_insensitive(self) -> None:
        """The user's loom.toml had both ``"minimax-M2.7"`` and
        ``"MiniMax-M2.7"`` because case mismatches were silently failing.
        Provider lookup must canonicalize."""
        p = object.__new__(AnthropicProvider)
        for variant in ("minimax-m2.7", "MiniMax-M2.7", "MINIMAX-M2.7"):
            assert p.native_max_tokens(variant) == 65536, variant

    def test_deepseek_models_have_entries(self) -> None:
        p = object.__new__(AnthropicProvider)
        assert p.native_max_tokens("deepseek-chat") == 8192
        assert p.native_max_tokens("deepseek-reasoner") == 8192

    def test_unknown_model_returns_none(self) -> None:
        p = object.__new__(AnthropicProvider)
        assert p.native_max_tokens("claude-gibberish-99") is None
        assert p.native_max_tokens("") is None


class TestEmptyModelHandling:
    """Edge cases on the model parameter."""

    def test_empty_model_string(self) -> None:
        cfg: dict = {}
        router = _Router(_StubProvider())
        # Empty model should not crash; provider returns None → constant
        assert (
            _resolve_output_max_tokens(cfg, "", router)
            == _DEFAULT_OUTPUT_MAX_TOKENS
        )
