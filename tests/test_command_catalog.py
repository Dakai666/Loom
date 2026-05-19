from __future__ import annotations

from pathlib import Path


def test_personality_commands_are_built_from_directory(tmp_path: Path) -> None:
    from loom.platform.command_catalog import personality_slash_commands

    personalities = tmp_path / "personalities"
    personalities.mkdir()
    (personalities / "operator.md").write_text("operator", encoding="utf-8")
    (personalities / "barista.md").write_text("barista", encoding="utf-8")

    assert personality_slash_commands(personalities) == [
        "/personality",
        "/personality off",
        "/personality barista",
        "/personality operator",
    ]


def test_personality_commands_normalize_filename_case(tmp_path: Path) -> None:
    from loom.platform.command_catalog import personality_slash_commands

    personalities = tmp_path / "personalities"
    personalities.mkdir()
    (personalities / "Sisi_Tarot_Mood.md").write_text("tarot", encoding="utf-8")

    assert personality_slash_commands(personalities) == [
        "/personality",
        "/personality off",
        "/personality sisi_tarot_mood",
    ]


def test_cli_slash_catalog_includes_dynamic_personality_files() -> None:
    from loom.platform.cli.ui import SLASH_COMMANDS

    commands = [command for command, _description in SLASH_COMMANDS]

    assert "/personality barista" in commands


def test_discord_personality_command_uses_autocomplete_not_static_choices() -> None:
    from loom.platform.discord.bot import LoomDiscordBot

    bot = LoomDiscordBot(model="claude-opus-4-7", db_path="/tmp/loom-test.db")
    command = next(cmd for cmd in bot._tree.get_commands() if cmd.name == "loom-personality")
    param = next(param for param in command.parameters if param.name == "name")

    assert not param.choices
    assert param.autocomplete is not None


def test_discord_help_lists_dynamic_personalities() -> None:
    from loom.platform.discord.bot import LoomDiscordBot

    bot = LoomDiscordBot(model="claude-opus-4-7", db_path="/tmp/loom-test.db")

    assert "barista" in bot._cmd_help()
