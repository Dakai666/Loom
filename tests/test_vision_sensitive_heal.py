"""Vision self-heal: when a provider rejects an image as sensitive content
(e.g. MiniMax error 1026, surfaced as a 500), Loom must not retry the
identical payload forever and must un-wedge the session by stripping the
offending image from history.

These tests pin the two pure helpers:

* ``providers._is_sensitive_content_rejection`` — classify the rejection
  from the exception, conservatively (a generic 500 / timeout is NOT it).
* ``session._strip_images_from_messages`` — replace image blocks in
  history with a text marker so the next turn is text-only.
"""

from __future__ import annotations

import pytest

from loom.core.cognition.providers import (
    SensitiveContentError,
    _is_sensitive_content_rejection,
)
from loom.core.session import _strip_images_from_messages


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


_MINIMAX_SENSITIVE = (
    "InternalServerError: Error code: 500 - {'type': 'error', 'error': "
    "{'type': 'api_error', 'message': \"input new_sensitive, messages[152]'s "
    "content[2] image is sensitive, please check your input (1026)\"}}"
)


class TestSensitiveDetection:
    def test_detects_minimax_sensitive_image(self) -> None:
        assert _is_sensitive_content_rejection(RuntimeError(_MINIMAX_SENSITIVE))

    def test_detects_by_error_code_1026(self) -> None:
        assert _is_sensitive_content_rejection(
            RuntimeError("rejected: content flagged (1026)")
        )

    def test_ignores_generic_500(self) -> None:
        assert not _is_sensitive_content_rejection(
            RuntimeError("Error code: 500 - internal server error, try again")
        )

    def test_ignores_timeout(self) -> None:
        assert not _is_sensitive_content_rejection(TimeoutError("read timeout"))

    def test_ignores_rate_limit(self) -> None:
        assert not _is_sensitive_content_rejection(
            RuntimeError("Error code: 429 - rate limit exceeded")
        )

    def test_sensitive_content_error_is_runtimeerror(self) -> None:
        # Session-layer catch relies on the typed class; keep it a
        # RuntimeError subclass so existing broad handlers still work.
        assert issubclass(SensitiveContentError, RuntimeError)


# ---------------------------------------------------------------------------
# History strip
# ---------------------------------------------------------------------------


def _img_block(ref: str = "/tmp/x.png") -> dict:
    return {
        "type": "image",
        "source": {
            "kind": "file", "ref": ref,
            "media_type": "image/png", "digest": "sha256:abc",
        },
    }


class TestStripImagesFromMessages:
    def test_replaces_image_keeps_text(self) -> None:
        msgs = [
            {"role": "user", "content": "plain"},
            {"role": "user", "content": [
                {"type": "text", "text": "看下這張"},
                _img_block(),
            ]},
        ]
        removed = _strip_images_from_messages(msgs)
        assert removed == 1
        # str message untouched
        assert msgs[0]["content"] == "plain"
        # image gone, text preserved, no image blocks remain
        blocks = msgs[1]["content"]
        assert all(b["type"] == "text" for b in blocks)
        assert any("看下這張" in b["text"] for b in blocks)

    def test_counts_multiple_images(self) -> None:
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "兩張"}, _img_block("/a.png"), _img_block("/b.png"),
            ]},
        ]
        assert _strip_images_from_messages(msgs) == 2
        assert all(
            b["type"] == "text" for b in msgs[0]["content"]
        )

    def test_no_images_returns_zero(self) -> None:
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert _strip_images_from_messages(msgs) == 0
        assert msgs[0]["content"] == "hi"

    def test_image_only_message_becomes_text_marker(self) -> None:
        msgs = [{"role": "user", "content": [_img_block()]}]
        removed = _strip_images_from_messages(msgs)
        assert removed == 1
        blocks = msgs[0]["content"]
        assert len(blocks) == 1 and blocks[0]["type"] == "text"
        assert blocks[0]["text"]  # non-empty marker


# ---------------------------------------------------------------------------
# Integration: stream_turn self-heals on SensitiveContentError
# ---------------------------------------------------------------------------


async def test_stream_turn_heals_on_sensitive_image(monkeypatch, tmp_path) -> None:
    """The wiring the unit tests can't reach: a provider raises
    SensitiveContentError on the first call; stream_turn must strip the
    image from history, drop a breadcrumb, and retry text-only — landing
    a normal response on the second call instead of wedging."""
    from types import SimpleNamespace

    from loom.core import session as session_module
    from loom.core.cognition.providers import LLMResponse, SensitiveContentError
    from loom.core.events import TurnDropped
    from loom.core.session import LoomSession

    calls = {"n": 0}

    async def flaky_stream_chat(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SensitiveContentError(
                "messages[152]'s content[2] image is sensitive (1026)"
            )
            yield "", None  # pragma: no cover (generator marker)
        # Second call: image is gone from history → succeed text-only.
        yield "好的，我改用文字繼續。", None
        yield "", LLMResponse(
            text="好的，我改用文字繼續。",
            tool_uses=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
            raw_message={"role": "assistant", "content": "好的，我改用文字繼續。"},
        )

    router = SimpleNamespace(
        stream_chat=flaky_stream_chat,
        native_max_tokens=lambda model: None,
    )
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: router)
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = LoomSession(
        model="MiniMax-M3",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )

    class FakeEpisodic:
        async def write(self, entry):
            return None

    session._memory = SimpleNamespace(episodic=FakeEpisodic())

    # User turn carrying an image (canonical list content).
    user_input = [
        {"type": "text", "text": "看下這張"},
        _img_block("/tmp/portrait.png"),
    ]

    events = []
    async for event in session.stream_turn(user_input):
        events.append(event)

    # Retried exactly once (two provider calls total).
    assert calls["n"] == 2
    # The heal emitted its breadcrumb drop event.
    assert any(
        isinstance(e, TurnDropped) and e.stop_reason == "sensitive_image_stripped"
        for e in events
    )
    # No image block remains anywhere in history (un-wedged).
    for m in session.messages:
        c = m.get("content")
        if isinstance(c, list):
            assert all(b.get("type") != "image" for b in c)
    # The breadcrumb note made it into history so the agent can explain.
    assert any(
        isinstance(m.get("content"), str) and "敏感內容" in m["content"]
        for m in session.messages
    )
