import base64
import json
import time

import httpx
import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli import tools as cli_tools
from loom.platform.cli.tools import make_xai_tts_tool


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or (content.decode("utf-8", "replace") if status_code >= 400 else "")

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.x.ai/v1/tts")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return None


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in that records POST calls."""

    last: "_FakeAsyncClient | None" = None
    response_factory = staticmethod(lambda: _FakeResponse(content=b"audio-bytes"))

    def __init__(self, *args, **kwargs):
        self.requests: list[dict] = []
        _FakeAsyncClient.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return type(self).response_factory()


def _save_oauth(tmp_path, monkeypatch, *, token: str | None = None) -> str:
    from loom.core.cognition.xai_auth import DEFAULT_XAI_BASE_URL, save_xai_oauth_state

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    access = token or _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": access,
            "refresh_token": "refresh-secret",
        },
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    return access


@pytest.mark.asyncio
async def test_tts_writes_audio_file_with_oauth_bearer(tmp_path, monkeypatch):
    token = _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"audio-bytes")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hello world", "output_path": "out/hello.mp3"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success, result.error
    assert (tmp_path / "out" / "hello.mp3").read_bytes() == b"audio-bytes"
    output = json.loads(result.output)
    assert output["path"] == "out/hello.mp3"
    assert output["provider"] == "xai"
    assert output["voice_id"] == "eve"
    assert output["format"] == "mp3"
    assert output["bytes"] == len(b"audio-bytes")

    req = _FakeAsyncClient.last.requests[0]
    assert req["url"] == "https://api.x.ai/v1/tts"
    assert req["headers"]["Authorization"] == f"Bearer {token}"
    assert req["json"] == {
        "text": "hello world",
        "voice_id": "eve",
        "language": "auto",
        "output_format": {"codec": "mp3"},
    }


@pytest.mark.asyncio
async def test_tts_ignores_xai_api_key_when_no_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    monkeypatch.setenv("XAI_API_KEY", "xai-api-key-that-must-not-be-used")
    called: list[dict] = []

    class _NoCallClient(_FakeAsyncClient):
        async def post(self, url, headers=None, json=None):  # pragma: no cover - should not run
            called.append({"url": url, "headers": headers, "json": json})
            return _FakeResponse(content=b"audio-bytes")

    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _NoCallClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hello"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "loom auth xai" in (result.error or "")
    assert called == []
    assert "xai-api-key-that-must-not-be-used" not in (result.error or "")


@pytest.mark.asyncio
async def test_tts_rejects_non_oauth_auth_mode(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi", "auth_mode": "api_key"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "OAuth-only" in (result.error or "")


@pytest.mark.asyncio
async def test_tts_rejects_missing_text(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "   "},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "text" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_tts_rejects_unsupported_codec(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi", "format": "ogg"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "format" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_tts_passes_voice_language_format_and_speed(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"wav-bytes")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={
            "text": "你好",
            "voice_id": "ara",
            "language": "zh",
            "format": "wav",
            "speed": 1.2,
            "output_path": "renders/hi.wav",
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success, result.error
    req = _FakeAsyncClient.last.requests[0]
    assert req["json"] == {
        "text": "你好",
        "voice_id": "ara",
        "language": "zh",
        "output_format": {"codec": "wav"},
        "speed": 1.2,
    }
    output = json.loads(result.output)
    assert output["voice_id"] == "ara"
    assert output["format"] == "wav"
    assert (tmp_path / "renders" / "hi.wav").read_bytes() == b"wav-bytes"


@pytest.mark.asyncio
async def test_tts_includes_sample_rate_and_bit_rate_when_provided(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"mp3-bytes")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(
        tmp_path,
        default_sample_rate=24000,
        default_bit_rate=128000,
    )
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi", "sample_rate": 48000, "bit_rate": 192000},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success, result.error
    req = _FakeAsyncClient.last.requests[0]
    assert req["json"]["output_format"] == {
        "codec": "mp3",
        "sample_rate": 48000,
        "bit_rate": 192000,
    }


@pytest.mark.asyncio
async def test_tts_skips_bit_rate_for_non_mp3_codec(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"wav-bytes")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(
        tmp_path,
        default_sample_rate=24000,
        default_bit_rate=128000,
    )
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi", "format": "wav"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success, result.error
    output_format = _FakeAsyncClient.last.requests[0]["json"]["output_format"]
    assert output_format == {"codec": "wav", "sample_rate": 24000}
    assert "bit_rate" not in output_format


@pytest.mark.asyncio
async def test_tts_defaults_output_path_uses_codec_extension(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"pcm-bytes")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi", "format": "pcm"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success, result.error
    output = json.loads(result.output)
    assert output["path"].startswith("outputs/tts-")
    assert output["path"].endswith(".pcm")


@pytest.mark.asyncio
async def test_tts_surfaces_http_error_body(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(
        status_code=429, text="rate limited"
    )
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "429" in (result.error or "")
    assert "rate limited" in (result.error or "")


@pytest.mark.asyncio
async def test_tts_rejects_empty_audio_response(tmp_path, monkeypatch):
    _save_oauth(tmp_path, monkeypatch)
    _FakeAsyncClient.response_factory = lambda: _FakeResponse(content=b"")
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)

    tool = make_xai_tts_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"text": "hi"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "empty" in (result.error or "").lower()


def test_tts_artifact_extractor_returns_audio_metadata():
    from loom.core.harness.middleware import (
        ToolResult,
        _ARTIFACT_EXTRACTORS,
    )

    extractor = _ARTIFACT_EXTRACTORS["tts_generate"]
    call = ToolCall(
        tool_name="tts_generate",
        args={"text": "hello"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.tool_name,
        success=True,
        output=json.dumps({
            "path": "outputs/tts-abc.mp3",
            "provider": "xai",
            "voice_id": "eve",
            "format": "mp3",
            "bytes": 1234,
        }),
    )

    artifact = extractor(call, result)

    assert artifact is not None
    assert artifact["artifact_type"] == "audio"
    assert artifact["size_bytes"] == 1234
    assert artifact["location"] == "outputs/tts-abc.mp3"
    assert artifact["digest"].startswith("sha256:")
