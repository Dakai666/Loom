from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from loom.core.agent.subagent import SubAgentConfig, run_subagent
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.registry import ToolRegistry
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
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
        registry.register(make_memorize_tool(semantic))
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
