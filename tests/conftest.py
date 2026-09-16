"""
Test-suite conftest.

Pins the user timezone to UTC so timestamp-formatting assertions are
deterministic regardless of which ``loom.toml`` the runner discovers.
Tests that need a different zone should monkeypatch
``loom.core.timezone._USER_ZONE`` themselves.
"""

from __future__ import annotations

import zoneinfo

import loom.core.timezone as _tz_module

_tz_module._USER_ZONE = zoneinfo.ZoneInfo("UTC")


import pytest

import loom.autonomy.circadian.journal as _journal_module


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_circadian_dirs: keep the shipped journal/dreams default dirs "
        "(for tests that assert the defaults themselves; must not write)",
    )


@pytest.fixture(autouse=True)
def _isolate_circadian_dirs(request, tmp_path, monkeypatch):
    """Redirect the circadian journal/dreams default dirs into ``tmp_path``.

    Those defaults are relative to the repo cwd — i.e. the agent's LIVE runtime
    tree. Code paths that take no dir argument (the daemon's
    ``_run_scheduled_*`` hooks) otherwise append fake reports into the real
    dream file on every suite run (#528, seen 2026-09-13).
    """
    if request.node.get_closest_marker("live_circadian_dirs"):
        return
    monkeypatch.setattr(
        _journal_module, "DEFAULT_DREAMS_DIR", tmp_path / "autonomy/circadian/dreams",
    )
    monkeypatch.setattr(
        _journal_module, "DEFAULT_JOURNAL_DIR", tmp_path / "autonomy/circadian/journal",
    )
