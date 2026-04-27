from __future__ import annotations

import json
from pathlib import Path

import pytest_asyncio

from loom.core.agent.subagent import SubAgentConfig, run_subagent
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.jobs.scratchpad import Scratchpad
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import make_memorize_tool


class _FakeRouter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)

    async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
        yield "", next(self._responses)

    def format_tool_result(
        self,
        model: str,
        tool_use_id: str,
        content: str,
        success: bool = True,
    ) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": content,
        }


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    store = SQLiteStore(str(tmp_path / "test.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def semantic(db_conn):
    return SemanticMemory(db_conn)


@pytest_asyncio.fixture
async def episodic(db_conn):
    return EpisodicMemory(db_conn)


@pytest_asyncio.fixture
async def procedural(db_conn):
    return ProceduralMemory(db_conn)


class TestSubAgentMemorize:
    async def test_memorize_retags_source_with_agent_id(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        registry = ToolRegistry()
        relational = RelationalMemory(semantic._db)
        facade = MemoryFacade(
            semantic=semantic, procedural=procedural,
            relational=relational, episodic=episodic,
            search=MemorySearch(semantic, procedural),
        )
        registry.register(make_memorize_tool(facade))
        router = _FakeRouter([
            LLMResponse(
                text=None,
                tool_uses=[
                    ToolUse(
                        id="toolu_1",
                        name="memorize",
                        args={"key": "project:fact", "value": "subagent learned this"},
                    )
                ],
                stop_reason="tool_use",
                raw_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "memorize",
                                "arguments": (
                                    '{"key":"project:fact",'
                                    '"value":"subagent learned this"}'
                                ),
                            },
                        }
                    ],
                },
            ),
            LLMResponse(
                text="done",
                tool_uses=[],
                stop_reason="end_turn",
                raw_message={"role": "assistant", "content": "done"},
            ),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Remember one fact and finish.",
                model="gpt-test",
                allowed_tools=["memorize"],
                agent_id="sub-123",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is True
        entry = await semantic.get("project:fact")
        assert entry is not None
        assert entry.source == "agent:sub-123"


def _make_failing_tool() -> ToolDefinition:
    """A SAFE tool that always fails — used to simulate tool errors in the loop."""

    async def _always_fail(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=False, error="simulated failure for test",
            failure_type="execution_error",
        )

    return ToolDefinition(
        name="always_fail",
        description="Test tool that always fails.",
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability(0),
        input_schema={"type": "object", "properties": {}},
        executor=_always_fail,
        tags=["test"],
    )


def _tool_use_response(tool_id: str, preamble_text: str) -> LLMResponse:
    """Assistant emits a short free-text preamble then calls always_fail."""
    return LLMResponse(
        text=None,
        tool_uses=[ToolUse(id=tool_id, name="always_fail", args={})],
        stop_reason="tool_use",
        raw_message={
            "role": "assistant",
            "content": [
                {"type": "text", "text": preamble_text},
                {"type": "tool_use", "id": tool_id, "name": "always_fail", "input": {}},
            ],
            "tool_calls": [
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": "always_fail", "arguments": "{}"},
                }
            ],
        },
    )


class TestSubAgentFailureContext:
    """Regression tests for Issue #192 P0 — failure context + scratchpad."""

    async def test_max_turns_failure_populates_context_and_scratchpad(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        registry = ToolRegistry()
        registry.register(_make_failing_tool())

        # Two tool_use turns — both call the failing tool. max_turns=2 means the
        # loop exits via the max_turns branch, not end_turn.
        router = _FakeRouter([
            _tool_use_response("call_1", "first attempt: trying to resolve X"),
            _tool_use_response("call_2", "second attempt: retrying with different approach"),
        ])

        scratchpad = Scratchpad()

        result = await run_subagent(
            SubAgentConfig(
                task="Do the impossible.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=2,
                agent_id="sub-fail",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            scratchpad=scratchpad,
        )

        # SubAgentResult carries structured failure context
        assert result.success is False
        assert result.turns_used == 2
        assert result.tool_calls == 2
        assert result.last_tool_name == "always_fail"
        assert result.last_tool_error == "simulated failure for test"
        assert result.partial_output is not None
        assert "first attempt" in result.partial_output
        assert "second attempt" in result.partial_output
        assert "max_turns limit" in (result.error or "")

        # Scratchpad ref populated with full structured payload
        ref = "subagent_failure:sub-fail"
        assert ref in scratchpad
        payload = json.loads(scratchpad.read(ref))
        assert payload["agent_id"] == "sub-fail"
        assert payload["turns_used"] == 2
        assert payload["tool_calls"] == 2
        assert payload["max_turns"] == 2
        assert payload["last_tool_name"] == "always_fail"
        assert payload["last_tool_error"] == "simulated failure for test"
        assert "second attempt" in (payload["partial_output"] or "")

    async def test_success_path_leaves_failure_fields_none(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Successful sub-agent does not populate failure context or scratchpad."""
        registry = ToolRegistry()
        router = _FakeRouter([
            LLMResponse(
                text="done",
                tool_uses=[],
                stop_reason="end_turn",
                raw_message={"role": "assistant", "content": "done"},
            ),
        ])
        scratchpad = Scratchpad()

        result = await run_subagent(
            SubAgentConfig(
                task="Trivial task.",
                model="gpt-test",
                allowed_tools=[],
                max_turns=5,
                agent_id="sub-ok",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            scratchpad=scratchpad,
        )

        assert result.success is True
        assert result.output == "done"
        assert result.last_tool_name is None
        assert result.last_tool_error is None
        assert result.partial_output is None
        assert scratchpad.list_refs() == []

    async def test_failure_without_scratchpad_still_populates_result(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """When no scratchpad is provided, failure context must still be on the result."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("call_1", "only attempt"),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Task without scratchpad.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=1,
                agent_id="sub-no-pad",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            # scratchpad omitted
        )

        assert result.success is False
        assert result.last_tool_name == "always_fail"
        assert result.last_tool_error == "simulated failure for test"
        assert result.partial_output is not None
        assert "only attempt" in result.partial_output


def _make_named_failing_tool(name: str) -> ToolDefinition:
    """Like _make_failing_tool but with a configurable name (for tool_loop tests)."""

    async def _always_fail(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=False, error=f"{name} simulated failure",
            failure_type="execution_error",
        )

    return ToolDefinition(
        name=name,
        description=f"Test tool {name} that always fails.",
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability(0),
        input_schema={"type": "object", "properties": {}},
        executor=_always_fail,
        tags=["test"],
    )


def _tool_use_response_for(tool_name: str, call_id: str, preamble_text: str) -> LLMResponse:
    """Variant of _tool_use_response that targets a specific tool name."""
    return LLMResponse(
        text=None,
        tool_uses=[ToolUse(id=call_id, name=tool_name, args={})],
        stop_reason="tool_use",
        raw_message={
            "role": "assistant",
            "content": [
                {"type": "text", "text": preamble_text},
                {"type": "tool_use", "id": call_id, "name": tool_name, "input": {}},
            ],
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
    )


def _tool_use_response_no_text(call_id: str) -> LLMResponse:
    """Tool call with no free-text preamble — used for no_progress classification."""
    return LLMResponse(
        text=None,
        tool_uses=[ToolUse(id=call_id, name="always_fail", args={})],
        stop_reason="tool_use",
        raw_message={
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": "always_fail", "input": {}},
            ],
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "always_fail", "arguments": "{}"},
                }
            ],
        },
    )


class TestSubAgentFailureCodes:
    """Regression tests for Issue #193 P1 — failure_code / recovery_suggestion / error_context."""

    async def test_tool_loop_detected_on_three_consecutive_same_tool_failures(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "try 1"),
            _tool_use_response("c2", "try 2"),
            _tool_use_response("c3", "try 3"),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Task.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=3,
                agent_id="sub-loop",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is False
        # Verify the causal signals first, then the classification derived from them.
        assert result.error_context["tool_failure_counts"] == {"always_fail": 3}
        assert result.error_context["max_consecutive_failures"] == 3
        assert result.error_context["stuck_tool"] == "always_fail"
        assert result.failure_code == "tool_loop"
        assert "always_fail" in (result.recovery_suggestion or "")

    async def test_max_turns_partial_when_failures_not_consecutive(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Alternating failures across two tools → no single tool stuck, partial output present."""
        registry = ToolRegistry()
        registry.register(_make_named_failing_tool("tool_a"))
        registry.register(_make_named_failing_tool("tool_b"))
        router = _FakeRouter([
            _tool_use_response_for("tool_a", "c1", "trying A"),
            _tool_use_response_for("tool_b", "c2", "trying B"),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Task.",
                model="gpt-test",
                allowed_tools=["tool_a", "tool_b"],
                max_turns=2,
                agent_id="sub-partial",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is False
        # Verify the causal signals first, then the classification derived from them.
        assert result.partial_output is not None
        assert "trying A" in result.partial_output
        assert "trying B" in result.partial_output
        assert result.error_context["tool_failure_counts"] == {"tool_a": 1, "tool_b": 1}
        assert result.error_context["max_consecutive_failures"] == 1
        assert result.error_context["stuck_tool"] is None
        assert result.failure_code == "max_turns_partial"
        assert "narrower scope" in (result.recovery_suggestion or "")

    async def test_max_turns_no_progress_when_no_text_captured(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Tool calls with no free-text preamble → partial_output empty → no_progress code."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response_no_text("c1"),
            _tool_use_response_no_text("c2"),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Task.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=2,
                agent_id="sub-noprogress",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is False
        assert result.partial_output is None
        # Two consecutive same-tool failures is < 3 → not tool_loop; empty partial → no_progress.
        assert result.failure_code == "max_turns_no_progress"
        assert "infeasible" in (result.recovery_suggestion or "")
        assert result.error_context["max_consecutive_failures"] == 2
        assert result.error_context["stuck_tool"] is None

    async def test_result_slot_in_scratchpad_payload(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Issue #225: failure scratchpad payload carries the result_slot field."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "trying"),
        ])
        scratchpad = Scratchpad()

        result = await run_subagent(
            SubAgentConfig(
                task="Task.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=1,
                agent_id="sub-slotpad",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            scratchpad=scratchpad,
        )

        payload = json.loads(scratchpad.read(f"subagent_failure:{result.agent_id}"))
        # Field exists; None because result_write was never called.
        assert "result_slot" in payload
        assert payload["result_slot"] is None

    async def test_failure_code_in_scratchpad_payload(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Scratchpad JSON must include the new P1 fields alongside P0 context."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "a"),
            _tool_use_response("c2", "b"),
            _tool_use_response("c3", "c"),
        ])
        scratchpad = Scratchpad()

        result = await run_subagent(
            SubAgentConfig(
                task="Task.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=3,
                agent_id="sub-sp",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            scratchpad=scratchpad,
        )

        payload = json.loads(scratchpad.read(f"subagent_failure:{result.agent_id}"))
        assert payload["failure_code"] == "tool_loop"
        assert "always_fail" in payload["recovery_suggestion"]
        assert payload["error_context"]["stuck_tool"] == "always_fail"
        assert payload["error_context"]["max_consecutive_failures"] == 3


# ── result_write slot — issue #225 ──────────────────────────────────────────


def _result_write_response(call_id: str, content: str) -> LLMResponse:
    """Assistant calls result_write(content=...) — registered by run_subagent."""
    return LLMResponse(
        text=None,
        tool_uses=[ToolUse(id=call_id, name="result_write", args={"content": content})],
        stop_reason="tool_use",
        raw_message={
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use", "id": call_id,
                    "name": "result_write", "input": {"content": content},
                },
            ],
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "result_write",
                        "arguments": json.dumps({"content": content}),
                    },
                }
            ],
        },
    )


def _end_turn_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_uses=[],
        stop_reason="end_turn",
        raw_message={"role": "assistant", "content": text},
    )


class TestResultWriteSlot:
    """Issue #225: best-effort result slot survives both termination paths."""

    async def test_slot_overrides_end_turn_text_on_success(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """When sub-agent committed a slot, it takes precedence over end_turn text.

        The slot is the explicit hand-off; end_turn text is incidental closing
        chatter. Parent should see the slot.
        """
        registry = ToolRegistry()
        router = _FakeRouter([
            _result_write_response("c1", "structured result payload"),
            _end_turn_response("ok, done."),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="Produce a result.",
                model="gpt-test",
                allowed_tools=[],
                max_turns=5,
                agent_id="sub-slot-success",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is True
        assert result.result_slot == "structured result payload"
        assert result.output == "structured result payload"

    async def test_slot_falls_back_to_end_turn_text_when_unset(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """No result_write call ⇒ end_turn text is the output (legacy contract)."""
        registry = ToolRegistry()
        router = _FakeRouter([_end_turn_response("done")])

        result = await run_subagent(
            SubAgentConfig(
                task="Trivial.",
                model="gpt-test",
                allowed_tools=[],
                max_turns=3,
                agent_id="sub-no-slot",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.success is True
        assert result.result_slot is None
        assert result.output == "done"

    async def test_slot_survives_max_turns_failure(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """The big #225 win: slot is non-empty even though success=False.

        Sub-agent commits a checkpoint, then keeps spinning, then runs out of
        turns. Parent gets the checkpoint as output instead of an empty hand.
        """
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _result_write_response("c1", "checkpoint A: 3/5 done"),
            _tool_use_response("c2", "trying to finish"),
        ])
        scratchpad = Scratchpad()

        result = await run_subagent(
            SubAgentConfig(
                task="Do five things.",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=2,
                agent_id="sub-slot-maxturns",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
            scratchpad=scratchpad,
        )

        assert result.success is False
        assert result.result_slot == "checkpoint A: 3/5 done"
        # The key contract: output is non-empty on max_turns when slot was set.
        assert result.output == "checkpoint A: 3/5 done"
        # And the scratchpad payload carries it for full audit.
        payload = json.loads(scratchpad.read(f"subagent_failure:sub-slot-maxturns"))
        assert payload["result_slot"] == "checkpoint A: 3/5 done"

    async def test_slot_overwrites_on_repeated_writes(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """Each result_write replaces the slot — last write wins."""
        registry = ToolRegistry()
        router = _FakeRouter([
            _result_write_response("c1", "version 1"),
            _result_write_response("c2", "version 2 — refined"),
            _end_turn_response("done"),
        ])

        result = await run_subagent(
            SubAgentConfig(
                task="x",
                model="gpt-test",
                allowed_tools=[],
                max_turns=5,
                agent_id="sub-overwrite",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        assert result.result_slot == "version 2 — refined"
        assert result.output == "version 2 — refined"


class TestWrapupReminder:
    """Issue #225: harness injects a wrap-up reminder when 2 turns remain."""

    async def test_reminder_injected_when_two_turns_left(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """With max_turns=4, after turn 2 (turns_remaining=2) reminder fires.

        We verify by spying on the messages list — the test router records
        the messages it received on each call.
        """
        registry = ToolRegistry()
        registry.register(_make_failing_tool())

        seen_messages: list[list[dict]] = []

        class _SpyRouter(_FakeRouter):
            async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
                seen_messages.append([dict(m) for m in messages])
                async for chunk, final in super().stream_chat(model, messages, tools, max_tokens):
                    yield chunk, final

        router = _SpyRouter([
            _tool_use_response("c1", "turn 1"),
            _tool_use_response("c2", "turn 2"),
            _tool_use_response("c3", "turn 3"),
            _tool_use_response("c4", "turn 4"),
        ])

        await run_subagent(
            SubAgentConfig(
                task="x",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=4,
                agent_id="sub-wrap",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        # The reminder is appended after the tool_use of turn 2 (turns_used==2,
        # remaining==2), so the 3rd LLM call should see it in its messages.
        third_call = seen_messages[2]
        reminder_present = any(
            isinstance(m.get("content"), str) and "2 turns left" in m["content"]
            for m in third_call
        )
        assert reminder_present, "wrap-up reminder should be injected before turn 3"

        # And it should fire only once — turn 4's messages should still contain
        # exactly one such reminder.
        fourth_call = seen_messages[3]
        count = sum(
            1 for m in fourth_call
            if isinstance(m.get("content"), str) and "2 turns left" in m["content"]
        )
        assert count == 1, f"reminder should be idempotent; saw {count}"

    async def test_reminder_skipped_when_max_turns_below_three(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
        tmp_path: Path,
    ) -> None:
        """max_turns=2 gives no useful 'wrap up' window — reminder must not fire."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())

        seen_messages: list[list[dict]] = []

        class _SpyRouter(_FakeRouter):
            async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
                seen_messages.append([dict(m) for m in messages])
                async for chunk, final in super().stream_chat(model, messages, tools, max_tokens):
                    yield chunk, final

        router = _SpyRouter([
            _tool_use_response("c1", "turn 1"),
            _tool_use_response("c2", "turn 2"),
        ])

        await run_subagent(
            SubAgentConfig(
                task="x",
                model="gpt-test",
                allowed_tools=["always_fail"],
                max_turns=2,
                agent_id="sub-wrap-short",
            ),
            router=router,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            tool_registry=registry,
            parent_session_id="parent-1",
            workspace=tmp_path,
        )

        # No call should have seen the reminder.
        for msgs in seen_messages:
            for m in msgs:
                assert not (
                    isinstance(m.get("content"), str)
                    and "2 turns left" in m["content"]
                ), "no reminder expected when max_turns < 3"
