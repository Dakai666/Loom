"""#648 P3: one scratchpad window, one JIT threshold; no live state."""

import inspect
import itertools
import json
import re

import pytest

from loom.core.harness.middleware import JITRetrievalMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.jobs import Scratchpad
from loom.platform.cli.tools import make_scratchpad_read_tool, sanitize_untrusted_text


def call(args=None, name="scratchpad_read"):
    return ToolCall(
        id="p3",
        tool_name=name,
        args=args or {},
        trust_level=TrustLevel.SAFE,
        session_id="s",
    )


def body(text):
    return text.rsplit("\n\n[lines ", 1)[0]


def test_offset_limit_footer_tail_and_eof():
    pad = Scratchpad()
    pad.write("x", "one\ntwo\nthree\nfour\nfive\n")
    assert body(pad.read("x", offset=2, limit=2)) == "two\nthree"
    assert "[lines 2–3 of 5" in pad.read("x", offset=2, limit=2)
    assert body(pad.read("x", limit=2)) == "one\ntwo"
    assert body(pad.read("x", offset=4)) == "four\nfive"
    assert pad.read("x", offset=20) == "\n\n[lines 0–0 of 5]"


@pytest.mark.parametrize(
    "offset,limit,grep", itertools.product([0, 1, 5], [0, 1, 5], ["", "hit", "missing"])
)
async def test_every_combination_is_valid(offset, limit, grep):
    pad = Scratchpad()
    pad.write("x", "hit a\nmiss\nhit b\nhit c\nhit d")
    result = await make_scratchpad_read_tool(pad).executor(
        call({"ref": "x", "offset": offset, "limit": limit, "grep": grep})
    )
    assert result.success, result.error
    assert result.output.count("[lines ") == 1


@pytest.mark.parametrize(
    "args",
    [
        {"offset": 0, "limit": 0, "grep": ""},
        {"offset": None, "limit": None, "grep": None},
    ],
)
def test_zero_empty_and_none_are_unset(args):
    pad = Scratchpad()
    pad.write("x", "a\nb")
    assert pad.read("x", **args) == pad.read("x")


def test_grep_then_window_keeps_original_line_numbers_and_zero_matches():
    pad = Scratchpad()
    pad.write("x", "hit a\nmiss\nhit b\nhit c\nmiss")
    out = pad.read("x", grep="hit", offset=2, limit=1)
    assert body(out) == "3: hit b"
    assert "matched 3" in out and "continue with offset=3" in out
    miss = pad.read("x", grep="none")
    assert "matched 0" in miss and "continue" not in miss


def test_fixed_cap_whole_line_pages_have_no_overlap_or_gap():
    from loom.core.jobs import scratchpad as module

    pad = Scratchpad()
    lines = [f"{i}: " + "中" * 400 for i in range(450)]
    pad.write("x", "\n".join(lines))
    got = []
    offset = 1
    for _ in range(20):
        out = pad.read("x", offset=offset)
        assert len(out.encode()) <= module.MAX_READ_BYTES
        assert "\ufffd" not in out
        got.extend(body(out).split("\n"))
        nxt = re.search(r"continue with offset=(\d+)", out)
        if not nxt:
            break
        assert int(nxt[1]) > offset
        offset = int(nxt[1])
    assert got == lines


def test_over_cap_line_prefix_is_explicit_and_continues_at_next_line():
    from loom.core.jobs import scratchpad as module

    pad = Scratchpad()
    long = "中" * 100_000
    pad.write("x", long + "\nnext-line")
    out = pad.read("x")
    assert 0 < len(body(out)) < len(long) and long.startswith(body(out))
    assert len(out.encode()) <= module.MAX_READ_BYTES and "\ufffd" not in out
    assert "line 1 truncated" in out and "300000 bytes" in out
    assert "continue with offset=2" in out
    assert body(pad.read("x", offset=2)) == "next-line"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"offset": 2, "limit": 1},
        {"offset": 0, "limit": 0, "grep": ""},
        {"grep": "payload", "offset": 1, "limit": 1},
        {"offset": 5},
    ],
)
async def test_untrusted_wrap_survives_every_read_shape(args):
    pad = Scratchpad()
    pad.write("fetch_x", sanitize_untrusted_text("payload a\npayload b\npayload c"))
    result = await make_scratchpad_read_tool(pad).executor(
        call({"ref": "fetch_x", **args})
    )
    assert result.success
    assert result.output.count("<untrusted_external_content>") == 1
    assert result.output.count("</untrusted_external_content>") == 1


async def test_flat_listing_and_schema_have_no_retired_knobs():
    pad = Scratchpad()
    pad.write("auto_x", "x")
    pad.write("masked_x", "y")
    tool = make_scratchpad_read_tool(pad)
    assert set(tool.input_schema["properties"]) == {"ref", "offset", "limit", "grep"}
    result = await tool.executor(call())
    assert json.loads(result.output) == {"available_refs": ["auto_x", "masked_x"]}
    assert set(inspect.signature(Scratchpad.read).parameters) == {
        "self",
        "ref",
        "offset",
        "limit",
        "grep",
    }
    assert "spill_threshold_chars" not in ToolDefinition.__dataclass_fields__


async def test_jit_real_threshold_preview_digest_and_passthrough():
    pad = Scratchpad()
    reg = ToolRegistry()
    jit = JITRetrievalMiddleware(pad, reg)
    assert JITRetrievalMiddleware.THRESHOLD_CHARS == 24000

    async def run(text, metadata=None):
        async def inner(c):
            return ToolResult(
                call_id=c.id,
                tool_name=c.tool_name,
                success=True,
                output=text,
                metadata=metadata or {},
            )

        return await jit.process(call(name="test"), inner)

    for size in (8001, 23999, 24000):
        result = await run("x" * size)
        assert result.output == "x" * size and not result.metadata.get("jit_spilled")
    text = "\n".join(f"line-{i:02d} " + ("x" * 1000) for i in range(25))
    result = await run(text)
    assert result.metadata["jit_spilled"] and result.metadata["jit_digest"]
    assert result.metadata["jit_ref"] in result.output and "25 lines" in result.output
    assert "line-00" in result.output and "line-20" not in result.output
    assert len(result.output) < 24000
    async_result = await run(text, {"async": True})
    assert async_result.output == text and not async_result.metadata.get("jit_spilled")


def test_old_jit_setting_is_ignored_and_warned(monkeypatch, tmp_path, caplog):
    from loom.core import session as module
    from unittest.mock import MagicMock

    monkeypatch.setattr(module, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(module, "_load_env", lambda *a, **k: {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        module,
        "_load_loom_config",
        lambda: {"harness": {"jit_spill_threshold_tokens": 0}},
    )
    s = module.LoomSession(
        model="test", db_path=str(tmp_path / "unused.db"), workspace=tmp_path
    )
    assert not hasattr(s, "_jit_threshold_chars")
    notes = [
        r.message for r in caplog.records if "jit_spill_threshold_tokens" in r.message
    ]
    assert len(notes) == 1 and "ignored" in notes[0]


@pytest.mark.parametrize(
    "args",
    [
        {"offset": 0, "limit": 0, "grep": ""},
        {"offset": None, "limit": None, "grep": None},
        {"offset": "", "limit": "", "grep": ""},
        {"offset": 0, "limit": 0, "grep": None},
    ],
)
async def test_unset_values_survive_real_schema_middleware(args):
    from loom.core.harness.validation import SchemaValidationMiddleware

    pad = Scratchpad()
    pad.write("x", "first\nsecond")
    tool = make_scratchpad_read_tool(pad)
    registry = ToolRegistry()
    registry.register(tool)
    result = await SchemaValidationMiddleware(registry).process(
        call({"ref": "x", **args}), tool.executor
    )
    assert result.success, result.error
    assert result.output == pad.read("x")


async def test_jit_preview_keeps_external_content_delimited():
    pad = Scratchpad()
    text = sanitize_untrusted_text("external line\n" * 3000)

    async def inner(c):
        return ToolResult(
            call_id=c.id, tool_name=c.tool_name, success=True, output=text
        )

    result = await JITRetrievalMiddleware(pad, ToolRegistry()).process(
        call(name="fetch"), inner
    )
    assert result.metadata["jit_spilled"]
    assert result.output.count("<untrusted_external_content>") == 1
    assert result.output.count("</untrusted_external_content>") == 1

    preview = result.output.split("Preview (up to 20 lines, 2000 chars):\n", 1)[1]
    assert len(preview.splitlines()) <= 20


def test_over_cap_invalid_bytes_marker_reports_original_line_byte_length():
    pad = Scratchpad()
    pad.write("raw", b"\xff" * 250_000 + b"\nnext")
    output = pad.read("raw")
    assert "line 1 truncated (250000 bytes)" in output
    assert "continue with offset=2" in output


@pytest.mark.parametrize(
    "profile",
    [
        "loom.toml.example",
        "examples/profiles/loom.toml.example.assistant",
        "examples/profiles/loom.toml.example.autonomous",
        "examples/profiles/loom.toml.example.secure",
    ],
)
def test_config_examples_have_no_retired_jit_key(profile):
    from pathlib import Path
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / profile).read_text())
    assert "jit_spill_threshold_tokens" not in config.get("harness", {})


async def test_wrapped_over_cap_read_stays_within_fixed_output_cap():
    from loom.core.jobs.scratchpad import MAX_READ_BYTES

    pad = Scratchpad()
    pad.write("fetch_x", sanitize_untrusted_text("中" * 100_000 + "\nnext"))
    result = await make_scratchpad_read_tool(pad).executor(
        call({"ref": "fetch_x", "offset": 2})
    )
    assert result.success and len(result.output.encode()) <= MAX_READ_BYTES
    assert result.output.count("<untrusted_external_content>") == 1
    assert result.output.count("</untrusted_external_content>") == 1
    assert "line 2 truncated (300000 bytes)" in result.output
    assert "continue with offset=3" in result.output
