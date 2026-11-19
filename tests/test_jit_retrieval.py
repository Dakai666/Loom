"""Fixed JIT threshold, bounded preview, and existing preservation contracts."""

import hashlib
from unittest.mock import MagicMock

import pytest

from loom.core.harness.middleware import JITRetrievalMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.core.jobs import Scratchpad


def _call(name="test"):
    return ToolCall(
        id="c", tool_name=name, args={}, trust_level=TrustLevel.SAFE, session_id="s"
    )


async def _run(pad, text, *, registry=None, metadata=None, success=True, name="test"):
    async def inner(c):
        return ToolResult(
            call_id=c.id,
            tool_name=c.tool_name,
            success=success,
            output=text,
            metadata=metadata or {},
            error="failure" if not success else None,
            failure_type="execution_error" if not success else None,
            duration_ms=42,
            error_context="stack trace" if not success else None,
        )

    return await JITRetrievalMiddleware(pad, registry).process(_call(name), inner)


@pytest.mark.parametrize(
    "size,spilled", [(8001, False), (23999, False), (24000, False), (24001, True)]
)
async def test_real_threshold_boundary(size, spilled):
    pad = Scratchpad()
    text = "x" * size
    result = await _run(pad, text)
    assert bool(result.metadata.get("jit_spilled")) is spilled
    if spilled:
        ref = result.metadata["jit_ref"]
        assert pad.read(ref).rsplit("\n\n[lines ", 1)[0] == text
        assert result.metadata["jit_original_size"] == size
        assert (
            result.metadata["jit_digest"] == hashlib.sha256(text.encode()).hexdigest()
        )
    else:
        assert result.output == text and pad.list_refs() == []


async def test_preview_has_first_twenty_lines_without_dual_path_teaching():
    text = "\n".join(
        [f"line-{i:02d}" for i in range(20)] + ["large remainder " + "x" * 30_000]
    )
    result = await _run(Scratchpad(), text)
    assert result.metadata["jit_ref"] in result.output
    assert f"{len(text)} chars, 21 lines" in result.output
    assert "line-19" in result.output and "large remainder" not in result.output
    assert "re-call" not in result.output and "fresh data" not in result.output


async def test_single_giant_line_preview_is_bounded():
    result = await _run(Scratchpad(), "中" * 100_000)
    assert "1 lines" in result.output and len(result.output) < 24000


@pytest.mark.parametrize(
    "factory", ["load_skill", "scratchpad_read", "list_dir", "task_write"]
)
async def test_content_tools_remain_inline_even_above_new_threshold(factory):
    from loom.platform.cli.skill_tools import make_load_skill_tool
    from loom.platform.cli.tools import (
        make_scratchpad_read_tool,
        make_filesystem_tools,
        make_task_write_tool,
    )
    from pathlib import Path

    if factory == "load_skill":
        tool = make_load_skill_tool(MagicMock())
    elif factory == "scratchpad_read":
        tool = make_scratchpad_read_tool(Scratchpad())
    elif factory == "list_dir":
        tool = next(
            t for t in make_filesystem_tools(Path.cwd()) if t.name == "list_dir"
        )
    else:
        tool = make_task_write_tool(MagicMock())
    assert tool.inline_only
    registry = ToolRegistry()
    registry.register(tool)
    text = "x" * 50_000
    pad = Scratchpad()
    result = await _run(pad, text, registry=registry, name=factory)
    assert result.output == text and pad.list_refs() == []


async def test_async_job_output_never_spills():
    pad = Scratchpad()
    text = "job handle " + "x" * 30_000
    result = await _run(pad, text, metadata={"async": True, "job_id": "job_x"})
    assert (
        result.output == text
        and not result.metadata.get("jit_spilled")
        and pad.list_refs() == []
    )


async def test_write_failure_keeps_original_result_inline():
    class Broken:
        def write(self, *a):
            raise OSError("synthetic write failure")

    result = await _run(Broken(), "x" * 30_000)
    assert result.output == "x" * 30_000 and not result.metadata.get("jit_spilled")


async def test_failed_tool_metadata_and_error_context_survive_spill():
    result = await _run(
        Scratchpad(), "stderr\n" * 5000, metadata={"custom": "value"}, success=False
    )
    assert not result.success and result.metadata["jit_spilled"]
    assert result.metadata["custom"] == "value" and result.error == "failure"
    assert result.failure_type == "execution_error" and result.duration_ms == 42
    assert result.error_context == "stack trace"


async def test_spills_get_unique_refs_and_stable_digest():
    pad = Scratchpad()
    a = await _run(pad, "x" * 30_000)
    b = await _run(pad, "x" * 30_000)
    assert a.metadata["jit_ref"] != b.metadata["jit_ref"]
    assert a.metadata["jit_digest"] == b.metadata["jit_digest"]
