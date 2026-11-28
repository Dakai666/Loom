"""#708 S4b Item 4: load-time allowed_tools drift detection (synthetic workspace only).

Contracts pinned here:
- A workspace skill name placed in ``allowed_tools`` (e.g. ``async_jobs``) logs
  a WARNING identifying it as a skill name rather than a tool name.
- A retired or unconfigured MCP tool (e.g. ``openai__text_to_image``) or an
  unknown tool name logs a WARNING identifying tool-name drift.
- Valid native tools (core + conditional CLI/Discord/Circadian tools, including
  names that also match a skill folder such as ``weather``) and configured MCP
  server prefixes (e.g. ``minimax_coding__web_search``, ``minimax__text_to_image``)
  produce zero drift warnings.
- Detection is non-blocking (entries still load with declared tools intact) and
  keeps existing loader call signatures backward-compatible.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import loom
from loom.autonomy.circadian.rhythm import load_rhythm
from loom.autonomy.permission_names import (
    CONDITIONAL_TOOLS,
    KNOWN_NATIVE_TOOLS,
    warn_allowed_tool_drift,
)
from loom.autonomy.schedules import load_schedules


def _make_skill(ws: Path, name: str, body: str = "---\nname: x\nallows: {}\n---\n") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def _write_loom_toml(ws: Path, mcp_servers: tuple[str, ...]) -> None:
    blocks = "\n".join(
        f'[[mcp.servers]]\nname = "{name}"\ncommand = "true"\n' for name in mcp_servers
    )
    (ws / "loom.toml").write_text(f"[autonomy]\nenabled = true\n\n{blocks}", encoding="utf-8")


def _write_config(ws: Path, kind: str, tools: list[str]) -> Path:
    tools_toml = ", ".join(f'"{t}"' for t in tools)
    if kind == "schedule":
        path = ws / "autonomy" / "schedules.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'[[schedules]]\nname = "morning_briefing"\ncron = "0 1 * * *"\n'
            f'intent = "synthetic"\nallowed_tools = [{tools_toml}]\n',
            encoding="utf-8",
        )
        return path
    if kind == "trigger":
        path = ws / "autonomy" / "schedules.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'[[triggers]]\nname = "on_event"\nevent = "synthetic"\n'
            f'intent = "synthetic"\nallowed_tools = [{tools_toml}]\n',
            encoding="utf-8",
        )
        return path
    path = ws / "autonomy" / "circadian" / "rhythm.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[[anchors]]\ntime = "09:00"\nname = "shared_learning"\n'
        f'meaning = "synthetic"\nallowed_tools = [{tools_toml}]\n',
        encoding="utf-8",
    )
    return path


def _load(path: Path, kind: str, **kwargs):
    if kind == "schedule":
        return load_schedules(path, **kwargs)["schedules"]
    if kind == "trigger":
        return load_schedules(path, **kwargs)["triggers"]
    return load_rhythm(path, **kwargs)


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_workspace_skill_name_in_allowed_tools_emits_skill_warning(tmp_path, kind, caplog):
    _make_skill(tmp_path, "async_jobs")
    path = _write_config(tmp_path, kind, ["write_file", "async_jobs"])
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        items = _load(path, kind)
    # Non-blocking: entry still loads with its declared tools preserved.
    assert len(items) == 1
    tools = items[0]["allowed_tools"] if isinstance(items[0], dict) else list(items[0].allowed_tools)
    if kind == "anchor":
        assert tools == []
        assert not caplog.records
        return
    assert tools == ["write_file", "async_jobs"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0]
    assert "async_jobs" in msg
    assert "skill" in msg.lower()
    assert "skills" in msg or "load_skill" in msg


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_retired_mcp_and_unknown_tool_emit_drift_warning(tmp_path, kind, caplog):
    _write_loom_toml(tmp_path, ("minimax", "minimax_coding"))
    path = _write_config(
        tmp_path,
        kind,
        ["openai__text_to_image", "minimax__text_to_image", "memorise"],
    )
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        items = _load(path, kind)
    assert len(items) == 1
    if kind == "anchor":
        assert items[0].allowed_tools == ()
        assert not caplog.records
        return
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("openai__text_to_image" in w and "openai" in w for w in warnings)
    assert any("memorise" in w for w in warnings)
    assert not any("minimax__text_to_image" in w for w in warnings)


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_valid_native_conditional_and_configured_mcp_tools_have_zero_warnings(
    tmp_path, kind, caplog
):
    _write_loom_toml(tmp_path, ("minimax", "minimax_coding"))
    # ``weather`` is both a workspace skill directory AND a native tool; native wins.
    _make_skill(tmp_path, "weather")
    valid_tools = [
        "read_file",
        "write_file",
        "list_dir",
        "probe_file",
        "see_image",
        "run_bash",
        "load_skill",
        "unload_skill",
        "fetch_url",
        "weather",
        "memorize",
        "recall",
        "pursuit_write",
        "spawn_agent",
        "weave_revise",
        "journal_append",
        "web_search",
        "image_generate",
        "tts_generate",
        "memory_attend",
        "agent_health",
        "request_model_tier",
        "clear_model_tier",
        "set_reasoning_effort",
        "send_discord_file",
        "create_discord_forum_post",
        "read_discord_forum",
        "minimax_coding__web_search",
        "minimax__text_to_image",
        "minimax__text_to_audio",
    ]
    path = _write_config(tmp_path, kind, valid_tools)
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        items = _load(path, kind)
    assert len(items) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_explicit_mcp_servers_kwarg_overrides_loom_toml(tmp_path, caplog):
    path = _write_config(tmp_path, "schedule", ["custom_srv__search", "other_srv__run"])
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        items = load_schedules(path, mcp_servers={"custom_srv"})["schedules"]
    assert len(items) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "other_srv__run" in warnings[0]
    assert "custom_srv__search" not in warnings[0]


def test_skill_allows_tools_drift_is_also_checked(tmp_path, caplog):
    _make_skill(tmp_path, "async_jobs")
    _make_skill(
        tmp_path,
        "demo",
        "---\nname: demo\nallows:\n  tools: [memorize, async_jobs, openai__text_to_image]\n---\n",
    )
    path = tmp_path / "autonomy" / "schedules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[[schedules]]\nname = "demo_sched"\ncron = "0 1 * * *"\n'
        'intent = "synthetic"\nskills = ["demo"]\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        items = load_schedules(path)["schedules"]
    assert len(items) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("async_jobs" in w and "skill" in w.lower() for w in warnings)
    assert any("openai__text_to_image" in w for w in warnings)


def test_known_native_tools_covers_all_defined_tools():
    """Keep KNOWN_NATIVE_TOOLS in lockstep with ToolDefinition factories."""
    root = Path(loom.__file__).parent
    defined: set[str] = set()
    for py in root.rglob("*.py"):
        if py.name == "mcp_client.py":
            continue
        text = py.read_text(encoding="utf-8")
        if "ToolDefinition(" not in text:
            continue
        defined.update(re.findall(r'\bname\s*=\s*"([a-z][a-z0-9_]+)"', text))
    # All conditional tools (including image_generate/tts_generate) must be included too.
    expected = defined | set(CONDITIONAL_TOOLS)
    assert expected <= KNOWN_NATIVE_TOOLS
    assert KNOWN_NATIVE_TOOLS == expected


def test_tracked_examples_have_no_tool_drift_warnings(caplog):
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    with caplog.at_level(logging.WARNING, logger="loom.autonomy"):
        sched_cfg = load_schedules(repo / "autonomy/schedules.example.toml", workspace=repo)
        assert sched_cfg["schedules"]
        rhythm_raw = tomllib.loads(
            (repo / "autonomy/circadian/rhythm.example.toml").read_text(encoding="utf-8")
        )
        for anchor in rhythm_raw.get("anchors", []):
            warn_allowed_tool_drift(
                f"rhythm.example.toml {anchor.get('name')!r}",
                anchor.get("allowed_tools", []),
                workspace=repo,
            )
    drift_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "allowed_tools" in r.getMessage()
    ]
    assert drift_warnings == []
