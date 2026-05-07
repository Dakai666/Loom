import base64
import json

import httpx
import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.platform.cli import tools as cli_tools
from loom.platform.cli.tools import make_openai_image_generation_tool


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        encoded = base64.b64encode(b"png-bytes").decode()
        return _FakeResponse({"data": [{"b64_json": encoded}]})


class _FallbackAsyncClient(_FakeAsyncClient):
    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        if headers and headers.get("Authorization") == "Bearer codex-token":
            return _FakeResponse({"error": "bad token"}, status_code=401)
        encoded = base64.b64encode(b"fallback-png").decode()
        return _FakeResponse({"data": [{"b64_json": encoded}]})


@pytest.mark.asyncio
async def test_openai_image_tool_writes_generated_image(tmp_path, monkeypatch):
    from loom.core import session as session_module

    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {
        "OPENAI_API_KEY": "sk-test",
    })
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={
            "prompt": "a tiny loom logo",
            "output_path": "renders/logo.png",
            "auth_mode": "api_key",
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success
    assert (tmp_path / "renders" / "logo.png").read_bytes() == b"png-bytes"
    output = json.loads(result.output)
    assert output["credential_source"] == "api_key"
    assert output["model"] == "gpt-image-2"
    assert _FakeAsyncClient.requests[0]["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_openai_image_tool_auto_falls_back_when_codex_token_rejected(
    tmp_path,
    monkeypatch,
):
    from loom.core import session as session_module

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "codex-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {
        "OPENAI_API_KEY": "sk-fallback",
    })
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FallbackAsyncClient)
    _FallbackAsyncClient.requests = []
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"prompt": "x", "output_path": "renders/fallback.png"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success
    assert (tmp_path / "renders" / "fallback.png").read_bytes() == b"fallback-png"
    output = json.loads(result.output)
    assert output["credential_source"] == "api_key"
    assert [r["headers"]["Authorization"] for r in _FallbackAsyncClient.requests] == [
        "Bearer codex-token",
        "Bearer sk-fallback",
    ]


@pytest.mark.asyncio
async def test_openai_image_tool_requires_codex_when_requested(tmp_path, monkeypatch):
    from loom.core import session as session_module

    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"prompt": "x", "auth_mode": "codex"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "Codex OAuth token" in result.error
