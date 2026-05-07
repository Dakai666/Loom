"""Issue #290 — Discord footer must reflect active tier model + tier badge,
not the registered default. Mirrors CLI footer #276 gating: badge only
when sticky tier ≠ default tier.
"""

from types import SimpleNamespace

from loom.platform.discord.bot import _resolve_active_model_and_tier


def _stub_session(
    *,
    model: str = "minimax-m2.7",
    tier_models: dict[int, str] | None = None,
    default_tier: int = 1,
    sticky_tier: int | None = None,
) -> SimpleNamespace:
    """Minimal stub mirroring the LoomSession fields the helper reads."""
    active_tier = sticky_tier if sticky_tier is not None else default_tier
    return SimpleNamespace(
        model=model,
        _tier_models=tier_models or {},
        _default_tier=default_tier,
        _active_tier=lambda: active_tier,
        _active_model=lambda: (tier_models or {}).get(active_tier, model),
    )


def test_no_tier_system_uses_session_model_no_badge():
    s = _stub_session(model="minimax-m2.7")
    model, badge = _resolve_active_model_and_tier(s)
    assert model == "minimax-m2.7"
    assert badge == ""


def test_tier_system_on_default_tier_no_badge():
    s = _stub_session(
        model="minimax-m2.7",
        tier_models={1: "minimax-m2.7", 2: "deepseek-v4-pro"},
        default_tier=1,
        sticky_tier=None,
    )
    model, badge = _resolve_active_model_and_tier(s)
    assert model == "minimax-m2.7"
    assert badge == ""


def test_tier_system_sticky_override_shows_active_model_and_badge():
    """The exact scenario from issue #290: session escalated to Tier 2,
    Discord footer used to keep showing the Tier 1 default."""
    s = _stub_session(
        model="minimax-m2.7",
        tier_models={1: "minimax-m2.7", 2: "deepseek-v4-pro"},
        default_tier=1,
        sticky_tier=2,
    )
    model, badge = _resolve_active_model_and_tier(s)
    assert model == "deepseek-v4-pro"
    assert "Tier 2" in badge
    assert "⇪" in badge


def test_badge_suffix_is_appendable_format():
    """Badge starts with the `  ·  ` separator so the call site can
    concatenate unconditionally without conditional spacing."""
    s = _stub_session(
        tier_models={1: "a", 2: "b"},
        default_tier=1,
        sticky_tier=2,
    )
    _, badge = _resolve_active_model_and_tier(s)
    assert badge.startswith("  ·  ")
