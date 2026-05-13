"""Regression checks for Quest D Phase 1.C skill evolution retirement."""

from __future__ import annotations

import inspect

import pytest


def test_retired_skill_lifecycle_tool_factories_are_not_exported() -> None:
    from loom.platform.cli import tools

    assert not hasattr(tools, "make_skill_promote_tool")
    assert not hasattr(tools, "make_skill_rollback_tool")
    assert not hasattr(tools, "make_generate_skill_candidate_from_batch_tool")
    assert not hasattr(tools, "make_set_skill_maturity_tool")
    assert "skill_gate" not in inspect.signature(tools.make_load_skill_tool).parameters


def test_load_loom_config_warns_about_retired_mutation_section(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom.core import session

    (tmp_path / "loom.toml").write_text(
        "[mutation]\n"
        "enabled = true\n"
        "shadow_mode = \"auto_c\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.warns(UserWarning, match=r"\[mutation\].*retired.*safe to delete"):
        cfg = session._load_loom_config()

    assert cfg["mutation"]["enabled"] is True
