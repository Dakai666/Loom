"""
Contract: the context budget follows the model that will actually serve.

The budget used to be a hard-coded 204_800 regardless of model, while the
current tiers (MiniMax-M3.1-Flash, DeepSeek flash / v4-pro) all have 1M
windows. ``[cognition] max_tokens`` in loom.toml looked like the knob but
was never read.

Invariants:
  - Providers declare native context windows (like NATIVE_OUTPUT_LIMITS).
  - Resolution: per-model override > native (capped by the global
    ``[cognition] context_window`` ceiling) > 204_800 fallback.
  - The ceiling never *raises* a window above what the model supports.
  - Before every LLM call the budget is re-synced to the active model; if a
    tier switch lands on a smaller window, history is compacted *before*
    the call instead of overflowing it.
  - The telemetry context_layout window tracks the same number.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from loom.core.cognition.context import ContextBudget
from loom.core.cognition.providers import AnthropicProvider
from loom.core.cognition.router import LLMRouter
from loom.core.session import LoomSession
from loom.core.session_support import (
    _DEFAULT_CONTEXT_WINDOW,
    _resolve_context_window,
)


def _anthropic(name: str = "minimax", model: str = "MiniMax-M3.1-Flash-Preview") -> AnthropicProvider:
    return AnthropicProvider(
        api_key="test", model=model,
        base_url="https://example.invalid/anthropic", name=name,
    )


def _router() -> LLMRouter:
    r = LLMRouter()
    r.register(_anthropic("minimax"), default=True)
    r.register(_anthropic("deepseek", "deepseek-v4-pro"))
    return r


class TestNativeContextWindows:
    @pytest.mark.parametrize("model", [
        "MiniMax-M3.1-Flash-Preview", "minimax-m3.1-flash-preview", "MiniMax-M3",
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro",
    ])
    def test_1m_models(self, model):
        assert _anthropic().native_context_window(model) == 1_000_000

    def test_m27_keeps_its_window(self):
        assert _anthropic().native_context_window("minimax-m2.7") == 204_800

    def test_unknown_is_none(self):
        assert _anthropic().native_context_window("claude-gibberish") is None

    def test_deepseek_output_limits_raised(self):
        p = _anthropic()
        for m in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"):
            assert p.native_max_tokens(m) == 131072, m


class TestResolveContextWindow:
    def test_native_when_unconfigured(self):
        assert _resolve_context_window({}, "minimax-m3.1-flash-preview", _router()) == 1_000_000

    def test_fallback_for_unknown_model(self):
        assert _resolve_context_window({}, "claude-gibberish", _router()) == _DEFAULT_CONTEXT_WINDOW
        assert _DEFAULT_CONTEXT_WINDOW == 204_800

    def test_fallback_without_router(self):
        assert _resolve_context_window({}, "minimax-m3.1-flash-preview") == _DEFAULT_CONTEXT_WINDOW

    def test_global_ceiling_caps_native(self):
        cfg = {"cognition": {"context_window": 400_000}}
        assert _resolve_context_window(cfg, "deepseek-flash", _router()) == 400_000

    def test_global_ceiling_never_raises_above_native(self):
        cfg = {"cognition": {"context_window": 2_000_000}}
        assert _resolve_context_window(cfg, "minimax-m2.7", _router()) == 204_800

    def test_global_ceiling_applies_to_unknown_model(self):
        cfg = {"cognition": {"context_window": 128_000}}
        assert _resolve_context_window(cfg, "claude-gibberish", _router()) == 128_000

    def test_per_model_override_wins(self):
        cfg = {"cognition": {
            "context_window": 400_000,
            "context_window_overrides": {"deepseek-flash": 600_000},
        }}
        assert _resolve_context_window(cfg, "deepseek-flash", _router()) == 600_000

    def test_invalid_values_ignored(self):
        cfg = {"cognition": {
            "context_window": "huge",
            "context_window_overrides": {"deepseek-flash": -5},
        }}
        assert _resolve_context_window(cfg, "deepseek-flash", _router()) == 1_000_000

    def test_legacy_max_tokens_key_is_not_a_context_knob(self):
        # [cognition] max_tokens was dead config; it must not start capping now.
        cfg = {"cognition": {"max_tokens": 204_800}}
        assert _resolve_context_window(cfg, "deepseek-flash", _router()) == 1_000_000


# ---------------------------------------------------------------------------
# Session: re-sync + compact-before-call
# ---------------------------------------------------------------------------

class _FakeSession:
    _sync_context_budget = LoomSession._sync_context_budget
    _fit_context_to = LoomSession._fit_context_to


class _Layout:
    def __init__(self) -> None:
        self.window: int | None = None

    def set_max_window(self, n: int) -> None:
        self.window = n


def _session(cfg: dict | None = None) -> _FakeSession:
    s = _FakeSession()
    s._loom_config = cfg or {}
    s.router = _router()
    s.budget = ContextBudget(total_tokens=_DEFAULT_CONTEXT_WINDOW)
    s.messages = []
    layout = _Layout()
    s._layout = layout
    s._telemetry = SimpleNamespace(get=lambda n: layout if n == "context_layout" else None)
    s.compactions = 0

    async def _compact() -> None:
        s.compactions += 1
        s.budget.used_tokens = 10_000
    s._smart_compact = _compact
    return s


class TestSessionContextBudget:
    def test_sync_sets_budget_and_telemetry(self):
        s = _session()
        assert s._sync_context_budget("deepseek-flash") == 1_000_000
        assert s.budget.total_tokens == 1_000_000
        assert s._layout.window == 1_000_000

    def test_sync_tolerates_missing_telemetry(self):
        s = _session()
        s._telemetry = None
        assert s._sync_context_budget("minimax-m2.7") == 204_800

    async def test_large_window_no_compaction_at_old_threshold(self):
        # 300K used would have compacted under the old hard-coded 204_800.
        s = _session()
        s.budget.used_tokens = 300_000
        await s._fit_context_to("minimax-m3.1-flash-preview", where="initial")
        assert s.compactions == 0

    async def test_switch_to_smaller_window_compacts_before_call(self):
        s = _session()
        s._sync_context_budget("minimax-m3.1-flash-preview")
        s.budget.used_tokens = 600_000
        await s._fit_context_to("minimax-m2.7", where="tier switch")
        assert s.compactions == 1
        assert s.budget.total_tokens == 204_800
