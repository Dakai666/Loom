"""Tests for PursuitStore + pursuit tools — Issue #375.

Covers:
  - PursuitStore: id validation, list/read/write/delete, atomic write
  - Tool executors: success path, error path, JSON shape
"""

from __future__ import annotations

import json

import pytest

from loom.core.tasks.pursuit import PursuitStore, PursuitError
from loom.core.harness.middleware import (
    ToolCall,
    ToolResult,
    _extract_pursuit_write_artifact,
)
from loom.core.harness.permissions import TrustLevel, ToolCapability
from loom.platform.cli.tools import (
    make_pursuit_list_tool,
    make_pursuit_read_tool,
    make_pursuit_write_tool,
)


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


@pytest.fixture
def store(tmp_path):
    return PursuitStore(root=tmp_path / "pursuits")


# ── PursuitStore: id validation ───────────────────────────────────────

class TestIdValidation:
    @pytest.mark.parametrize("pid", [
        "openai-trial", "x", "a1", "long-name-with-many-hyphens-123",
        "0day", "a" * 64,
    ])
    def test_valid_ids(self, store, pid):
        store.write(pid, "content")
        assert store.exists(pid)

    @pytest.mark.parametrize("pid", [
        "",                          # empty
        "Has-Caps",                  # uppercase
        "with space",                # space
        "with/slash",                # path traversal attempt
        "..",                        # parent dir
        "../escape",                 # parent dir
        "-leading-hyphen",           # cannot start with hyphen
        "trailing.dot",              # dot
        "under_score",               # underscore
        "a" * 65,                    # too long
    ])
    def test_invalid_ids_rejected(self, store, pid):
        with pytest.raises(PursuitError):
            store.write(pid, "content")
        with pytest.raises(PursuitError):
            store.read(pid)


# ── PursuitStore: list / read / write / delete ────────────────────────

class TestStoreOperations:
    def test_list_empty_when_no_root(self, store):
        assert store.list() == []

    def test_list_returns_sorted_ids(self, store):
        store.write("zeta", "z")
        store.write("alpha", "a")
        store.write("middle", "m")
        assert store.list() == ["alpha", "middle", "zeta"]

    def test_list_ignores_non_md(self, store, tmp_path):
        store.write("real-one", "x")
        (tmp_path / "pursuits" / "stray.txt").write_text("not a pursuit")
        (tmp_path / "pursuits" / "no_extension").write_text("nope")
        assert store.list() == ["real-one"]

    def test_list_ignores_invalid_id_files(self, store, tmp_path):
        store.write("valid", "x")
        # File with invalid id sitting in the dir should not appear in list
        (tmp_path / "pursuits" / "BAD-ID.md").write_text("x")
        assert store.list() == ["valid"]

    def test_read_missing_raises(self, store):
        with pytest.raises(PursuitError, match="not found"):
            store.read("ghost")

    def test_write_then_read_roundtrip(self, store):
        content = "# OpenAI 世紀審判\n\nstatus: active\n"
        store.write("openai-trial", content)
        assert store.read("openai-trial") == content

    def test_write_overwrites(self, store):
        store.write("topic", "v1")
        store.write("topic", "v2")
        assert store.read("topic") == "v2"

    def test_write_creates_root_dir(self, store):
        # Root does not exist before first write
        assert not store.root.exists()
        store.write("first", "hi")
        assert store.root.is_dir()

    def test_write_atomic_leaves_no_tmp(self, store):
        store.write("topic", "content")
        leftovers = [p for p in store.root.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_exists(self, store):
        assert store.exists("topic") is False
        store.write("topic", "x")
        assert store.exists("topic") is True


# ── Tool: pursuit_list ────────────────────────────────────────────────

class TestPursuitListTool:
    async def test_empty(self, store):
        tool = make_pursuit_list_tool(store)
        result = await tool.executor(_tc("pursuit_list", {}))
        assert result.success
        payload = json.loads(result.output)
        assert payload == {"count": 0, "pursuits": []}

    async def test_populated(self, store):
        store.write("b-one", "x")
        store.write("a-one", "y")
        tool = make_pursuit_list_tool(store)
        result = await tool.executor(_tc("pursuit_list", {}))
        payload = json.loads(result.output)
        assert payload["count"] == 2
        assert payload["pursuits"] == ["a-one", "b-one"]


# ── Tool: pursuit_read ────────────────────────────────────────────────

class TestPursuitReadTool:
    async def test_success(self, store):
        store.write("topic", "# Hello\n\ncontent here")
        tool = make_pursuit_read_tool(store)
        result = await tool.executor(_tc("pursuit_read", {"id": "topic"}))
        assert result.success
        payload = json.loads(result.output)
        assert payload == {"id": "topic", "content": "# Hello\n\ncontent here"}

    async def test_missing_id_arg(self, store):
        tool = make_pursuit_read_tool(store)
        result = await tool.executor(_tc("pursuit_read", {}))
        assert not result.success
        assert "id" in result.error

    async def test_not_found(self, store):
        tool = make_pursuit_read_tool(store)
        result = await tool.executor(_tc("pursuit_read", {"id": "ghost"}))
        assert not result.success
        assert "not found" in result.error

    async def test_invalid_id(self, store):
        tool = make_pursuit_read_tool(store)
        result = await tool.executor(_tc("pursuit_read", {"id": "Bad Id"}))
        assert not result.success
        assert "invalid" in result.error


# ── Tool: pursuit_write ───────────────────────────────────────────────

class TestPursuitWriteTool:
    async def test_create(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "id": "openai-trial",
            "content": "# OpenAI 世紀審判\nstatus: active\n",
            "justification": "user asked to start tracking",
        }))
        assert result.success
        payload = json.loads(result.output)
        assert payload["id"] == "openai-trial"
        assert payload["bytes"] > 0
        assert payload["path"].endswith("openai-trial.md")
        assert store.read("openai-trial").startswith("# OpenAI 世紀審判")

    async def test_overwrite(self, store):
        tool = make_pursuit_write_tool(store)
        await tool.executor(_tc("pursuit_write", {
            "id": "t", "content": "v1", "justification": "j",
        }))
        await tool.executor(_tc("pursuit_write", {
            "id": "t", "content": "v2", "justification": "j",
        }))
        assert store.read("t") == "v2"

    async def test_missing_id(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "content": "x", "justification": "j",
        }))
        assert not result.success
        assert "id" in result.error

    async def test_missing_content(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "id": "topic", "justification": "j",
        }))
        assert not result.success
        assert "content" in result.error

    async def test_empty_content_rejected(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "id": "topic", "content": "   \n  ", "justification": "j",
        }))
        assert not result.success
        assert "content" in result.error

    async def test_invalid_id(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "id": "Has Caps", "content": "x", "justification": "j",
        }))
        assert not result.success
        assert "invalid" in result.error

    async def test_path_traversal_blocked(self, store):
        tool = make_pursuit_write_tool(store)
        result = await tool.executor(_tc("pursuit_write", {
            "id": "../escape", "content": "x", "justification": "j",
        }))
        assert not result.success
        # And no file was created outside root
        assert not (store.root.parent / "escape.md").exists()


# ── Tool definition guarantees ────────────────────────────────────────

class TestPursuitWriteToolDefinition:
    """Regression guards: GUARDED + MUTATES + justification required +
    scope resolver wired. These shape the harness-layer behavior the
    executor unit tests cannot verify directly."""

    def test_trust_and_capabilities_are_guarded_mutating(self, store):
        tool = make_pursuit_write_tool(store)
        assert tool.trust_level == TrustLevel.GUARDED
        assert tool.capabilities == ToolCapability.MUTATES

    def test_input_schema_requires_justification(self, store):
        tool = make_pursuit_write_tool(store)
        required = tool.input_schema["required"]
        assert "justification" in required
        assert "id" in required
        assert "content" in required

    def test_scope_resolver_selector_is_pursuit_id(self, store):
        tool = make_pursuit_write_tool(store)
        assert tool.scope_resolver is not None
        call = _tc("pursuit_write", {
            "id": "openai-trial", "content": "x", "justification": "j",
        })
        req = tool.scope_resolver(call)
        assert len(req.requirements) == 1
        r = req.requirements[0]
        assert r.resource == "pursuit"
        assert r.action == "write"
        assert r.selector == "openai-trial"

    def test_scope_resolver_falls_back_to_star_for_missing_id(self, store):
        tool = make_pursuit_write_tool(store)
        call = _tc("pursuit_write", {"content": "x", "justification": "j"})
        req = tool.scope_resolver(call)
        assert req.requirements[0].selector == "*"


# ── Artifact extractor ────────────────────────────────────────────────

class TestPursuitWriteArtifact:
    def test_extracts_size_and_digest(self):
        import hashlib

        content = "# Hello\n\ncontent body"
        encoded = content.encode("utf-8")
        call = ToolCall(
            tool_name="pursuit_write",
            args={"id": "t", "content": content, "justification": "j"},
            trust_level=TrustLevel.GUARDED, session_id="test",
        )
        result = ToolResult(
            call_id=call.id, tool_name="pursuit_write",
            success=True,
            output=json.dumps({
                "id": "t", "path": "/tmp/pursuits/t.md", "bytes": len(encoded),
            }),
        )
        artifact = _extract_pursuit_write_artifact(call, result)
        assert artifact == {
            "artifact_type": "text_file",
            "size_bytes": len(encoded),
            "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
            "location": "/tmp/pursuits/t.md",
        }

    def test_extractor_handles_malformed_output_json(self):
        call = ToolCall(
            tool_name="pursuit_write",
            args={"id": "t", "content": "x", "justification": "j"},
            trust_level=TrustLevel.GUARDED, session_id="test",
        )
        # Output that isn't valid JSON should not raise — location ends None
        result = ToolResult(
            call_id=call.id, tool_name="pursuit_write",
            success=True, output="not-json",
        )
        artifact = _extract_pursuit_write_artifact(call, result)
        assert artifact is not None
        assert artifact["location"] is None
        assert artifact["size_bytes"] == 1  # one byte: 'x'
