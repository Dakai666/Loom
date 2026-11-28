"""Skill tests are offline; individual cases supply HTTP response fixtures."""

import pytest
import requests


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network request in offline skill tests")

    monkeypatch.setattr(requests.sessions.Session, "request", denied)
