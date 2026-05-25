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
async def test_xai_provider_uses_output_text_for_assistant_history(tmp_path, monkeypatch):
    """Mirrors the Codex Responses contract — assistant content must be
    ``output_text``, user content ``input_text``."""
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
    fake_client = _FakeAsyncClient([
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":0,"output_tokens":0}}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    await provider.chat(messages=[
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second user"},
    ])

    input_items = fake_client.requests[0]["json"]["input"]
    assert [item["role"] for item in input_items] == ["user", "assistant", "user"]
    assert input_items[0]["content"][0]["type"] == "input_text"
    assert input_items[1]["content"][0]["type"] == "output_text"
    assert input_items[2]["content"][0]["type"] == "input_text"


@pytest.mark.asyncio
async def test_xai_provider_does_not_leak_function_call_args_into_text(tmp_path, monkeypatch):
    """Mirror of the Codex regression — function_call_arguments.delta
    must not bleed into assistant text."""
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
    fake_client = _FakeAsyncClient([
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"thinking"}',
        "",
        "event: response.function_call_arguments.delta",
        'data: {"type":"response.function_call_arguments.delta","delta":"{\\"q\\":"}',
        "",
        "event: response.function_call_arguments.delta",
        'data: {"type":"response.function_call_arguments.delta","delta":"\\"loom\\"}"}',
        "",
        "event: response.output_item.done",
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"search","arguments":"{\\"q\\":\\"loom\\"}"}}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    response = await provider.chat(messages=[{"role": "user", "content": "search loom"}])

    assert response.text == "thinking"
    assert '"q"' not in (response.text or "")
    assert len(response.tool_uses) == 1
    assert response.tool_uses[0].args == {"q": "loom"}


class _XAIStreamDropResponse:
    """Simulates an SSE peer closing the connection mid-stream."""

    status_code = 200

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        import httpx

        yield "event: response.output_text.delta"
        yield 'data: {"type":"response.output_text.delta","delta":"par"}'
        yield ""
        raise httpx.RemoteProtocolError("")


class _XAIStreamDropAsyncClient:
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
                return _XAIStreamDropResponse()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_xai_provider_raises_meaningful_error_on_stream_drop(tmp_path, monkeypatch):
    """Mid-stream RemoteProtocolError with empty message must not surface
    as ``turn aborted with error: `` (empty after colon)."""
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
    monkeypatch.setattr(httpx, "AsyncClient", _XAIStreamDropAsyncClient)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    with pytest.raises(RuntimeError) as excinfo:
        await provider.chat(messages=[{"role": "user", "content": "ping"}])

    message = str(excinfo.value)
    assert "xAI Responses stream interrupted" in message
    assert "RemoteProtocolError" in message
    assert message.strip().endswith("RemoteProtocolError")


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
async def test_xai_provider_surfaces_sse_response_failed(tmp_path, monkeypatch):
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
    fake_client = _FakeAsyncClient([
        "event: response.failed",
        (
            'data: {"type":"response.failed","response":{"status":"failed",'
            '"error":{"code":"context_length_exceeded",'
            '"message":"input is too large"}}}'
        ),
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    with pytest.raises(RuntimeError) as excinfo:
        await provider.chat(messages=[{"role": "user", "content": "ping"}])

    message = str(excinfo.value)
    assert "xAI Responses response error" in message
    assert "context_length_exceeded" in message
    assert "input is too large" in message


@pytest.mark.asyncio
async def test_xai_provider_keeps_max_output_incomplete_recoverable(tmp_path, monkeypatch):
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
    fake_client = _FakeAsyncClient([
        "event: response.incomplete",
        (
            'data: {"type":"response.incomplete","response":{"status":"incomplete",'
            '"incomplete_details":{"reason":"max_output_tokens"}}}'
        ),
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert response.stop_reason == "max_tokens"


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


@pytest.mark.asyncio
async def test_xai_provider_omits_reasoning_block_by_default(tmp_path, monkeypatch):
    """xAI Grok-4.3 already streams text during reasoning; injecting the
    ``reasoning`` field by default risks a 400 if compat tightens and gains
    nothing visible. Preserve the working payload — opt-in only via config.
    """
    import httpx

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {"access_token": token, "refresh_token": "refresh-secret"},
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    fake_client = _FakeAsyncClient([
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert "reasoning" not in fake_client.requests[0]["json"]


@pytest.mark.asyncio
async def test_xai_provider_payload_includes_reasoning_when_configured(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {"access_token": token, "refresh_token": "refresh-secret"},
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    fake_client = _FakeAsyncClient([
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(
        model="xai/grok-4.3", reasoning_effort="high",
    )

    await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert fake_client.requests[0]["json"]["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }


@pytest.mark.asyncio
async def test_xai_provider_wraps_reasoning_summary_in_think_tags(tmp_path, monkeypatch):
    """If xAI ever does emit reasoning_summary deltas, surface them via the
    same ``<think>…</think>`` protocol Codex uses.
    """
    import httpx

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {"access_token": token, "refresh_token": "refresh-secret"},
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    fake_client = _FakeAsyncClient([
        "event: response.reasoning_summary_text.delta",
        'data: {"type":"response.reasoning_summary_text.delta","delta":"weighing"}',
        "",
        "event: response.reasoning_summary_part.done",
        'data: {"type":"response.reasoning_summary_part.done"}',
        "",
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"reply"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = XAIResponsesProvider(model="xai/grok-4.3")

    chunks: list[str] = []
    async for delta, final in provider.stream_chat(
        messages=[{"role": "user", "content": "ping"}],
    ):
        if final is None and delta:
            chunks.append(delta)

    joined = "".join(chunks)
    assert "<think>weighing</think>" in joined
    assert joined.endswith("reply")
    # Stream-order invariant: close-think before output text.
    close_idx = next(i for i, c in enumerate(chunks) if "</think>" in c)
    reply_idx = next(i for i, c in enumerate(chunks) if c == "reply")
    assert close_idx < reply_idx
