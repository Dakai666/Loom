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
