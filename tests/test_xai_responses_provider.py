import base64
import json
import time

import pytest

from loom.core.cognition.providers import CodexResponsesProvider, XAIResponsesProvider
from loom.core.cognition.xai_auth import DEFAULT_XAI_BASE_URL, save_xai_oauth_state


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


def test_xai_provider_does_not_inherit_codex_provider():
    assert not issubclass(XAIResponsesProvider, CodexResponsesProvider)


class _StreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, lines):
        self.lines = lines
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})

        class _Ctx:
            async def __aenter__(self_inner):
                return _StreamResponse(self.lines)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_xai_provider_streams_with_oauth_token_not_api_key(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    monkeypatch.setenv("XAI_API_KEY", "xai-api-key-that-must-not-be-used")
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-secret",
        },
        "base_url": DEFAULT_XAI_BASE_URL,
    })

    fake_client = _FakeAsyncClient([
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"pong"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":4}}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert response.text == "pong"
    req = fake_client.requests[0]
    assert req["url"] == "https://api.x.ai/v1/responses"
    assert req["headers"]["Authorization"] == f"Bearer {token}"
    assert "xai-api-key-that-must-not-be-used" not in json.dumps(req)
    assert req["json"]["model"] == "grok-4.3"
    assert req["json"]["max_output_tokens"] == 8096


class _XAIErrorStreamResponse:
    """Streaming-aware mock that mirrors httpx.ResponseNotRead semantics."""

    status_code = 401
    _body = '{"detail":"invalid bearer token"}'

    def __init__(self):
        self._read = False

    async def aread(self):
        self._read = True

    @property
    def text(self):
        if not self._read:
            import httpx

            raise httpx.ResponseNotRead()
        return self._body

    def raise_for_status(self):
        import httpx

        request = httpx.Request("POST", "https://api.x.ai/v1/responses")
        raise httpx.HTTPStatusError(
            "Client error '401 Unauthorized' for url",
            request=request,
            response=self,
        )

    async def aiter_lines(self):
        if False:
            yield ""


class _XAIErrorAsyncClient:
    def __init__(self, *args, **kwargs):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})

        class _Ctx:
            async def __aenter__(self_inner):
                return _XAIErrorStreamResponse()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_xai_provider_surfaces_streaming_backend_error(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-secret",
        },
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    monkeypatch.setattr(httpx, "AsyncClient", _XAIErrorAsyncClient)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    with pytest.raises(RuntimeError) as excinfo:
        await provider.chat(messages=[{"role": "user", "content": "ping"}])

    message = str(excinfo.value)
    assert "401" in message
    assert "invalid bearer token" in message
    assert "without having called" not in message


@pytest.mark.asyncio
async def test_xai_router_rejects_config_base_url_before_streaming(tmp_path, monkeypatch):
    import httpx
    from loom.core import session as session_module

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-secret",
        },
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {
        "cognition": {"default_model": "xai/grok-4.3"},
        "providers": {
            "xai_oauth": {
                "enabled": True,
                "base_url": "https://attacker.example/v1/responses",
            },
        },
    })
    fake_client = _FakeAsyncClient([
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"pong"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":4}}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

    provider = session_module.build_router().get_provider("xai/grok-4.3")
    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert response.text == "pong"
    req = fake_client.requests[0]
    assert req["url"] == "https://api.x.ai/v1/responses"
    assert req["headers"]["Authorization"] == f"Bearer {token}"
