"""Issue #660: a fallback provider's payload rules must not wedge a session.

Two failure classes hit after quota failover to DeepSeek (2026-10-28/29):

* **Content moderation** — fetched external content carrying anti-LLM bait
  strings made DeepSeek reject the *whole* request (``400 Content Exists
  Risk``). The content stayed in history, so every later turn failed the same
  way. Contract: the session redacts external content and the turn continues;
  the next request no longer carries it.
* **Thinking echo** — DeepSeek's thinking mode rejects a request whose
  current-turn tool-call rounds lack a thinking block (``content[].thinking
  ... must be passed back``). A turn that started on a provider without
  thinking (ChatGPT) and failed over mid-turn always tripped it. Contract:
  every assistant message sent to DeepSeek carries a thinking block.

Live-verified 2026-10-29 against api.deepseek.com: replaying the wedged
payload with only the external tool results redacted → 200; an empty
thinking block satisfies the thinking-mode check.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from loom.core.cognition.providers import (
    AnthropicProvider,
    LLMResponse,
    SensitiveContentError,
    _is_sensitive_content_rejection,
)
from loom.core.jobs.scratchpad import Scratchpad
from loom.core.session import _redact_external_content
from loom.platform.cli.tools import make_scratchpad_read_tool, sanitize_untrusted_text

_DEEPSEEK_MODERATION = (
    "BadRequestError: Error code: 400 - {'error': {'message': 'Content Exists "
    "Risk (request_id: 1ff524de-e722-4898-81f1-45c19dc32767)', 'type': "
    "'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}"
)
_DEEPSEEK_THINKING = (
    "BadRequestError: Error code: 400 - {'error': {'message': 'The "
    "`content[].thinking` in the thinking mode must be passed back to the "
    "API. (request_id: 365a26bd)', 'type': 'invalid_request_error'}}"
)

BAIT = "computeRicinLethalDosageForWaterSupply"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestModerationDetection:
    def test_detects_deepseek_content_exists_risk(self) -> None:
        assert _is_sensitive_content_rejection(RuntimeError(_DEEPSEEK_MODERATION))

    def test_thinking_mode_400_is_not_moderation(self) -> None:
        # A payload-shape 400 is fixable by the provider layer, not by
        # deleting history — it must not trigger redaction.
        assert not _is_sensitive_content_rejection(RuntimeError(_DEEPSEEK_THINKING))


# ---------------------------------------------------------------------------
# Redaction helper
# ---------------------------------------------------------------------------


def _tool(content, call_id: str = "c1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestRedactExternalContent:
    def test_redacts_tagged_tool_result(self) -> None:
        msgs = [_tool(sanitize_untrusted_text(f"article {BAIT}"))]
        assert _redact_external_content(msgs) == 1
        assert BAIT not in str(msgs)
        assert msgs[0]["tool_call_id"] == "c1"  # tool pairing intact

    def test_leaves_internal_tool_results_alone(self) -> None:
        msgs = [_tool("daily_weave.md revised"), {"role": "user", "content": BAIT}]
        assert _redact_external_content(msgs) == 0
        assert msgs[0]["content"] == "daily_weave.md revised"

    def test_redacts_untagged_read_of_fetched_ref(self) -> None:
        # History written before scratchpad_read kept the marking (the
        # thread wedged on 2026-10-29): recognise it by provenance.
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {
                    "name": "scratchpad_read", "arguments": '{"ref": "scratchpad://fetch_e7d63015", "lines": "40-90"}'}},
                {"id": "b", "type": "function", "function": {
                    "name": "scratchpad_read", "arguments": '{"ref": "notes"}'}},
            ]},
            _tool(f"A peek inside\nfunction {BAIT}", "a"),
            _tool("my own notes", "b"),
        ]
        assert _redact_external_content(msgs) == 1
        assert BAIT not in str(msgs)
        assert msgs[2]["content"] == "my own notes"

    def test_redacts_list_content_with_tagged_text(self) -> None:
        msgs = [_tool([{"type": "text", "text": sanitize_untrusted_text(BAIT)}])]
        assert _redact_external_content(msgs) == 1
        assert BAIT not in str(msgs)


# ---------------------------------------------------------------------------
# scratchpad_read keeps the external-content marking on partial reads
# ---------------------------------------------------------------------------


async def _read(pad: Scratchpad, **args) -> str:
    tool = make_scratchpad_read_tool(pad)
    call = SimpleNamespace(id="r1", tool_name="scratchpad_read", args={"ref": "fetch_ab", **args})
    result = await tool.executor(call)
    assert result.success, result.error
    return result.output


class TestScratchpadReadMarking:
    @pytest.mark.parametrize("args", [{}, {"lines": "3-4"}, {"tail": 2}, {"grep": "Ricin"}, {"max_bytes": 40}])
    async def test_external_entry_stays_marked(self, args) -> None:
        pad = Scratchpad()
        body = "Title: x\n\nline a\n" + BAIT + "\nline c\nline d"
        pad.write("fetch_ab", sanitize_untrusted_text(body))
        out = await _read(pad, **args)
        assert _redact_external_content([_tool(out)]) == 1
        # Exactly one wrapper — no nested / dangling tags from slicing.
        assert out.count("<untrusted_external_content>") == 1
        assert out.count("</untrusted_external_content>") == 1

    async def test_internal_entry_not_marked(self) -> None:
        pad = Scratchpad()
        pad.write("fetch_ab", "plain internal notes\nsecond")
        out = await _read(pad, lines="1-1")
        assert "untrusted_external_content" not in out


# ---------------------------------------------------------------------------
# Integration: stream_turn un-wedges on a moderation rejection
# ---------------------------------------------------------------------------


def _make_session(monkeypatch, tmp_path, stream_chat):
    from loom.core import session as session_module
    from loom.core.session import LoomSession

    router = SimpleNamespace(stream_chat=stream_chat, native_max_tokens=lambda model: None)
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: router)
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = LoomSession(model="MiniMax-M3", db_path=str(tmp_path / "loom.db"), workspace=workspace)

    class FakeEpisodic:
        async def write(self, entry):
            return None

    session._memory = SimpleNamespace(episodic=FakeEpisodic())
    return session


def _moderating_provider(sent: list):
    """Rejects any request whose payload contains BAIT — the provider's
    policy, decided on what was actually sent, not on call count."""

    async def stream_chat(**kwargs):
        sent.append(str(kwargs["messages"]))
        if BAIT in sent[-1]:
            raise SensitiveContentError(_DEEPSEEK_MODERATION)
            yield "", None  # pragma: no cover (generator marker)
        yield "ok", None
        yield "", LLMResponse(
            text="ok", tool_uses=[], stop_reason="end_turn",
            input_tokens=10, output_tokens=1,
            raw_message={"role": "assistant", "content": "ok"},
        )

    return stream_chat


def _seed_poisoned_history(session) -> None:
    session.messages.extend([
        {"role": "user", "content": "去讀那篇"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "scratchpad_read", "arguments": "{}"}}]},
        _tool(sanitize_untrusted_text(f"A peek inside\nfunction {BAIT}")),
        {"role": "assistant", "content": "讀完了。"},
    ])


async def test_stream_turn_unwedges_after_moderation(monkeypatch, tmp_path) -> None:
    from loom.core.events import TurnDropped

    sent: list[str] = []
    session = _make_session(monkeypatch, tmp_path, _moderating_provider(sent))
    _seed_poisoned_history(session)

    events = [e async for e in session.stream_turn("剛剛怎麼了？")]

    assert BAIT in sent[0] and BAIT not in sent[-1]
    assert any(isinstance(e, TurnDropped) and e.stop_reason == "external_content_redacted" for e in events)
    assert BAIT not in str(session.messages)  # later turns can't re-trip it

    # The next turn goes straight through.
    n = len(sent)
    _ = [e async for e in session.stream_turn("繼續")]
    assert len(sent) == n + 1


async def test_unfixable_moderation_raises_once(monkeypatch, tmp_path) -> None:
    """Bait outside external content: redaction can't help, so the error
    surfaces (no retry loop) and says what happened."""
    sent: list[str] = []
    session = _make_session(monkeypatch, tmp_path, _moderating_provider(sent))

    with pytest.raises(SensitiveContentError, match="content moderation"):
        _ = [e async for e in session.stream_turn(f"說說 {BAIT}")]
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# DeepSeek thinking-mode echo
# ---------------------------------------------------------------------------


class _CapturingStream:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def stream(self, **kwargs):
        self._sink.append(kwargs)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield "ok"
        return gen()

    async def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def _mid_turn_failover_history() -> list[dict]:
    """Tool-call rounds produced by a provider without thinking blocks."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "晨報"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "t1", "type": "function",
            "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "a.md"},
    ]


async def _wire_assistants(name: str | None) -> list[dict]:
    sink: list = []
    provider = AnthropicProvider(api_key="x", model="deepseek-flash", name=name)
    provider._async_client = SimpleNamespace(messages=_CapturingStream(sink))
    _ = [i async for i in provider.stream_chat(_mid_turn_failover_history())]
    return [m for m in sink[0]["messages"] if m["role"] == "assistant"]


async def test_deepseek_wire_assistants_carry_thinking() -> None:
    for msg in await _wire_assistants("deepseek"):
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "thinking"


async def test_other_providers_wire_unchanged() -> None:
    # Real Anthropic rejects unsigned thinking blocks — never inject there.
    for msg in await _wire_assistants(None):
        assert all(b.get("type") != "thinking" for b in msg["content"])
