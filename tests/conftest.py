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
