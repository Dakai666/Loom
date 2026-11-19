"""#650 S3 PR-C: one governed write, honest outcomes, protected rollback."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from loom.core.agent.subagent import SubAgentConfig, run_subagent
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational_bridge import RelationalEntry, get_triple, upsert_triple
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory, classify_source
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import make_memorize_tool, make_relate_tool


class Embeddings:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def embed(self, texts):
        self.calls.extend(texts)
        if self.fail:
            raise RuntimeError("synthetic embedding outage")
        return [[0.01] * 8 for _ in texts]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from fastapi.testclient import TestClient
    from loom.platform.api import server
    provider = Embeddings()
    builder = Mock(return_value=provider)
    monkeypatch.setattr(server, "build_embedding_provider", builder, raising=False)
    monkeypatch.setattr(server, "load_loom_config", lambda: {"memory": {"governance": {"admission_threshold": 0.8}}}, raising=False)
    monkeypatch.setattr(server, "_load_env", lambda: {}, raising=False)
    path = tmp_path / "memory.db"
    asyncio.run(SQLiteStore(str(path)).initialize())
    app = server.create_app(db_path=str(path))
    with TestClient(app) as c:
        yield c, provider, builder


def stored(c, key):
    return c.portal.call(SemanticMemory(c.app.state.db).get, key)


def seed(c, key, value="trusted", source="manual"):
    c.portal.call(SemanticMemory(c.app.state.db).upsert,
                  SemanticEntry(key=key, value=value, source=source))


@pytest.mark.parametrize("source", ["api", "manual", "memorize", "tool:verified"])
def test_api_source_cannot_raise_trust_and_wires_real_governor_embeddings(client, source):
    c, provider, builder = client
    result = c.post("/memory/semantic", json={"key": "fact", "value": "new observation", "confidence": 0.1, "source": source})
    assert result.status_code == 201
    body = result.json()
    assert body["written"] is True and body["status"] == "written"
    assert body["trust_tier"] == "external" and body["ref"].startswith("mem:fact@")
    entry = stored(c, "fact")
    assert classify_source(entry.source) == ("external", 0.5)
    assert entry.confidence == 0.5
    builder.assert_called_once()
    assert isinstance(c.app.state.memory.governor, MemoryGovernor)
    assert provider.calls.count("fact new observation") == 1


def test_api_semantic_keep_and_empty_are_real_outcomes(client):
    c, _, _ = client
    seed(c, "fact")
    result = c.post("/memory/semantic", json={"key": "fact", "value": "lower trust", "source": "manual"})
    assert result.status_code == 200
    assert result.json()["written"] is False and result.json()["status"] == "kept"
    assert stored(c, "fact").value == "trusted"
    assert result.json()["ref"].startswith("mem:fact@")
    result = c.post("/memory/semantic", json={"key": "empty", "value": "..."})
    assert result.status_code == 422
    assert result.json()["status"] == "rejected" and result.json()["ref"] is None
    assert stored(c, "empty") is None


def test_api_relational_governed_roundtrip_and_keep(client):
    c, provider, _ = client
    data = {"subject": "alice", "predicate": "knows", "object": "bob", "confidence": 0.1}
    result = c.post("/memory/relational", json=data)
    assert result.status_code == 201 and result.json()["written"]
    key = "rel:alice::knows"
    assert result.json()["ref"].startswith(f"mem:{key}@")
    assert stored(c, key).metadata["object"] == "bob"
    assert provider.calls.count(f"{key} alice knows bob") == 1
    assert c.get("/memory/relational", params={"subject": "alice"}).json()[0]["object"] == "bob"
    c.portal.call(upsert_triple, SemanticMemory(c.app.state.db),
                  RelationalEntry(subject="alice", predicate="knows", object="trusted", source="manual"))
    result = c.post("/memory/relational", json={**data, "object": "mallory", "source": "manual"})
    assert result.status_code == 200 and result.json()["status"] == "kept"
    assert stored(c, key).value == "alice knows trusted"
    assert c.get("/memory/relational", params={"subject": "alice"}).json()[0]["object"] == "trusted"


def test_api_embedding_failure_preserves_literal_and_ref(client):
    c, provider, _ = client
    provider.fail = True
    result = c.post("/memory/semantic", json={"key": "offline", "value": "durable experience"})
    assert result.status_code == 201 and result.json()["written"]
    assert result.json()["embedding_status"] == "failed"
    assert stored(c, "offline").value == "durable experience"
    expanded = c.portal.call(c.app.state.memory.expand_ref, result.json()["ref"])
    assert expanded.found and "durable experience" in expanded.text


@pytest_asyncio.fixture
async def memory(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        provider = Embeddings()
        sem, proc, ep = SemanticMemory(db, provider), ProceduralMemory(db), EpisodicMemory(db)
        gov = MemoryGovernor(semantic=sem, procedural=proc, episodic=ep, db=db)
        yield MemoryFacade(semantic=sem, procedural=proc, episodic=ep, governor=gov,
                           search=MemorySearch(sem, proc)), provider


def relate_call(obj="bob"):
    return ToolCall(tool_name="relate", id="rel", session_id="s", trust_level=TrustLevel.GUARDED,
                    args={"subject": "alice", "predicate": "knows", "object": obj})


async def test_relate_keep_never_rolls_back_the_existing_triple(memory):
    mem, _ = memory
    await upsert_triple(mem.semantic, RelationalEntry(subject="alice", predicate="knows", object="trusted", source="manual"))
    tool = make_relate_tool(mem)
    result = await tool.executor(relate_call())
    assert result.metadata["written"] is False and "kept" in result.output.lower()
    assert result.metadata["ref"].startswith("mem:rel:alice::knows@")
    await tool.rollback_fn(relate_call(), result)
    assert (await get_triple(mem.semantic, "alice", "knows")).object == "trusted"


async def test_relate_write_and_stale_rollback(memory):
    mem, provider = memory
    tool = make_relate_tool(mem)
    result = await tool.executor(relate_call())
    assert result.metadata["written"] and result.metadata["trust_tier"] == "external"
    assert provider.calls.count("rel:alice::knows alice knows bob") == 1
    # A later update must not be erased by a rollback for the earlier write.
    await upsert_triple(mem.semantic, RelationalEntry(subject="alice", predicate="knows", object="newer", source="manual"))
    await tool.rollback_fn(relate_call(), result)
    assert (await get_triple(mem.semantic, "alice", "knows")).object == "newer"
    fresh = await tool.executor(ToolCall(tool_name="relate", session_id="s", trust_level=TrustLevel.GUARDED,
        args={"subject": "other", "predicate": "knows", "object": "person"}))
    await tool.rollback_fn(relate_call(), fresh)
    assert await get_triple(mem.semantic, "other", "knows") is None


async def test_control_state_bridge_stays_ungoverned(memory):
    mem, _ = memory
    mem.governor.governed_upsert = AsyncMock(side_effect=AssertionError("control state must bypass"))
    await upsert_triple(mem.semantic, RelationalEntry(subject="plugin:synthetic", predicate="approved", object="true", source="user"))
    assert (await get_triple(mem.semantic, "plugin:synthetic", "approved")).object == "true"
    mem.governor.governed_upsert.assert_not_awaited()


async def test_child_first_write_keeps_memorize_tier_and_provenance(memory, tmp_path):
    mem, provider = memory
    registry = ToolRegistry()
    registry.register(make_memorize_tool(mem))
    writes = []
    original = mem.semantic.upsert
    async def capture(entry):
        writes.append((entry.source, dict(entry.metadata)))
        return await original(entry)
    mem.semantic.upsert = capture
    class Router:
        async def stream_chat(self, **kwargs):
            if len(kwargs["messages"]) == 2:
                yield "", LLMResponse(text="remember", stop_reason="tool_use",
                    tool_uses=[ToolUse(id="c", name="memorize", args={"key": "child:fact", "value": "important", "confidence": 0.1})],
                    raw_message={"role": "assistant", "content": "remember", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "memorize", "arguments": "{}"}}]})
            else:
                yield "", LLMResponse(text="done", tool_uses=[], stop_reason="end_turn", raw_message={"role": "assistant", "content": "done"})
        def format_tool_result(self, model, cid, content, success=True):
            return {"role": "tool", "tool_call_id": cid, "content": content}
    await run_subagent(SubAgentConfig(task="remember", model="test", agent_id="child", allowed_tools=["memorize"]),
        router=Router(), episodic=mem.episodic, semantic=mem.semantic, procedural=mem.procedural,
        tool_registry=registry, parent_session_id="parent", workspace=tmp_path)
    assert writes == [("memorize", {"agent_id": "child", "parent_session_id": "parent"})]
    entry = await mem.semantic.get("child:fact")
    assert entry.source == "memorize" and entry.confidence == 0.85
    assert provider.calls.count("child:fact important") == 1


def test_api_can_initialize_a_fresh_selected_database(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from fastapi.testclient import TestClient
    from loom.platform.api import server
    monkeypatch.setattr(server, "load_loom_config", lambda: {}, raising=False)
    monkeypatch.setattr(server, "_load_env", lambda: {}, raising=False)
    monkeypatch.setattr(server, "build_embedding_provider", lambda *a: None, raising=False)
    with TestClient(server.create_app(db_path=str(tmp_path / "fresh.db"))) as c:
        result = c.post("/memory/semantic", json={"key": "fresh", "value": "persisted"})
        assert result.status_code == 201 and result.json()["written"]
        assert stored(c, "fresh").source == "api"


async def test_conditional_rollback_does_not_delete_same_value_new_version(memory):
    mem, _ = memory
    tool = make_relate_tool(mem)
    result = await tool.executor(relate_call())
    await upsert_triple(mem.semantic, RelationalEntry(subject="alice", predicate="knows", object="bob", source="relate", metadata={"later": True}))
    await tool.rollback_fn(relate_call(), result)
    entry = await mem.semantic.get("rel:alice::knows")
    assert entry is not None and entry.metadata["later"] is True
