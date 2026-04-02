"""
Tests for the Cognition Layer:
  - ContextBudget: token estimation, compression threshold, record_response
  - LLMRouter: provider registration, routing by model prefix, fallback
  - MiniMaxProvider: XML fallback parser, tool_call normalisation
  - AnthropicProvider: _to_anthropic_messages conversion (no real API calls)
  - ReflectionAPI: session_summary, recent_tool_calls, tool_success_rate
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from loom.core.cognition.context import ContextBudget, estimate_tokens
from loom.core.cognition.providers import (
    ToolUse, LLMResponse,
    MiniMaxProvider, AnthropicProvider,
    _parse_xml_tool_calls, _to_anthropic_messages,
)
from loom.core.cognition.router import LLMRouter
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.procedural import SkillGenome, ProceduralMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# ContextBudget
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") >= 1

    def test_short_string(self):
        # 4 chars ~ 1 token
        assert estimate_tokens("abcd") == 1

    def test_longer_string(self):
        assert estimate_tokens("a" * 400) == 100

    def test_dict_serializes(self):
        obj = {"key": "value", "number": 42}
        assert estimate_tokens(obj) >= 1


class TestContextBudget:
    def test_initial_state(self):
        b = ContextBudget(total_tokens=1000)
        assert b.used_tokens == 0
        assert b.remaining == 1000
        assert b.usage_fraction == 0.0

    def test_record_response_accumulates(self):
        b = ContextBudget(total_tokens=1000)
        b.record_response(input_tokens=200, output_tokens=100)
        assert b.used_tokens == 300

    def test_should_compress_false_below_threshold(self):
        b = ContextBudget(total_tokens=1000, compression_threshold=0.8)
        b.record_response(700, 0)
        assert b.should_compress() is False

    def test_should_compress_true_at_threshold(self):
        b = ContextBudget(total_tokens=1000, compression_threshold=0.8)
        b.record_response(800, 0)
        assert b.should_compress() is True

    def test_should_compress_true_above_threshold(self):
        b = ContextBudget(total_tokens=1000, compression_threshold=0.8)
        b.record_response(900, 0)
        assert b.should_compress() is True

    def test_fits_when_within_budget(self):
        b = ContextBudget(total_tokens=1000)
        b.record_response(0, 500)    # 500 remaining
        assert b.fits("a" * 400) is True  # ~100 tokens

    def test_not_fits_when_over_budget(self):
        b = ContextBudget(total_tokens=100)
        b.record_response(90, 0)     # 10 remaining
        assert b.fits("a" * 400) is False  # ~100 tokens needed

    def test_record_messages_recounts(self):
        b = ContextBudget(total_tokens=1000)
        b.record_response(500, 0)
        b.record_messages([{"role": "user", "content": "hi"}])
        # After recount, used_tokens reflects actual messages, not accumulated
        assert b.used_tokens < 500

    def test_add_increments(self):
        b = ContextBudget(total_tokens=1000)
        b.add(100)
        b.add(50)
        assert b.used_tokens == 150

    def test_reset(self):
        b = ContextBudget(total_tokens=1000)
        b.record_response(500, 0)
        b.reset()
        assert b.used_tokens == 0

    def test_remaining_never_negative(self):
        b = ContextBudget(total_tokens=100)
        b.record_response(200, 0)   # over budget
        assert b.remaining == 0

    def test_str_representation(self):
        b = ContextBudget(total_tokens=1000, compression_threshold=0.8)
        b.record_response(850, 0)
        s = str(b)
        assert "compress" in s
        assert "850" in s


# ---------------------------------------------------------------------------
# XML tool call parser
# ---------------------------------------------------------------------------

class TestParseXmlToolCalls:
    def test_single_tool_call(self):
        xml = """
        <minimax:tool_call>
          <invoke name="read_file">
            <parameter name="path">/tmp/foo.txt</parameter>
          </invoke>
        </minimax:tool_call>
        """
        uses = _parse_xml_tool_calls(xml)
        assert len(uses) == 1
        assert uses[0].name == "read_file"
        assert uses[0].args["path"] == "/tmp/foo.txt"

    def test_multiple_tool_calls(self):
        xml = """
        <minimax:tool_call>
          <invoke name="list_dir">
            <parameter name="path">.</parameter>
          </invoke>
        </minimax:tool_call>
        <minimax:tool_call>
          <invoke name="run_bash">
            <parameter name="command">echo hello</parameter>
          </invoke>
        </minimax:tool_call>
        """
        uses = _parse_xml_tool_calls(xml)
        assert len(uses) == 2
        names = {u.name for u in uses}
        assert names == {"list_dir", "run_bash"}

    def test_no_xml_returns_empty(self):
        uses = _parse_xml_tool_calls("Just plain text, no tool calls here.")
        assert uses == []

    def test_json_values_are_deserialized(self):
        xml = """
        <minimax:tool_call>
          <invoke name="some_tool">
            <parameter name="count">42</parameter>
            <parameter name="flag">true</parameter>
          </invoke>
        </minimax:tool_call>
        """
        uses = _parse_xml_tool_calls(xml)
        assert uses[0].args["count"] == 42
        assert uses[0].args["flag"] is True

    def test_each_call_gets_unique_id(self):
        xml = """
        <minimax:tool_call><invoke name="t1"><parameter name="a">1</parameter></invoke></minimax:tool_call>
        <minimax:tool_call><invoke name="t2"><parameter name="b">2</parameter></invoke></minimax:tool_call>
        """
        uses = _parse_xml_tool_calls(xml)
        assert uses[0].id != uses[1].id


# ---------------------------------------------------------------------------
# _to_anthropic_messages conversion
# ---------------------------------------------------------------------------

class TestToAnthropicMessages:
    def test_user_message_passthrough(self):
        msgs = [{"role": "user", "content": "Hello"}]
        result = _to_anthropic_messages(msgs)
        assert result == [{"role": "user", "content": "Hello"}]

    def test_assistant_text_only(self):
        msgs = [{"role": "assistant", "content": "Hi there", "tool_calls": []}]
        result = _to_anthropic_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        # Content has a text block
        assert any(b.get("type") == "text" for b in result[0]["content"])

    def test_assistant_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "foo.txt"}',
                    },
                }],
            }
        ]
        result = _to_anthropic_messages(msgs)
        content = result[0]["content"]
        tool_block = next(b for b in content if b.get("type") == "tool_use")
        assert tool_block["name"] == "read_file"
        assert tool_block["input"] == {"path": "foo.txt"}

    def test_tool_results_merged_into_user_message(self):
        msgs = [
            {"role": "tool", "tool_call_id": "tc1", "content": "result A"},
            {"role": "tool", "tool_call_id": "tc2", "content": "result B"},
        ]
        result = _to_anthropic_messages(msgs)
        # Both tool results collapsed into one user message
        assert len(result) == 1
        assert result[0]["role"] == "user"
        ids = [b["tool_use_id"] for b in result[0]["content"]]
        assert "tc1" in ids and "tc2" in ids

    def test_mixed_conversation(self):
        msgs = [
            {"role": "user", "content": "Do X"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "run_bash", "arguments": '{"command":"ls"}'}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file.txt"},
            {"role": "assistant", "content": "Done."},
        ]
        result = _to_anthropic_messages(msgs)
        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# LLMRouter
# ---------------------------------------------------------------------------

class TestLLMRouter:
    def _make_mock_provider(self, name: str):
        p = MagicMock()
        p.name = name
        return p

    def test_register_and_default(self):
        router = LLMRouter()
        p = self._make_mock_provider("minimax")
        router.register(p, default=True)
        assert "minimax" in router.providers

    def test_routing_by_prefix_minimax(self):
        router = LLMRouter()
        p_mm = self._make_mock_provider("minimax")
        p_ant = self._make_mock_provider("anthropic")
        router.register(p_mm)
        router.register(p_ant)
        assert router.get_provider("MiniMax-M2.7") is p_mm
        assert router.get_provider("MiniMax-M2.5") is p_mm

    def test_routing_by_prefix_claude(self):
        router = LLMRouter()
        p_mm = self._make_mock_provider("minimax")
        p_ant = self._make_mock_provider("anthropic")
        router.register(p_mm)
        router.register(p_ant)
        assert router.get_provider("claude-sonnet-4-6") is p_ant

    def test_fallback_to_default(self):
        router = LLMRouter()
        p = self._make_mock_provider("minimax")
        router.register(p, default=True)
        assert router.get_provider("unknown-model-xyz") is p

    def test_no_provider_raises(self):
        router = LLMRouter()
        with pytest.raises(RuntimeError, match="No provider"):
            router.get_provider("some-model")

    def test_first_registered_becomes_default(self):
        router = LLMRouter()
        p1 = self._make_mock_provider("minimax")
        p2 = self._make_mock_provider("anthropic")
        router.register(p1)
        router.register(p2)
        # p1 was first, should be default
        assert router.get_provider("unknown") is p1

    @pytest.mark.asyncio
    async def test_chat_delegates_to_provider(self):
        router = LLMRouter()
        mock_resp = LLMResponse(text="hello", tool_uses=[], stop_reason="end_turn")
        p = MagicMock()
        p.name = "minimax"
        p.chat = MagicMock(return_value=mock_resp)

        # Make chat awaitable
        import asyncio
        async def async_chat(**kwargs):
            return mock_resp
        p.chat = async_chat

        router.register(p, default=True)
        result = await router.chat(
            model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.text == "hello"


# ---------------------------------------------------------------------------
# ReflectionAPI
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def reflection_db(tmp_path):
    store = SQLiteStore(str(tmp_path / "reflect.db"))
    await store.initialize()
    async with store.connect() as db:
        yield db


class TestReflectionAPI:
    @pytest.mark.asyncio
    async def test_session_summary_no_entries(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)
        summary = await api.session_summary("empty")
        assert "No activity" in summary

    @pytest.mark.asyncio
    async def test_session_summary_with_entries(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)

        await ep.write(EpisodicEntry(
            session_id="s1", event_type="message", content="User: hello"
        ))
        for name, ok in [("read_file", True), ("run_bash", False)]:
            await ep.write(EpisodicEntry(
                session_id="s1", event_type="tool_result",
                content=f"Tool '{name}' {'ok' if ok else 'failed'}",
                metadata={"tool_name": name, "success": ok},
            ))

        summary = await api.session_summary("s1")
        assert "1 user message" in summary
        assert "2 tool call" in summary

    @pytest.mark.asyncio
    async def test_recent_tool_calls_order(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)

        for i in range(5):
            await ep.write(EpisodicEntry(
                session_id="s2", event_type="tool_result",
                content=f"step {i}",
                metadata={"tool_name": f"tool_{i}", "success": True},
            ))

        recent = await api.recent_tool_calls("s2", n=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["content"] == "step 4"

    @pytest.mark.asyncio
    async def test_tool_success_rate(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)

        for ok in [True, True, False]:
            await ep.write(EpisodicEntry(
                session_id="s3", event_type="tool_result",
                content="x",
                metadata={"tool_name": "run_bash", "success": ok},
            ))

        rates = await api.tool_success_rate("s3")
        assert "run_bash" in rates
        assert abs(rates["run_bash"] - 2/3) < 0.01

    @pytest.mark.asyncio
    async def test_skill_health_report_empty(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)
        report = await api.skill_health_report()
        assert report == []

    @pytest.mark.asyncio
    async def test_skill_health_report_with_skills(self, reflection_db):
        ep = EpisodicMemory(reflection_db)
        pr = ProceduralMemory(reflection_db)
        api = ReflectionAPI(ep, pr)

        await pr.upsert(SkillGenome(
            name="refactor", body="extract functions", confidence=0.85,
            usage_count=5, tags=["python"]
        ))
        report = await api.skill_health_report()
        assert len(report) == 1
        assert report[0]["name"] == "refactor"
        assert report[0]["confidence"] == 0.85
