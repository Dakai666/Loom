from __future__ import annotations

from loom.core.harness.permissions import PermissionContext

import json
from pathlib import Path

import pytest_asyncio

from loom.core.agent.subagent import SubAgentConfig, run_subagent
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
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
        facade = MemoryFacade(
            semantic=semantic, procedural=procedural,
            episodic=episodic,
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
            parent_declared={"memorize"},  # #704 S3: never wider than the parent
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
        assert entry.source == "memorize"
        assert entry.metadata["agent_id"] == "sub-123"
        assert entry.metadata["parent_session_id"] == "parent-1"



# ── helpers ─────────────────────────────────────────────────────────────────

def _make_failing_tool() -> ToolDefinition:
    """A SAFE tool that always fails — used to keep the loop in tool_use."""

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


def _tool_use_response(tool_id: str, preamble_text: str, tool_name: str = "always_fail") -> LLMResponse:
    """Assistant emits a short free-text preamble then calls a tool."""
    content: list[dict] = []
    if preamble_text:
        content.append({"type": "text", "text": preamble_text})
    content.append({"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}})
    return LLMResponse(
        text=None,
        tool_uses=[ToolUse(id=tool_id, name=tool_name, args={})],
        stop_reason="tool_use",
        raw_message={
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {"id": tool_id, "type": "function",
                 "function": {"name": tool_name, "arguments": "{}"}},
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


def _max_tokens_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_uses=[],
        stop_reason="max_tokens",
        raw_message={"role": "assistant", "content": text},
    )


class _SpyRouter(_FakeRouter):
    """Records what the model was shown on every call."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.calls: list[dict] = []

    async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": [t["name"] for t in (tools or [])],
        })
        async for chunk, final in super().stream_chat(model, messages, tools, max_tokens):
            yield chunk, final


class _NoResponseRouter(_FakeRouter):
    """A stream that ends without ever producing a final response."""

    async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
        if False:  # pragma: no cover - makes this an async generator
            yield "", None


async def _run(router, registry, tmp_path, *, max_turns=5, allowed_tools=[],
               semantic, episodic, procedural, agent_id="sub-t"):
    return await run_subagent(
        SubAgentConfig(task="do the thing", model="gpt-test",
                       allowed_tools=allowed_tools,
                       max_turns=max_turns, agent_id=agent_id),
        router=router, episodic=episodic, semantic=semantic, procedural=procedural,
        tool_registry=registry, parent_session_id="parent-1",
        workspace=Path("/tmp/sub-ws"),  # fixed: tmp_path embeds the test name
    )


# ── the single exit rule (#648 P4) ──────────────────────────────────────────

class TestSingleExitRule:
    """Success ⇔ the loop reached end_turn and the reply has text. Every other
    exit is a failure carrying the most recent assistant text."""

    async def test_end_turn_with_text_is_success(self, semantic, episodic, procedural, tmp_path):
        result = await _run(_FakeRouter([_end_turn_response("the answer")]),
                            ToolRegistry(), tmp_path,
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is True
        assert result.output == "the answer"
        assert result.turns_used == 1
        assert result.error is None

    async def test_max_turns_is_failure_and_collects_last_text(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "first attempt: trying X"),
            _tool_use_response("c2", "second attempt: retrying"),
        ])
        result = await _run(router, registry, tmp_path, max_turns=2,
                            allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert "second attempt" in result.output
        assert "max_turns" in result.error and "2" in result.error
        assert result.turns_used == 2

    async def test_failure_without_any_text_has_empty_output_and_an_error(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        result = await _run(_FakeRouter([_tool_use_response("c1", "")]), registry, tmp_path,
                            max_turns=1, allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == ""
        assert result.error

    async def test_max_tokens_exit_is_never_success(self, semantic, episodic, procedural, tmp_path):
        """False-success case 1: truncated reply (stop_reason=max_tokens)."""
        result = await _run(_FakeRouter([_max_tokens_response("cut off mid-thou")]),
                            ToolRegistry(), tmp_path,
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert "cut off mid-thou" in result.output
        assert "max_tokens" in result.error
        assert result.turns_used == 1

    async def test_max_tokens_with_no_text_is_never_success(
        self, semantic, episodic, procedural, tmp_path,
    ):
        """Reasoning model spends the whole budget thinking: empty + max_tokens."""
        result = await _run(_FakeRouter([_max_tokens_response("")]), ToolRegistry(), tmp_path,
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == ""
        assert result.error

    async def test_empty_end_turn_is_never_success(self, semantic, episodic, procedural, tmp_path):
        """False-success case 2: end_turn whose reply has no text."""
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "did some work"),
            _end_turn_response("   "),
        ])
        result = await _run(router, registry, tmp_path, allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert "did some work" in result.output  # last non-empty text is handed back
        assert "empty" in result.error
        assert result.turns_used == 2

    async def test_no_response_is_never_success(self, semantic, episodic, procedural, tmp_path):
        """False-success case 3: the stream ended without a final response."""
        result = await _run(_NoResponseRouter([]), ToolRegistry(), tmp_path,
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == ""
        assert "response" in result.error
        assert result.turns_used == 0

    async def test_failure_output_is_exactly_the_last_text_not_the_history(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "STALE first proposal"),
            _tool_use_response("c2", "CURRENT last proposal"),
        ])
        result = await _run(router, registry, tmp_path, max_turns=2,
                            allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == "CURRENT last proposal"

    async def test_failure_output_keeps_last_non_empty_text_when_latest_is_empty(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _FakeRouter([
            _tool_use_response("c1", "first"),
            _tool_use_response("c2", "second"),
            _tool_use_response("c3", ""),
        ])
        result = await _run(router, registry, tmp_path, max_turns=3,
                            allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == "second"

    async def test_model_call_exception_is_a_failure_with_last_text(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())

        class _RaisingRouter(_FakeRouter):
            async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
                if not self._responses_left():
                    raise RuntimeError("synthetic provider failed")
                async for item in super().stream_chat(model, messages, tools, max_tokens):
                    yield item

            def _responses_left(self):
                try:
                    nxt = next(self._responses)
                except StopIteration:
                    return False
                self._responses = iter([nxt])
                return True

        router = _RaisingRouter([_tool_use_response("c1", "collected findings")])
        result = await _run(router, registry, tmp_path, allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is False
        assert result.output == "collected findings"
        assert "RuntimeError" in result.error and "1 turn(s)" in result.error
        assert result.turns_used == 1

    async def test_cancellation_still_propagates(
        self, semantic, episodic, procedural, tmp_path,
    ):
        import asyncio
        import pytest

        class _CancelRouter(_FakeRouter):
            async def stream_chat(self, model, messages, tools=None, max_tokens=8096):
                raise asyncio.CancelledError()
                yield  # pragma: no cover

        with pytest.raises(asyncio.CancelledError):
            await _run(_CancelRouter([]), ToolRegistry(), tmp_path,
                       semantic=semantic, episodic=episodic, procedural=procedural)

    async def test_result_carries_only_the_slim_fields(self):
        import dataclasses
        from loom.core.agent.subagent import SubAgentResult
        assert {f.name for f in dataclasses.fields(SubAgentResult)} == {
            "success", "output", "agent_id", "turns_used", "tool_calls", "error",
        }


class TestFailureRitualRetired:
    def test_module_has_no_ritual_helpers(self):
        import loom.core.agent.subagent as module
        assert not hasattr(module, "_classify_failure")
        assert not hasattr(module, "_write_failure_scratchpad")

    async def test_no_result_write_tool_and_no_handoff_contract(
        self, semantic, episodic, procedural, tmp_path,
    ):
        router = _SpyRouter([_end_turn_response("ok")])
        await _run(router, ToolRegistry(), tmp_path,
                   semantic=semantic, episodic=episodic, procedural=procedural)
        first = router.calls[0]
        assert "result_write" not in first["tools"]
        system = first["messages"][0]["content"]
        assert "result_write" not in system
        assert "HAND-OFF CONTRACT" not in system
        assert "reply" in system  # the one sentence: the reply is the handoff

    async def test_no_wrapup_reminder_near_max_turns(
        self, semantic, episodic, procedural, tmp_path,
    ):
        registry = ToolRegistry()
        registry.register(_make_failing_tool())
        router = _SpyRouter([
            _tool_use_response("c1", "a"), _tool_use_response("c2", "b"),
            _tool_use_response("c3", "c"), _end_turn_response("fin"),
        ])
        result = await _run(router, registry, tmp_path, max_turns=5,
                            allowed_tools=["always_fail"],
                            semantic=semantic, episodic=episodic, procedural=procedural)
        assert result.success is True
        for call in router.calls:
            for m in call["messages"]:
                assert "<system-reminder>" not in str(m.get("content"))


class TestUnknownToolHint:
    async def test_unknown_tool_lists_available_tools_without_result_write(
        self, semantic, episodic, procedural, tmp_path,
    ):
        router = _SpyRouter([
            _tool_use_response("c1", "", tool_name="write_file"),
            _end_turn_response("ok"),
        ])
        await _run(router, ToolRegistry(), tmp_path, max_turns=4,
                   semantic=semantic, episodic=episodic, procedural=procedural)
        body = [m for m in router.calls[1]["messages"] if m.get("role") == "tool"][-1]["content"]
        assert "write_file" in body
        assert "Available tools" in body
        assert "result_write" not in body


class TestToolScopeUnchanged:
    async def test_critical_and_unlisted_tools_stay_out(
        self, semantic, episodic, procedural, tmp_path,
    ):
        async def _noop(call):
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="x")

        def _tool(name, trust):
            return ToolDefinition(name=name, description=name, trust_level=trust,
                                  input_schema={"type": "object", "properties": {}},
                                  executor=_noop, tags=[])

        registry = ToolRegistry()
        registry.register(_tool("safe_one", TrustLevel.SAFE))
        registry.register(_tool("guarded_one", TrustLevel.GUARDED))
        registry.register(_tool("critical_one", TrustLevel.CRITICAL))

        default_scope = _SpyRouter([_end_turn_response("ok")])
        await _run(default_scope, registry, tmp_path, allowed_tools=None,
                   semantic=semantic, episodic=episodic, procedural=procedural)
        assert default_scope.calls[0]["tools"] == ["safe_one"]

        whitelisted = _SpyRouter([_end_turn_response("ok")])
        await _run(whitelisted, registry, tmp_path,
                   allowed_tools=["guarded_one", "critical_one"],
                   semantic=semantic, episodic=episodic, procedural=procedural)
        assert whitelisted.calls[0]["tools"] == ["guarded_one"]


# ── spawn_agent (parent-facing tool) ────────────────────────────────────────

def _spawn_parent(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(
        model="test", router=object(), registry=ToolRegistry(), session_id="parent",
        workspace=tmp_path, perm=PermissionContext(session_id="parent"),
        _memory=SimpleNamespace(episodic=object(), semantic=object(),
                                procedural=object(), session_log=None),
    )


def _spawn_call(**args):
    return ToolCall(id="spawn", tool_name="spawn_agent", args={"task": "inspect", **args},
                    trust_level=TrustLevel.GUARDED, session_id="parent")


class TestSpawnAgentTool:
    async def test_failure_message_is_error_plus_last_text(self, tmp_path, monkeypatch):
        import loom.core.agent.subagent as module
        from loom.core.agent.subagent import SubAgentResult
        from loom.platform.cli.tools import make_spawn_agent_tool

        async def run(config, **kwargs):
            return SubAgentResult(
                success=False, output="halfway findings", agent_id="sub-x",
                turns_used=3, tool_calls=2,
                error="Sub-agent reached max_turns (3) without a final reply.",
            )

        monkeypatch.setattr(module, "run_subagent", run)
        result = await make_spawn_agent_tool(_spawn_parent(tmp_path)).executor(_spawn_call())
        assert result.success is False
        assert "max_turns (3)" in result.error
        assert "halfway findings" in result.error
        assert "scratchpad" not in result.error and "subagent_failure" not in result.error
        assert result.failure_type == "execution_error"

    async def test_success_returns_header_and_reply(self, tmp_path, monkeypatch):
        import loom.core.agent.subagent as module
        from loom.core.agent.subagent import SubAgentResult
        from loom.platform.cli.tools import make_spawn_agent_tool

        async def run(config, **kwargs):
            return SubAgentResult(success=True, output="short ok", agent_id="sub-x",
                                  turns_used=2, tool_calls=1)

        monkeypatch.setattr(module, "run_subagent", run)
        result = await make_spawn_agent_tool(_spawn_parent(tmp_path)).executor(_spawn_call())
        assert result.success is True
        assert "[sub-agent sub-x]" in result.output and result.output.endswith("short ok")

    def test_definition_has_no_verifier_and_no_result_write(self, tmp_path):
        from loom.platform.cli import tools
        definition = tools.make_spawn_agent_tool(_spawn_parent(tmp_path))
        assert "CANNOT persist" not in definition.description
        assert "no arbitrary write_file" in definition.description
        assert definition.post_validator is None
        assert "result_write" not in definition.description
        assert not hasattr(tools, "_verify_spawn_agent")
