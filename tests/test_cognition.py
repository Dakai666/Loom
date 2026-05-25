"""
Tests for the Cognition Layer:
  - ContextBudget: token estimation, compression threshold, record_response
  - LLMRouter: provider registration, routing by model prefix, fallback
  - Anthropic message conversion helpers
  - ReflectionAPI: session_summary, recent_tool_calls, tool_success_rate
"""

import pytest
import pytest_asyncio
from unittest.mock import MagicMock

from loom.core.cognition.context import (
    ContextBudget,
    estimate_block_count,
    estimate_tokens,
)
from loom.core.cognition.providers import (
    CodexResponsesProvider,
    LLMResponse,
    OpenAIProvider,
    _to_anthropic_messages,
)
from loom.core.cognition.router import LLMRouter
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import SkillGenome, ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore


def _facade(conn) -> MemoryFacade:
    sem = SemanticMemory(conn)
    pr = ProceduralMemory(conn)
    return MemoryFacade(
        semantic=sem,
        procedural=pr,
        episodic=EpisodicMemory(conn),
        search=MemorySearch(sem, pr),
    )


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

    def test_cjk_weighted_heavier_than_ascii(self):
        """CJK ~1.5 char/token; ASCII ~4. 60 CJK ≈ 40 tokens, 60 ASCII = 15.

        Real-tokenizer numbers vary by model, but the relative weight
        must be correct or the footer drifts dramatically on Chinese
        sessions after compaction (the bug this guards)."""
        ascii_tokens = estimate_tokens("a" * 60)
        cjk_tokens = estimate_tokens("我" * 60)
        assert cjk_tokens > ascii_tokens
        # Within ~30% of the 1.5 char/token target.
        assert 30 <= cjk_tokens <= 50

    def test_mixed_text_weighted_per_char(self):
        """A 50/50 mix should sit between the pure-ASCII and pure-CJK
        estimates, not be dragged down to either extreme."""
        mixed = ("a" * 60) + ("我" * 60)
        # 60 ASCII → 15 tokens; 60 CJK → 40 tokens. Expect ~55.
        assert 45 <= estimate_tokens(mixed) <= 65


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
        b.record_response(500, 0)    # 500 remaining
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

    def test_record_response_zero_input_is_noop(self):
        """Provider returning input_tokens=0 (aborted stream / missing usage
        on MiniMax) must not zero out a real prior reading — otherwise
        should_compress() silently disarms and history grows unbounded."""
        b = ContextBudget(total_tokens=1000, compression_threshold=0.8)
        b.record_response(900, 50)
        assert b.used_tokens == 950
        assert b.should_compress() is True

        b.record_response(0, 0)               # phantom zero
        assert b.used_tokens == 950           # preserved
        assert b.should_compress() is True    # still armed

        b.record_response(0, 100)             # input still 0 — also no-op
        assert b.used_tokens == 950

        b.record_response(600, 30)            # real reading lands
        assert b.used_tokens == 630

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


class TestEstimateBlockCount:
    """Wire content-block estimation — what MiniMax counts against its 2013 cap."""

    def test_simple_user_assistant_pair(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        # 1 system + 1 user + 1 assistant text = 3 blocks
        assert estimate_block_count(msgs) == 3

    def test_assistant_with_parallel_tool_calls(self):
        """One assistant turn with N parallel tool calls fans out to
        1 text block + N tool_use blocks on the wire. This is the
        exact pattern that pushes the news-thread session over 2013."""
        msgs = [
            {
                "role": "assistant",
                "content": "let me check three things",
                "tool_calls": [
                    {"id": "a", "function": {"name": "run_bash", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "run_bash", "arguments": "{}"}},
                    {"id": "c", "function": {"name": "run_bash", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "tool", "tool_call_id": "b", "content": "ok"},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
        ]
        # 1 (assistant text) + 3 (tool_use) + 3 (tool_result) = 7
        assert estimate_block_count(msgs) == 7

    def test_assistant_tool_call_with_no_text(self):
        """Assistant message with tool_calls but no text body — Anthropic
        wire omits the text block, so we should count only tool_uses."""
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "function": {"name": "run_bash", "arguments": "{}"}},
                ],
            },
        ]
        assert estimate_block_count(msgs) == 1

    def test_user_with_list_content_blocks(self):
        """Anthropic-canonical user messages with embedded tool_result
        blocks count each block separately."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "ok"},
                ],
            },
        ]
        assert estimate_block_count(msgs) == 2

    def test_assistant_thinking_blocks_counted(self):
        """Reasoning models put _thinking_blocks back on the wire; the
        estimator must count them or it underreports on Anthropic/MiniMax
        reasoning-heavy sessions and lets the wire cap blindside us."""
        msgs = [
            {
                "role": "assistant",
                "content": "ok",
                "_thinking_blocks": [
                    {"type": "thinking", "thinking": "step 1"},
                    {"type": "thinking", "thinking": "step 2"},
                ],
                "tool_calls": [
                    {"id": "a", "function": {"name": "run_bash", "arguments": "{}"}},
                ],
            },
        ]
        # 2 thinking + 1 text + 1 tool_use = 4
        assert estimate_block_count(msgs) == 4

    def test_estimator_matches_to_anthropic_messages(self):
        """The estimator is a model of ``_to_anthropic_messages`` —
        a regression here means the two paths have drifted and the
        budget can either over- or under-report wire pressure."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "thinking out loud",
                "_thinking_blocks": [
                    {"type": "thinking", "thinking": "step 1"},
                ],
                "tool_calls": [
                    {"id": "a", "function": {"name": "run_bash", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "run_bash", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "tool", "tool_call_id": "b", "content": "ok"},
        ]
        # _to_anthropic_messages returns (system, body). System lives on
        # the top-level kwarg, not in messages, so it doesn't count
        # toward the per-request block budget — compare the *body* for
        # the contract here. (The estimator does count the system row,
        # but that one-block delta is noise against the 2013 cap.)
        _system, wire = _to_anthropic_messages(msgs[1:])
        wire_blocks = 0
        for m in wire:
            c = m.get("content")
            if isinstance(c, list):
                wire_blocks += len(c)
            elif c:
                wire_blocks += 1
        body_only_estimate = estimate_block_count(msgs[1:])
        assert body_only_estimate == wire_blocks


class TestContextBudgetBlocks:
    """Block-count dimension — guards against MiniMax 2013 hard cap."""

    def test_record_messages_updates_block_count(self):
        b = ContextBudget(total_tokens=1000)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        b.record_messages(msgs)
        assert b.block_count == 3

    def test_block_pressure_triggers_compaction(self):
        """A session well under the token budget can still need
        compaction if the wire block count is near MiniMax's cap.
        This is the failure mode that broke the news thread."""
        b = ContextBudget(
            total_tokens=200_000,
            compression_threshold=0.80,
            max_blocks=2000,
            block_threshold=0.85,
        )
        b.record_response(input_tokens=50_000, output_tokens=0)   # 25% tokens
        assert not b.should_compress()
        # Synthesise 1800 blocks (90% of max) — pure block pressure.
        b.block_count = 1800
        assert b.should_compress()
        assert b.block_bound is True

    def test_token_pressure_alone_still_triggers(self):
        b = ContextBudget(total_tokens=1000, compression_threshold=0.80, max_blocks=2000)
        b.record_response(900, 0)
        b.block_count = 10   # well below cap
        assert b.should_compress()
        assert b.block_bound is False

    def test_pressure_returns_max_of_two_dimensions(self):
        b = ContextBudget(total_tokens=1000, max_blocks=100)
        b.record_response(300, 0)        # 30% token
        b.block_count = 70                # 70% block
        assert b.pressure == pytest.approx(0.70)
        assert b.block_bound is True

    def test_format_pressure_annotates_block_bound(self):
        b = ContextBudget(total_tokens=1000, max_blocks=100)
        b.record_response(200, 0)
        b.block_count = 90
        s = b.format_pressure()
        assert "blocks" in s
        assert "90" in s

    def test_format_pressure_plain_when_token_bound(self):
        b = ContextBudget(total_tokens=1000, max_blocks=100)
        b.record_response(800, 0)
        b.block_count = 10
        s = b.format_pressure()
        assert "blocks" not in s
        assert "80" in s

    def test_update_block_count_does_not_touch_tokens(self):
        """sanitize/mask paths use update_block_count to keep block
        accounting fresh without overwriting the authoritative token
        reading from the last provider response."""
        b = ContextBudget(total_tokens=1000)
        b.record_response(500, 0)
        before_tokens = b.used_tokens
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        b.update_block_count(msgs)
        assert b.used_tokens == before_tokens
        assert b.block_count == 2

    def test_reset_clears_blocks(self):
        b = ContextBudget(total_tokens=1000)
        b.record_response(500, 0)
        b.block_count = 50
        b.reset()
        assert b.used_tokens == 0
        assert b.block_count == 0


# ---------------------------------------------------------------------------
# _to_anthropic_messages conversion
# ---------------------------------------------------------------------------

class TestToAnthropicMessages:
    """`_to_anthropic_messages` returns ``(system_text, messages)`` — system
    is hoisted to a top-level kwarg per Anthropic wire spec (DeepSeek
    endpoint is strict about this). Tests destructure accordingly."""

    def test_user_message_passthrough(self):
        msgs = [{"role": "user", "content": "Hello"}]
        system, result = _to_anthropic_messages(msgs)
        assert system is None
        assert result == [{"role": "user", "content": "Hello"}]

    def test_assistant_text_only(self):
        msgs = [{"role": "assistant", "content": "Hi there", "tool_calls": []}]
        _, result = _to_anthropic_messages(msgs)
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
        _, result = _to_anthropic_messages(msgs)
        content = result[0]["content"]
        tool_block = next(b for b in content if b.get("type") == "tool_use")
        assert tool_block["name"] == "read_file"
        assert tool_block["input"] == {"path": "foo.txt"}

    def test_tool_results_merged_into_user_message(self):
        msgs = [
            {"role": "tool", "tool_call_id": "tc1", "content": "result A"},
            {"role": "tool", "tool_call_id": "tc2", "content": "result B"},
        ]
        _, result = _to_anthropic_messages(msgs)
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
        _, result = _to_anthropic_messages(msgs)
        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_system_hoisted_to_top_level(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        system, result = _to_anthropic_messages(msgs)
        assert system == "you are helpful"
        # System must NOT appear in the messages array — DeepSeek rejects it
        assert all(m["role"] != "system" for m in result)
        assert result == [{"role": "user", "content": "hi"}]


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

    def test_routing_by_prefix_openai(self):
        router = LLMRouter()
        p_openai = self._make_mock_provider("openai")
        router.register(p_openai)
        assert router.get_provider("gpt-5.5") is p_openai
        assert router.get_provider("gpt-5.5-pro") is p_openai
        assert router.get_provider("openai/gpt-5.5") is p_openai

    def test_routing_by_prefix_codex(self):
        router = LLMRouter()
        p_default = self._make_mock_provider("minimax")
        p_codex = self._make_mock_provider("codex")
        router.register(p_default, default=True)
        router.register(p_codex, fallback=False)
        assert router.get_provider("codex/gpt-5.5") is p_codex
        assert router.get_provider("unknown") is p_default

    def test_openai_provider_strips_optional_prefix(self):
        provider = OpenAIProvider(api_key="test", model="openai/gpt-5.5")
        assert provider._api_model() == "gpt-5.5"

    def test_codex_provider_strips_prefix(self):
        provider = CodexResponsesProvider(model="codex/gpt-5.5")
        assert provider._api_model() == "gpt-5.5"

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
        api = ReflectionAPI(_facade(reflection_db))
        summary = await api.session_summary("empty")
        assert "No activity" in summary

    @pytest.mark.asyncio
    async def test_session_summary_with_entries(self, reflection_db):
        facade = _facade(reflection_db)
        ep = facade.episodic
        api = ReflectionAPI(facade)

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
        facade = _facade(reflection_db)
        ep = facade.episodic
        api = ReflectionAPI(facade)

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
        facade = _facade(reflection_db)
        ep = facade.episodic
        api = ReflectionAPI(facade)

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
        api = ReflectionAPI(_facade(reflection_db))
        report = await api.skill_health_report()
        assert report == []

    @pytest.mark.asyncio
    async def test_skill_health_report_with_skills(self, reflection_db):
        facade = _facade(reflection_db)
        pr = facade.procedural
        api = ReflectionAPI(facade)

        await pr.upsert(SkillGenome(
            name="refactor", body="extract functions", confidence=0.85,
            usage_count=5, tags=["python"]
        ))
        report = await api.skill_health_report()
        assert len(report) == 1
        assert report[0]["name"] == "refactor"
        assert report[0]["confidence"] == 0.85
