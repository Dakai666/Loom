"""
Contract tests for reasoning-effort control on Anthropic-compatible models.

MiniMax-M3.1-Flash-Preview exposes thinking depth via ``output_config.effort``
(low | medium | high | xhigh | max); omitting it means the vendor default
``max`` — every turn at the deepest, slowest setting. Thinking cannot be
disabled (``none`` → 400).

Invariants:
  - ``output_config`` is sent ONLY for models that declare effort support;
    every other model on the same provider class (M2.7, DeepSeek, Claude)
    gets a byte-identical payload to before.
  - Effort unset → field omitted (vendor default), never a guessed value.
  - Session precedence: agent/user override > ``[cognition] reasoning_effort``
    > vendor default. Clearing the override falls back, it doesn't reset to max.
  - The setting survives ``set_model`` router rebuilds (provider instances
    are replaced, the session's intent is not).
  - The agent can see its current effort in ``runtime_identity``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from loom.core.cognition.providers import (
    ANTHROPIC_EFFORT_LEVELS,
    AnthropicProvider,
    normalize_anthropic_effort,
)
from loom.core.cognition.router import LLMRouter
from loom.core.infra.telemetry import RuntimeIdentityDimension
from loom.core.session import LoomSession
from loom.core.tier_manager import TierManager
from loom.platform.cli.tools import make_set_reasoning_effort_tool


M31 = "MiniMax-M3.1-Flash-Preview"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _fake_message() -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


class _CapturingSyncMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return _fake_message()


class _FakeStream:
    def __init__(self) -> None:
        async def _gen():
            yield "ok"
        self.text_stream = _gen()

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return _fake_message()


class _CapturingAsyncMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.kwargs = kwargs
        return _FakeStream()


def _provider(model: str, effort: str | None = None) -> AnthropicProvider:
    p = AnthropicProvider(
        api_key="test", model=model,
        base_url="https://example.invalid/anthropic", name="minimax",
    )
    p.effort = effort
    p._client = SimpleNamespace(messages=_CapturingSyncMessages())
    p._async_client = SimpleNamespace(messages=_CapturingAsyncMessages())
    return p


def _sync_kwargs(p: AnthropicProvider) -> dict[str, Any]:
    p._sync_chat([{"role": "user", "content": "hi"}], None, 256)
    return p._client.messages.kwargs


async def _stream_kwargs(p: AnthropicProvider) -> dict[str, Any]:
    async for _ in p.stream_chat([{"role": "user", "content": "hi"}], None, 256):
        pass
    return p._async_client.messages.kwargs


# ---------------------------------------------------------------------------
# Provider: wire format
# ---------------------------------------------------------------------------

class TestEffortWireFormat:
    def test_supported_model_sends_output_config_sync(self):
        kw = _sync_kwargs(_provider(M31, "high"))
        assert kw["output_config"] == {"effort": "high"}

    async def test_supported_model_sends_output_config_stream(self):
        kw = await _stream_kwargs(_provider(M31, "low"))
        assert kw["output_config"] == {"effort": "low"}

    def test_lowercase_model_name_is_supported(self):
        kw = _sync_kwargs(_provider("minimax-m3.1-flash-preview", "medium"))
        assert kw["output_config"] == {"effort": "medium"}

    @pytest.mark.parametrize(
        "model", ["minimax-m2.7", "MiniMax-M3", "claude-sonnet-4-6", "deepseek-flash"],
    )
    async def test_unsupported_model_payload_unchanged(self, model):
        p = _provider(model, "high")
        assert "output_config" not in _sync_kwargs(p)
        assert "output_config" not in await _stream_kwargs(p)

    async def test_unset_effort_omits_field(self):
        p = _provider(M31, None)
        assert "output_config" not in _sync_kwargs(p)
        assert "output_config" not in await _stream_kwargs(p)


class TestEffortCapabilityTable:
    def test_supports_effort(self):
        p = _provider(M31)
        assert p.supports_effort(M31)
        assert p.supports_effort("minimax-m3.1-flash-preview")
        assert not p.supports_effort("minimax-m2.7")
        assert not p.supports_effort("")

    def test_vendor_default_is_declared(self):
        p = _provider(M31)
        assert p.default_effort(M31) == "max"
        assert p.default_effort("minimax-m2.7") is None

    def test_levels(self):
        assert ANTHROPIC_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    @pytest.mark.parametrize("raw,expected", [
        ("high", "high"), ("HIGH", "high"), (" max ", "max"),
        ("none", None),  # thinking can't be disabled — 400 on the wire
        ("bogus", None), ("", None), (None, None), (3, None),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_anthropic_effort(raw) == expected


# ---------------------------------------------------------------------------
# Session: precedence + propagation
# ---------------------------------------------------------------------------

class _FakeSession:
    """Reuses LoomSession's real effort/tier methods over minimal state."""

    _tier_models = property(lambda self: self._tier.tier_models)
    _sticky_tier = property(lambda self: self._tier.sticky_tier)
    _active_tier = LoomSession._active_tier
    _active_model = LoomSession._active_model
    _refresh_runtime_identity = LoomSession._refresh_runtime_identity
    _effective_effort = LoomSession._effective_effort
    _effort_display = LoomSession._effort_display
    _apply_reasoning_effort = LoomSession._apply_reasoning_effort
    _set_reasoning_effort = LoomSession._set_reasoning_effort
    set_model = LoomSession.set_model


def _router_with(provider: AnthropicProvider) -> LLMRouter:
    r = LLMRouter()
    r.register(provider, default=True)
    return r


def _session(*, default: str | None = None, model: str = M31) -> _FakeSession:
    s = _FakeSession()
    s._model = model
    s._tier = TierManager(
        base_model_getter=lambda: s._model,
        tier_models={1: model, 2: "deepseek-flash"},
        default_tier=1, reminder_after_turns=10,
    )
    s._effort_default = default
    s._effort_override = None
    s._lifecycle_events = asyncio.Queue()
    ri = RuntimeIdentityDimension()
    s._telemetry = SimpleNamespace(get=lambda name: ri if name == "runtime_identity" else None)
    s._ri = ri
    s.router = _router_with(_provider(model))
    return s


def _mm(s: _FakeSession) -> AnthropicProvider:
    return s.router._providers["minimax"]


class TestSessionEffort:
    def test_config_default_applies_to_provider(self):
        s = _session(default="high")
        s._apply_reasoning_effort()
        assert _mm(s).effort == "high"

    def test_no_config_leaves_vendor_default(self):
        s = _session()
        s._apply_reasoning_effort()
        assert _mm(s).effort is None
        assert s._effective_effort() is None

    def test_override_beats_config_and_clear_falls_back(self):
        s = _session(default="high")
        s._set_reasoning_effort("low")
        assert _mm(s).effort == "low"
        s._set_reasoning_effort(None)
        assert s._effort_override is None
        assert _mm(s).effort == "high"  # back to config, not vendor max

    def test_effort_survives_router_rebuild(self, monkeypatch):
        s = _session(default="medium")
        # Original router lacks the minimax provider → set_model must rebuild.
        s.router = LLMRouter()
        fresh = _router_with(_provider("minimax-m2.7"))
        monkeypatch.setattr("loom.core.session.build_router", lambda active_model=None: fresh)
        assert s.set_model(M31)
        assert s.router is fresh
        assert _mm(s).effort == "medium"

    def test_runtime_identity_shows_effort(self):
        s = _session(default="high")
        s._set_reasoning_effort("xhigh")
        assert "effort: xhigh" in s._ri.render_detail()
        assert "xhigh" in s._ri.render_summary()

    def test_runtime_identity_shows_vendor_default(self):
        s = _session()
        s._refresh_runtime_identity()
        assert "effort: max (vendor default)" in s._ri.render_detail()

    def test_runtime_identity_unsupported_model_hides_effort(self):
        s = _session(model="minimax-m2.7")
        s._refresh_runtime_identity()
        assert "effort" not in s._ri.render_detail()


# ---------------------------------------------------------------------------
# Tool: set_reasoning_effort
# ---------------------------------------------------------------------------

def _call(**kwargs: Any) -> Any:
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel
    return ToolCall(
        id="call_effort", tool_name="set_reasoning_effort", args=kwargs,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


class TestSetReasoningEffortTool:
    async def test_reason_required(self):
        s = _session()
        r = await make_set_reasoning_effort_tool(s).executor(_call(effort="low"))
        assert not r.success and "reason" in r.error.lower()

    async def test_invalid_level_lists_options(self):
        s = _session()
        r = await make_set_reasoning_effort_tool(s).executor(
            _call(effort="none", reason="x"),
        )
        assert not r.success
        for lvl in ANTHROPIC_EFFORT_LEVELS:
            assert lvl in r.error

    async def test_sets_override_and_provider(self):
        s = _session(default="high")
        r = await make_set_reasoning_effort_tool(s).executor(
            _call(effort="low", reason="DK 說今天輕一點"),
        )
        assert r.success
        assert s._effort_override == "low"
        assert _mm(s).effort == "low"
        assert r.metadata["effort"] == "low"
        assert r.metadata["applies"] is True

    async def test_default_clears_override(self):
        s = _session(default="high")
        tool = make_set_reasoning_effort_tool(s)
        await tool.executor(_call(effort="max", reason="hard"))
        r = await tool.executor(_call(effort="default", reason="done"))
        assert r.success
        assert s._effort_override is None
        assert _mm(s).effort == "high"

    async def test_unsupported_active_model_still_records_but_says_so(self):
        s = _session(model="minimax-m2.7")
        r = await make_set_reasoning_effort_tool(s).executor(
            _call(effort="low", reason="x"),
        )
        assert r.success
        assert s._effort_override == "low"
        assert r.metadata["applies"] is False
        assert "does not support" in r.output


# ---------------------------------------------------------------------------
# End-to-end through the router (depends on per-call model binding)
# ---------------------------------------------------------------------------

class TestEffortThroughRouter:
    async def test_effort_follows_routed_model_not_build_time_model(self):
        # Provider built on an effort-less model; the tier routes to M3.1.
        p = _provider("MiniMax-M2.7", "low")
        r = _router_with(p)
        async for _ in r.stream_chat(
            model="minimax-m3.1-flash-preview",
            messages=[{"role": "user", "content": "hi"}], max_tokens=64,
        ):
            pass
        assert p._async_client.messages.kwargs["output_config"] == {"effort": "low"}

    async def test_switching_back_to_unsupported_model_drops_effort(self):
        p = _provider(M31, "low")
        r = _router_with(p)
        await r.chat(model="minimax-m2.7", messages=[{"role": "user", "content": "hi"}], max_tokens=64)
        assert "output_config" not in p._client.messages.kwargs
