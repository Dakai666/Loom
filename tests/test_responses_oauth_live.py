from __future__ import annotations

import os

import pytest

from loom.core.cognition.providers import CodexResponsesProvider, XAIResponsesProvider


pytestmark = pytest.mark.skipif(
    os.environ.get("LOOM_LIVE_OAUTH_SMOKE") != "1",
    reason="set LOOM_LIVE_OAUTH_SMOKE=1 to run real OAuth provider smoke tests",
)


@pytest.mark.asyncio
async def test_live_codex_oauth_small_prompt() -> None:
    provider = CodexResponsesProvider()
    response = await provider.chat(
        [{"role": "user", "content": "Reply with exactly: loom-codex-smoke"}],
        max_tokens=128,
    )

    assert response.text
    assert "loom-codex-smoke" in response.text.lower()


@pytest.mark.asyncio
async def test_live_xai_oauth_small_prompt() -> None:
    provider = XAIResponsesProvider()
    response = await provider.chat(
        [{"role": "user", "content": "Reply with exactly: loom-xai-smoke"}],
        max_tokens=128,
    )

    assert response.text
    assert "loom-xai-smoke" in response.text.lower()


@pytest.mark.asyncio
async def test_live_codex_oauth_large_context_does_not_silently_stall() -> None:
    provider = CodexResponsesProvider()
    long_context = "\n".join(f"line {idx}: implementation context" for idx in range(600))
    response = await provider.chat(
        [
            {"role": "system", "content": "Answer concisely."},
            {"role": "user", "content": f"{long_context}\n\nReturn the final word: codex-large-ok"},
        ],
        max_tokens=256,
    )

    assert response.text
    assert "codex-large-ok" in response.text.lower()


@pytest.mark.asyncio
async def test_live_xai_oauth_large_context_does_not_silently_stall() -> None:
    provider = XAIResponsesProvider()
    long_context = "\n".join(f"line {idx}: implementation context" for idx in range(600))
    response = await provider.chat(
        [
            {"role": "system", "content": "Answer concisely."},
            {"role": "user", "content": f"{long_context}\n\nReturn the final word: xai-large-ok"},
        ],
        max_tokens=256,
    )

    assert response.text
    assert "xai-large-ok" in response.text.lower()
