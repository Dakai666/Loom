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
    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})

        class _Stream:
            async def __aenter__(self_inner):
                request = httpx.Request(method, url)
                response = httpx.Response(401, request=request, text="bad token")
                raise httpx.HTTPStatusError("bad token", request=request, response=response)

            async def __aexit__(self_inner, *exc):
                return False

        return _Stream()

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        encoded = base64.b64encode(b"fallback-png").decode()
        return _FakeResponse({"data": [{"b64_json": encoded}]})


class _CodexStreamAsyncClient(_FakeAsyncClient):
    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        encoded = base64.b64encode(b"codex-png").decode()

        class _Stream:
            status_code = 200

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            def raise_for_status(self_inner):
                return None

            async def aiter_lines(self_inner):
                payload = json_module.dumps({
                    "type": "response.image_generation_call.partial_image",
                    "partial_image_b64": encoded,
                })
                for line in [
                    "event: response.image_generation_call.partial_image",
                    f"data: {payload}",
                    "",
                    "event: response.completed",
                    "data: {\"type\":\"response.completed\"}",
                    "",
                ]:
                    yield line

        return _Stream()


json_module = json


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
async def test_openai_image_tool_uses_codex_responses_backend(tmp_path, monkeypatch):
    from loom.core import session as session_module

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "codex-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _CodexStreamAsyncClient)
    _CodexStreamAsyncClient.requests = []
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={"prompt": "x", "output_path": "renders/codex.png", "auth_mode": "codex"},
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success
    assert (tmp_path / "renders" / "codex.png").read_bytes() == b"codex-png"
    output = json.loads(result.output)
    assert output["credential_source"] == "codex_oauth"
    request = _CodexStreamAsyncClient.requests[0]
    assert request["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert request["json"]["model"] == "gpt-5.5"
    assert request["json"]["tools"][0]["model"] == "gpt-image-2"


@pytest.mark.asyncio
async def test_openai_image_tool_includes_subject_reference_in_codex_payload(
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
    reference = tmp_path / "refs" / "siluyi.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference-png")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _CodexStreamAsyncClient)
    _CodexStreamAsyncClient.requests = []
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={
            "prompt": "draw the same character",
            "output_path": "renders/codex-ref.png",
            "auth_mode": "codex",
            "subject_reference": [
                {"type": "character", "image_file": "refs/siluyi.png"},
            ],
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success
    content = _CodexStreamAsyncClient.requests[0]["json"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "draw the same character"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert content[1]["image_url"].endswith(base64.b64encode(b"reference-png").decode())


@pytest.mark.asyncio
async def test_openai_image_tool_rejects_subject_reference_on_api_key_path(
    tmp_path,
    monkeypatch,
):
    from loom.core import session as session_module

    reference = tmp_path / "refs" / "siluyi.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference-png")
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {
        "OPENAI_API_KEY": "sk-test",
    })
    monkeypatch.setattr(cli_tools.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.requests = []
    tool = make_openai_image_generation_tool(tmp_path)
    call = ToolCall(
        tool_name=tool.name,
        args={
            "prompt": "draw",
            "auth_mode": "api_key",
            "subject_reference": [{"image_file": "refs/siluyi.png"}],
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert not result.success
    assert "subject_reference currently requires Codex OAuth" in result.error
    assert _FakeAsyncClient.requests == []


def test_openai_image_tool_schema_and_scope_include_subject_reference(tmp_path):
    tool = make_openai_image_generation_tool(tmp_path)
    reference = tmp_path / "refs" / "siluyi.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference-png")
    call = ToolCall(
        tool_name=tool.name,
        args={
            "prompt": "draw",
            "output_path": "renders/out.png",
            "subject_reference": [{"image_file": "refs/siluyi.png"}],
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    assert "subject_reference" in tool.input_schema["properties"]
    scope = tool.scope_resolver(call)

    assert any(
        req.resource == "path"
        and req.action == "read"
        and req.selector == "refs/siluyi.png"
        for req in scope.requirements
    )


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
