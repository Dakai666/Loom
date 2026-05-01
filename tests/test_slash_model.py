"""
Regression test for the ``/model`` slash command (#272 follow-up).

Bug: ``main._handle_slash`` had two independent ``if command == ...``
chains; ``/model`` matched the first chain at L742 but execution then
continued into the second chain (which started with another bare ``if``
at L766), and the trailing ``else`` at L1000 fired with
``Unknown command '/model'``.

Symptom the user observed::

    you › /model deepseek-v4-pro
    Model switched to: deepseek-v4-pro
    Unknown command '/model'. Type /help for help.

Fix: change the second chain's leading ``if`` to ``elif`` so the dispatcher
becomes a single chain whose ``else`` only fires when no command matches.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom.platform.cli import main as cli_main


@pytest.fixture
def capture_console(monkeypatch):
    """Capture all console.print payloads as plain strings."""
    captured: list[str] = []

    def _print(*args, **kwargs) -> None:
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(cli_main.console, "print", _print)
    return captured


def _stub_session(switch_ok: bool = True) -> SimpleNamespace:
    """Minimal session with the surface ``/model`` reads."""
    return SimpleNamespace(
        model="minimax-m2.7",
        router=SimpleNamespace(providers=["anthropic", "deepseek"]),
        set_model=lambda model: switch_ok,
        # Other slash branches reach for these — provide no-op stubs so the
        # full dispatcher walk doesn't AttributeError on unrelated paths.
        current_personality=None,
        _stack=MagicMock(),
        _last_think="",
    )


class TestSlashModel:
    async def test_model_switch_does_not_print_unknown_command(
        self, capture_console
    ) -> None:
        """The bug: dispatcher fell through to the else branch after a
        successful /model command. Output must NOT contain 'Unknown command'."""
        session = _stub_session()
        await cli_main._handle_slash("/model deepseek-v4-pro", session)

        joined = "\n".join(capture_console)
        assert "Model switched to" in joined
        assert "Unknown command" not in joined, (
            "Dispatcher fell through to the else branch — /model bug regressed.\n"
            f"Captured output:\n{joined}"
        )

    async def test_model_query_without_arg_does_not_print_unknown(
        self, capture_console
    ) -> None:
        """Bare ``/model`` (no arg) shows the providers list and must
        likewise not trigger the 'Unknown command' fallthrough."""
        session = _stub_session()
        await cli_main._handle_slash("/model", session)

        joined = "\n".join(capture_console)
        assert "Current model" in joined
        assert "Unknown command" not in joined

    async def test_model_switch_failure_shows_error_not_unknown(
        self, capture_console
    ) -> None:
        """When set_model returns False, the user sees the targeted
        'Could not switch' message — never the generic 'Unknown command'."""
        session = _stub_session(switch_ok=False)
        await cli_main._handle_slash("/model nonexistent-model", session)

        joined = "\n".join(capture_console)
        assert "Could not switch" in joined
        assert "Unknown command" not in joined

    async def test_actually_unknown_command_still_caught(
        self, capture_console
    ) -> None:
        """The else branch must still fire for genuinely unknown commands —
        we fixed the over-firing, not removed it."""
        session = _stub_session()
        await cli_main._handle_slash("/zzz-not-a-real-command", session)

        joined = "\n".join(capture_console)
        assert "Unknown command" in joined
