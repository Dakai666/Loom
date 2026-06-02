"""Agent-initiated vision: the `see_image` tool lets the agent view an
image it found or generated (not only images the user supplies). The
image rides back as a canonical block in the tool result and reaches the
wire as a base64 image inside the Anthropic tool_result content.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.cognition.providers import _to_anthropic_messages
from loom.core.session import _attach_tool_vision
from loom.platform.cli.tools import make_filesystem_tools


def _png_bytes(w: int = 4, h: int = 4) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _see_tool(workspace: Path):
    return next(t for t in make_filesystem_tools(workspace) if t.name == "see_image")


def _call(path: str) -> ToolCall:
    return ToolCall(tool_name="see_image", args={"path": path},
                    trust_level=TrustLevel.SAFE, session_id="s1")


class TestSeeImageTool:
    async def test_loads_image_returns_vision_block(self, tmp_path: Path) -> None:
        (tmp_path / "shot.png").write_bytes(_png_bytes())
        res = await _see_tool(tmp_path).executor(_call("shot.png"))
        assert res.success
        block = res.metadata["vision_image"]
        assert block["type"] == "image"
        assert block["source"]["kind"] == "file"
        assert block["source"]["media_type"] == "image/png"
        assert "已載入" in res.output

    async def test_rejects_non_image(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hi")
        res = await _see_tool(tmp_path).executor(_call("notes.txt"))
        assert not res.success
        assert "vision_image" not in (res.metadata or {})

    async def test_rejects_missing_file(self, tmp_path: Path) -> None:
        res = await _see_tool(tmp_path).executor(_call("nope.png"))
        assert not res.success

    def test_tool_is_safe(self, tmp_path: Path) -> None:
        # SAFE → pre-authorized like read_file; reading an image is read-only.
        assert _see_tool(tmp_path).trust_level == TrustLevel.SAFE


class TestToolVisionWire:
    def test_attach_rewrites_content_as_list(self) -> None:
        block = {"type": "image", "source": {
            "kind": "file", "ref": "/x.png",
            "media_type": "image/png", "digest": "sha256:a"}}
        # Reads the already-formatted tool content ("loaded"), not a raw arg.
        tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "loaded"}
        out = _attach_tool_vision(tool_msg, SimpleNamespace(metadata={"vision_image": block}))
        assert out is None  # mutator, not transformer
        assert tool_msg["content"][0] == {"type": "text", "text": "loaded"}
        assert tool_msg["content"][1] == block

    def test_attach_noop_for_plain_tool(self) -> None:
        tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        _attach_tool_vision(tool_msg, SimpleNamespace(metadata={}))
        assert tool_msg["content"] == "ok"

    def test_attach_skips_malformed_block(self) -> None:
        # A block missing source.ref must NOT be attached — otherwise it
        # would crash at wire time in load_vision_input(None). Degrade to
        # the text string instead.
        bad = {"type": "image", "source": {}}
        tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "loaded"}
        _attach_tool_vision(tool_msg, SimpleNamespace(metadata={"vision_image": bad}))
        assert tool_msg["content"] == "loaded"

    def test_attach_tolerates_non_dict_metadata(self) -> None:
        tool_msg = {"role": "tool", "tool_call_id": "t1", "content": "ok"}
        _attach_tool_vision(tool_msg, SimpleNamespace(metadata=None))
        assert tool_msg["content"] == "ok"

    def test_tool_result_image_reaches_wire_as_base64(self, tmp_path: Path) -> None:
        # The seam #506→#512 never had: an image inside a tool_result.
        p = tmp_path / "shot.png"
        p.write_bytes(_png_bytes())
        block = {"type": "image", "source": {
            "kind": "file", "ref": str(p),
            "media_type": "image/png", "digest": "sha256:a"}}
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "see_image", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1",
             "content": [{"type": "text", "text": "已載入"}, block]},
        ]
        _, anth = _to_anthropic_messages(messages)
        tr_msg = anth[-1]
        assert tr_msg["role"] == "user"
        tr = tr_msg["content"][0]
        assert tr["type"] == "tool_result"
        img = [b for b in tr["content"] if b["type"] == "image"][0]
        assert img["source"]["type"] == "base64"
        assert base64.b64decode(img["source"]["data"]) == p.read_bytes()

    def test_plain_string_tool_result_unchanged(self) -> None:
        # Regression: ordinary (non-vision) tool results still pass through
        # as a plain string, not a block list.
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "file contents"},
        ]
        _, anth = _to_anthropic_messages(messages)
        tr = anth[-1]["content"][0]
        assert tr["content"] == "file contents"
