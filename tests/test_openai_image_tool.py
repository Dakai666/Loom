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
    assert request["json"]["model"] == "gpt-6.1-sol"
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
async def test_openai_image_tool_detects_reference_mime_without_extension(
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
    reference = tmp_path / "refs" / "portrait"
    reference.parent.mkdir()
    reference.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
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
            "subject_reference": [{"image_file": "refs/portrait"}],
        },
        trust_level=TrustLevel.GUARDED,
        session_id="s1",
    )

    result = await tool.executor(call)

    assert result.success
    content = _CodexStreamAsyncClient.requests[0]["json"]["input"][0]["content"]
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")


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
    assert "You selected auth_mode=api_key" in result.error
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


def test_openai_image_tool_registers_as_canonical_image_generate(tmp_path):
    tool = make_openai_image_generation_tool(
        tmp_path, default_model="gpt-image-custom"
    )

    assert tool.name == "image_generate"
    assert tool.tags == ["image", "openai"]
    assert "gpt-image-custom" in tool.description


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


@pytest.fixture
def codex_image_probe(tmp_path, monkeypatch):
    from loom.core import session as session_module
    codex_home = tmp_path / 'codex'
    codex_home.mkdir()
    (codex_home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'codex-test-token'}}))
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    monkeypatch.setattr(session_module, '_load_env', lambda: {'OPENAI_API_KEY': 'must-not-fallback'})
    monkeypatch.setattr(session_module, '_load_loom_config', lambda: {})
    actual_client = httpx.AsyncClient
    requests = []

    async def run(body, *, status=200, args=None):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield body.encode()
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(status, stream=Body())
        monkeypatch.setattr(cli_tools.httpx, 'AsyncClient', lambda **kw: actual_client(
            transport=httpx.MockTransport(transport), **kw))
        tool = make_openai_image_generation_tool(tmp_path)
        call = ToolCall(tool_name=tool.name, args={'prompt': 'synthetic probe', 'auth_mode': 'codex',
                        'output_path': 'result.png', **(args or {})}, trust_level=TrustLevel.GUARDED, session_id='test')
        return await tool.executor(call), tool
    return run, requests


def _image_event(kind, **data):
    return f'event: {kind}\ndata: {json.dumps({"type": kind, **data})}\n\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('final_in_completed', [False, True])
async def test_codex_image_reads_official_final_result_without_partial_images(tmp_path, codex_image_probe, final_in_completed):
    run, requests = codex_image_probe
    item = {'type': 'image_generation_call', 'status': 'completed',
            'result': base64.b64encode(b'final-image-bytes').decode()}
    body = '' if final_in_completed else _image_event('response.output_item.done', item=item)
    body += _image_event('response.completed', response={'status': 'completed',
                          'output': [item] if final_in_completed else []})
    result, _ = await run(body)
    assert result.success
    assert (tmp_path / 'result.png').read_bytes() == b'final-image-bytes'
    assert json.loads(result.output)['credential_source'] == 'codex_oauth'
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ending,expected', [
    ('', 'without response.completed'),
    (_image_event('response.failed', response={'error': {'code': 'image_limit', 'message': 'image quota reached'}}), 'image quota reached'),
    (_image_event('response.incomplete', response={'status': 'incomplete'}), 'incomplete'),
    (_image_event('response.completed', response={'status': 'failed'}), 'status=failed'),
])
async def test_codex_image_never_saves_partial_on_failed_or_unfinished_stream(tmp_path, codex_image_probe, ending, expected):
    run, requests = codex_image_probe
    body = _image_event('response.image_generation_call.partial_image',
                        partial_image_b64=base64.b64encode(b'partial').decode()) + ending
    result, _ = await run(body)
    assert not result.success
    assert expected in result.error
    assert not (tmp_path / 'result.png').exists()
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,override,expected', [
    ('codex/configured-model', None, 'configured-model'),
    ('configured-model', 'codex/override-model', 'override-model'),
    ('configured-model', '  ', 'configured-model'),
])
async def test_codex_image_model_follows_provider_config_and_valid_schema_override(monkeypatch, codex_image_probe, configured, override, expected):
    from loom.core import session as session_module
    from jsonschema import validate
    monkeypatch.setattr(session_module, '_load_loom_config', lambda: {'providers': {'codex': {'default_model': configured}}})
    run, requests = codex_image_probe
    item = {'type': 'image_generation_call', 'result': base64.b64encode(b'image').decode()}
    args = {'codex_model': override} if override is not None else {}
    result, tool = await run(_image_event('response.completed', response={'status': 'completed', 'output': [item]}), args=args)
    assert result.success
    assert requests[0]['model'] == expected
    validate({'prompt': 'draw', **args}, tool.input_schema)
    assert tool.input_schema['properties']['codex_model']['default'] == configured.removeprefix('codex/')


@pytest.mark.asyncio
async def test_codex_image_stream_http_error_preserves_real_body_without_api_fallback(tmp_path, codex_image_probe):
    run, requests = codex_image_probe
    result, _ = await run('{"error":"model_not_found codex-test-token"}', status=400)
    assert not result.success
    assert 'HTTP 400' in result.error and 'model_not_found' in result.error
    assert 'codex-test-token' not in result.error
    assert 'ResponseNotRead' not in result.error
    assert len(requests) == 1
    assert not (tmp_path / 'result.png').exists()


def test_image_decoder_ignores_unrelated_result_text():
    assert cli_tools._extract_image_b64({'type': 'function_call_output', 'result': 'not-an-image'}) == ''
