"""Regression checks for Quest D Phase 1.C skill evolution retirement."""

from __future__ import annotations

import inspect

import pytest


def test_retired_skill_lifecycle_tool_factories_are_not_exported() -> None:
    from loom.platform.cli import tools, skill_tools

    assert not hasattr(tools, "make_skill_promote_tool")
    assert not hasattr(tools, "make_skill_rollback_tool")
    assert not hasattr(tools, "make_generate_skill_candidate_from_batch_tool")
    assert not hasattr(tools, "make_set_skill_maturity_tool")
    # After audit-B (#399) skill factories moved to cli.skill_tools.
    assert not hasattr(skill_tools, "make_skill_promote_tool")
    assert not hasattr(skill_tools, "make_skill_rollback_tool")
    assert "skill_gate" not in inspect.signature(skill_tools.make_load_skill_tool).parameters


def test_load_loom_config_silently_accepts_legacy_mutation_section(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy [mutation] section is parsed without error (silently ignored).

    The earlier UserWarning was removed in audit-A round 2 (#398) after the
    section had been retired long enough that anyone with it in their
    loom.toml would already have seen — and acted on — the warning.
    """
    from loom.core import session

    (tmp_path / "loom.toml").write_text(
        "[mutation]\n"
        "enabled = true\n"
        "shadow_mode = \"auto_c\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = session._load_loom_config()

    # Toml is parsed; the [mutation] section is harmlessly ignored by every
    # consumer in the current codebase.
    assert cfg["mutation"]["enabled"] is True
