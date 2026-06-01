"""Tests for loom.core.cognition.vision and the vision input wiring.

Covers:

* magic-byte MIME detection
* size cap
* provider-neutral block conversion
* Anthropic / Responses API / OpenAI adapters
* CLI path detection
* Memory: list content is normalised to a base64-free summary
"""

from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path

import pytest

from loom.core.cognition import vision
from loom.core.cognition.providers import _to_anthropic_messages
from loom.core.cognition.codex_responses_provider import _build_responses_content
from loom.core.memory.session_log import _strip_vision_blocks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    """Build a minimal valid PNG without any external library."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Each scanline: 1 filter byte + 4 bytes RGB × width
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_jpeg_bytes() -> bytes:
    """Minimal valid JPEG (SOI + APP0 + EOI)."""
    # JFIF marker
    app0 = (
        b"JFIF\x00"
        b"\x01\x01"  # version
        b"\x00"  # units
        b"\x00\x01\x00\x01"  # density
        b"\x00\x00"  # thumbnail
    )
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0 + b"\xff\xd9"


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "fixture.png"
    p.write_bytes(_make_png_bytes())
    return p


@pytest.fixture
def jpeg_path(tmp_path: Path) -> Path:
    p = tmp_path / "fixture.jpg"
    p.write_bytes(_make_jpeg_bytes())
    return p


@pytest.fixture
def text_path(tmp_path: Path) -> Path:
    p = tmp_path / "notes.txt"
    p.write_text("hello world", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestMagicByteDetection:
    def test_detects_png(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        assert vi.media_type == "image/png"

    def test_detects_jpeg(self, jpeg_path: Path) -> None:
        vi = vision.load_vision_input(jpeg_path)
        assert vi.media_type == "image/jpeg"

    def test_rejects_text(self, text_path: Path) -> None:
        with pytest.raises(vision.VisionInputError):
            vision.load_vision_input(text_path)

    def test_rejects_extension_spoofing(self, tmp_path: Path) -> None:
        # PNG bytes saved as .txt — must still detect PNG content
        p = tmp_path / "spoof.txt"
        p.write_bytes(_make_png_bytes())
        vi = vision.load_vision_input(p)
        assert vi.media_type == "image/png"

    def test_rejects_oversize(self, tmp_path: Path) -> None:
        # Create a PNG with a too-big IDAT chunk
        big = _make_png_bytes() + b"\x00" * (vision.MAX_SIZE_BYTES + 1)
        p = tmp_path / "huge.png"
        p.write_bytes(big)
        with pytest.raises(vision.VisionInputError):
            vision.load_vision_input(p)

    def test_rejects_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.png"
        p.write_bytes(b"")
        with pytest.raises(vision.VisionInputError):
            vision.load_vision_input(p)


class TestDigest:
    def test_stable_for_same_bytes(self, png_path: Path) -> None:
        a = vision.load_vision_input(png_path)
        b = vision.load_vision_input(png_path)
        assert a.digest == b.digest
        assert a.digest.startswith("sha256:")

    def test_distinct_for_distinct_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_bytes(_make_png_bytes(width=2))
        (tmp_path / "b.png").write_bytes(_make_png_bytes(width=8))
        a_digest = vision.load_vision_input(tmp_path / "a.png").digest
        b_digest = vision.load_vision_input(tmp_path / "b.png").digest
        assert a_digest != b_digest


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


class TestAnthropicAdapter:
    def test_file_to_base64_block(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        block = vision.to_anthropic_image_block(vi)
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        assert isinstance(block["source"]["data"], str)
        # base64 should round-trip back to the original bytes
        import base64
        assert base64.b64decode(block["source"]["data"]) == png_path.read_bytes()

    def test_url_to_url_block(self) -> None:
        vi = vision.VisionInput(
            source=vision.VisionSource(kind="url", ref="https://example.com/x.png"),
        )
        block = vision.to_anthropic_image_block(vi)
        assert block["source"]["type"] == "url"
        assert block["source"]["url"] == "https://example.com/x.png"


class TestOpenAIAdapter:
    def test_file_to_data_uri(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        block = vision.to_openai_image_block(vi)
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")

    def test_url_passthrough(self) -> None:
        vi = vision.VisionInput(
            source=vision.VisionSource(kind="url", ref="https://example.com/x.png"),
        )
        block = vision.to_openai_image_block(vi)
        assert block["image_url"]["url"] == "https://example.com/x.png"


class TestResponsesAdapter:
    def test_input_image_shape(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        block = vision.to_responses_image_block(vi)
        assert block["type"] == "input_image"
        assert block["image_url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Canonical content builder
# ---------------------------------------------------------------------------


class TestBuildUserContent:
    def test_text_only_returns_str(self) -> None:
        out = vision.build_user_content("hello")
        assert out == "hello"

    def test_text_plus_image_returns_list(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        out = vision.build_user_content("hi", [vi])
        assert isinstance(out, list)
        assert out[0]["type"] == "text"
        assert out[0]["text"] == "hi"
        assert out[1]["type"] == "image"
        assert out[1]["source"]["digest"] == vi.digest
        # Regression for #506 review by CC: file refs must be preserved
        # in the canonical block so the provider adapter can re-read
        # the image at wire time. Previously this was nulled out and
        # the CLI path-detection round-trip would crash.
        assert out[1]["source"]["kind"] == "file"
        assert out[1]["source"]["ref"] == str(png_path)

    def test_url_ref_preserved(self) -> None:
        vi = vision.VisionInput(
            source=vision.VisionSource(kind="url", ref="https://example.com/x.png"),
        )
        out = vision.build_user_content("see", [vi])
        assert out[1]["source"]["ref"] == "https://example.com/x.png"

    def test_raw_ref_dropped(self, png_path: Path) -> None:
        # Use real PNG bytes from the fixture so magic-byte detection
        # passes through validate_vision_input.
        vi = vision.VisionInput(
            source=vision.VisionSource(kind="raw", ref=png_path.read_bytes()),
        )
        out = vision.build_user_content("see", [vi])
        # raw bytes are not JSON-serialisable; ref is intentionally None.
        assert out[1]["source"]["ref"] is None


class TestBuildUserContentRoundTrips:
    """End-to-end: build_user_content -> provider adapter.

    These exist because the unit tests for each adapter build canonical
    blocks by hand and never go through build_user_content. That gap
    let a bug ship where file refs were nulled at the build step
    (caught by CC in #506 review).
    """

    def test_anthropic_round_trip(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        content = vision.build_user_content("看下這張", [vi])
        _, anth = _to_anthropic_messages(
            [{"role": "user", "content": content}]
        )
        anth_content = anth[0]["content"]
        assert isinstance(anth_content, list)
        img = anth_content[1]
        assert img["type"] == "image"
        assert img["source"]["type"] == "base64"
        import base64
        assert base64.b64decode(img["source"]["data"]) == png_path.read_bytes()

    def test_responses_round_trip(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        content = vision.build_user_content("看下這張", [vi])
        blocks = _build_responses_content(content, "user")
        assert blocks[0] == {"type": "input_text", "text": "看下這張"}
        assert blocks[1]["type"] == "input_image"
        assert blocks[1]["image_url"].startswith("data:image/png;base64,")

    def test_full_cli_path_detection_round_trip(self, png_path: Path) -> None:
        """Reproduce the exact CLI flow: str input with image path ->
        extract_image_paths -> build_user_content -> provider adapter.

        This is the acceptance criterion #2 from #505 and was the path
        CC's review caught as broken.
        """
        user_input = f"看下 {png_path} 謝謝"
        detected = vision.extract_image_paths(user_input, base=png_path.parent)
        assert len(detected) == 1
        images = [vision.load_vision_input(p, origin="user") for p in detected]
        content = vision.build_user_content(user_input, images)
        # Should not raise.
        _, anth = _to_anthropic_messages(
            [{"role": "user", "content": content}]
        )
        assert anth[0]["content"][1]["source"]["type"] == "base64"


# ---------------------------------------------------------------------------
# CLI path detection
# ---------------------------------------------------------------------------


class TestExtractImagePaths:
    def test_finds_image_paths(self, tmp_path: Path) -> None:
        a = tmp_path / "a.png"
        a.write_bytes(_make_png_bytes())
        b = tmp_path / "b.jpg"
        b.write_bytes(_make_jpeg_bytes())
        text = f"看下 {a} 還有 {b} 謝謝"
        paths = vision.extract_image_paths(text, base=tmp_path)
        assert a.resolve() in paths
        assert b.resolve() in paths

    def test_ignores_non_image_extensions(self, tmp_path: Path) -> None:
        t = tmp_path / "x.txt"
        t.write_text("hi")
        paths = vision.extract_image_paths(f"see {t}", base=tmp_path)
        assert paths == []

    def test_ignores_nonexistent(self, tmp_path: Path) -> None:
        text = "see /no/such/file.png"
        paths = vision.extract_image_paths(text, base=tmp_path)
        assert paths == []

    def test_dedup(self, tmp_path: Path) -> None:
        a = tmp_path / "a.png"
        a.write_bytes(_make_png_bytes())
        paths = vision.extract_image_paths(f"{a} and again {a}", base=tmp_path)
        assert len(paths) == 1


# ---------------------------------------------------------------------------
# End-to-end: list content through each provider
# ---------------------------------------------------------------------------


class TestAnthropicMessages:
    def test_str_content_unchanged(self) -> None:
        sys_text, anth = _to_anthropic_messages([
            {"role": "user", "content": "hello"},
        ])
        assert sys_text is None
        assert anth[0]["content"] == "hello"

    def test_list_content_emits_image_block(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "看下這張"},
                {"type": "image", "source": {
                    "kind": "file", "ref": str(png_path),
                    "media_type": vi.media_type, "digest": vi.digest,
                }},
            ],
        }
        sys_text, anth = _to_anthropic_messages([msg])
        anth_content = anth[0]["content"]
        assert isinstance(anth_content, list)
        assert anth_content[0] == {"type": "text", "text": "看下這張"}
        assert anth_content[1]["type"] == "image"
        assert anth_content[1]["source"]["type"] == "base64"

    def test_url_image_passthrough(self) -> None:
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看"},
                {"type": "image", "source": {
                    "kind": "url", "ref": "https://example.com/x.png",
                    "media_type": "image/png", "digest": "sha256:x",
                }},
            ],
        }
        _, anth = _to_anthropic_messages([msg])
        assert anth[0]["content"][1]["source"]["type"] == "url"


class TestResponsesContent:
    def test_text_only(self) -> None:
        out = _build_responses_content("hello", "user")
        assert out == [{"type": "input_text", "text": "hello"}]

    def test_text_and_image(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        content = [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {
                "kind": "file", "ref": str(png_path),
                "media_type": vi.media_type, "digest": vi.digest,
            }},
        ]
        out = _build_responses_content(content, "user")
        assert out[0] == {"type": "input_text", "text": "hi"}
        assert out[1]["type"] == "input_image"
        assert out[1]["image_url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Memory: list content normalises without base64
# ---------------------------------------------------------------------------


class TestStripVisionBlocks:
    def test_text_preserved(self) -> None:
        text, md = _strip_vision_blocks(
            [{"type": "text", "text": "hello"}], {}
        )
        assert text == "hello"
        assert "vision_inputs" not in md

    def test_image_emits_metadata_no_base64(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        text, md = _strip_vision_blocks([
            {"type": "text", "text": "看圖"},
            {"type": "image", "source": {
                "kind": "file", "ref": str(png_path),
                "media_type": vi.media_type, "digest": vi.digest,
            }},
        ], {"foo": "bar"})
        assert text == "看圖"
        assert md["foo"] == "bar"  # existing metadata preserved
        assert "vision_inputs" in md
        assert md["vision_inputs"][0]["digest"] == vi.digest
        # base64 must NOT appear in the output anywhere
        assert "base64," not in json.dumps({"text": text, "md": md})
        assert "iVBORw" not in json.dumps({"text": text, "md": md})

    def test_only_image_uses_placeholder(self, png_path: Path) -> None:
        vi = vision.load_vision_input(png_path)
        text, md = _strip_vision_blocks([
            {"type": "image", "source": {
                "kind": "file", "ref": str(png_path),
                "media_type": vi.media_type, "digest": vi.digest,
            }},
        ], {})
        assert text == "[vision input: 1 image(s)]"
        assert len(md["vision_inputs"]) == 1
