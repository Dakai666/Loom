from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def temp_theme_file(monkeypatch, tmp_path):
    from loom.platform.cli import theme_persist

    theme_dir = tmp_path / ".config" / "loom"
    theme_file = theme_dir / "theme.toml"
    monkeypatch.setattr(theme_persist, "_THEME_DIR", theme_dir)
    monkeypatch.setattr(theme_persist, "_THEME_FILE", theme_file)
    yield theme_persist

    from loom.platform.cli import theme

    importlib.reload(theme)


def _style_color(theme_obj, token: str) -> str:
    color = theme_obj.styles[token].color
    assert color is not None
    return color.get_truecolor().hex


def test_loom_theme_uses_saved_preference_on_import(temp_theme_file) -> None:
    from loom.platform.cli import theme

    temp_theme_file.save_preference("sunrise")

    reloaded = importlib.reload(theme)

    assert _style_color(reloaded.LOOM_THEME, "loom.accent") == "#ffd54f"
    assert _style_color(reloaded.LOOM_THEME, "loom.success") == "#81d4fa"


def test_load_preference_falls_back_for_unknown_theme(temp_theme_file) -> None:
    temp_theme_file._THEME_DIR.mkdir(parents=True)
    temp_theme_file._THEME_FILE.write_text("[theme]\nname = 'not-real'\n", encoding="utf-8")

    assert temp_theme_file.load_preference() == "parchment"


def test_active_palette_uses_saved_preference(temp_theme_file) -> None:
    from loom.platform.cli import theme

    temp_theme_file.save_preference("sunrise")

    reloaded = importlib.reload(theme)

    assert reloaded.active_palette()["accent"] == "#FFD54F"
    assert reloaded.active_palette()["surface"] == "#161b22"


def test_theme_command_is_registered_for_cli_completion() -> None:
    from loom.platform.cli.ui import SLASH_COMMANDS

    commands = [command for command, _description in SLASH_COMMANDS]

    assert "/theme" in commands
    assert "/theme sunrise" in commands


@pytest.mark.asyncio
async def test_theme_command_lists_available_themes(monkeypatch) -> None:
    from loom.platform.cli import main as cli_main

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(cli_main, "load_preference", lambda: "sunrise")
    monkeypatch.setattr(cli_main, "available_themes", lambda: ["parchment", "sunrise"])
    monkeypatch.setattr(
        cli_main.harness,
        "inline",
        lambda message, *, level="info": captured.append((message, level)),
    )

    await cli_main._handle_slash("/theme", SimpleNamespace())

    messages = [message for message, _level in captured]
    assert "[bold]Available themes[/bold]" in messages
    assert "  sunrise  ← currently active" in messages


@pytest.mark.asyncio
async def test_theme_command_persists_valid_theme(monkeypatch) -> None:
    from loom.platform.cli import main as cli_main

    saved: list[str] = []
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(cli_main, "load_preference", lambda: "parchment")
    monkeypatch.setattr(cli_main, "available_themes", lambda: ["parchment", "sunrise"])
    monkeypatch.setattr(cli_main, "save_preference", lambda name: saved.append(name))
    monkeypatch.setattr(
        cli_main.harness,
        "inline",
        lambda message, *, level="info": captured.append((message, level)),
    )

    await cli_main._handle_slash("/theme sunrise", SimpleNamespace())

    assert saved == ["sunrise"]
    assert captured == [
        ("Theme switched to [loom.accent]sunrise[/loom.accent]. Restart to apply.", "info")
    ]
