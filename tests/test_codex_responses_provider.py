import base64
import json
import time

import pytest

from loom.core.cognition.providers import CodexResponsesProvider


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


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


class _ErrorStreamResponse:
    """Mock that mirrors httpx streaming-response semantics.

    A streaming response raises ``httpx.ResponseNotRead`` when ``.text``
    is accessed before ``aread()``. ``raise_for_status()`` itself does
    *not* read the body, so the exception carries this same streaming
    response — accessing its ``.text`` blindly is the bug PR #449's
    original handler had.
    """

    status_code = 400
    _body = '{"detail":"Unsupported parameter: max_output_tokens"}'

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

        request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request' for url",
            request=request,
            response=self,
        )

    async def aiter_lines(self):
        if False:
            yield ""


class _FakeErrorAsyncClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})

        class _Ctx:
            async def __aenter__(self_inner):
                return _ErrorStreamResponse()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": _jwt(int(time.time()) + 3600)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_codex_provider_streams_text(monkeypatch, codex_home):
    import httpx

    fake_client = _FakeAsyncClient([
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"pong"}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":4}}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = CodexResponsesProvider(model="codex/gpt-5.5")

    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    assert response.text == "pong"
    assert response.input_tokens == 3
    assert response.output_tokens == 4
    req = fake_client.requests[0]
    assert req["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert req["json"]["model"] == "gpt-5.5"
    assert req["json"]["store"] is False
    assert req["json"]["stream"] is True
    assert "max_output_tokens" not in req["json"]


@pytest.mark.asyncio
async def test_codex_provider_normalizes_tool_call(monkeypatch, codex_home):
    import httpx

    fake_client = _FakeAsyncClient([
        "event: response.output_item.done",
        'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}',
        "",
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = CodexResponsesProvider(model="codex/gpt-5.5")

    response = await provider.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[{
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }],
    )

    assert response.stop_reason == "tool_use"
    assert response.tool_uses[0].id == "call_1"
    assert response.tool_uses[0].name == "read_file"
    assert response.tool_uses[0].args == {"path": "README.md"}
    assert response.raw_message["tool_calls"][0]["function"]["name"] == "read_file"
    assert fake_client.requests[0]["json"]["tools"][0]["type"] == "function"


@pytest.mark.asyncio
async def test_codex_provider_uses_output_text_for_assistant_history(monkeypatch, codex_home):
    """Codex Responses backend rejects ``input_text`` in assistant slots
    with HTTP 400 ``Supported values are: 'output_text' and 'refusal'``.
    History replay must use ``output_text`` for the assistant role and
    ``input_text`` for the user role."""
    import httpx

    fake_client = _FakeAsyncClient([
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":0,"output_tokens":0}}}',
        "",
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = CodexResponsesProvider(model="codex/gpt-5.5")

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


class _StreamDropResponse:
    """Simulates an SSE peer closing the connection mid-stream.

    httpx raises ``RemoteProtocolError`` from ``aiter_lines`` when h11
    sees a premature peer close. Some of those instances carry an empty
    ``str(exc)`` — previously that surfaced as a bare
    ``turn aborted with error: `` with nothing after the colon.
    """

    status_code = 200

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        import httpx

        # Yield a partial event, then raise — mirrors a real peer-close.
        yield "event: response.output_text.delta"
        yield 'data: {"type":"response.output_text.delta","delta":"par"}'
        yield ""
        raise httpx.RemoteProtocolError("")


class _StreamDropAsyncClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})

        class _Ctx:
            async def __aenter__(self_inner):
                return _StreamDropResponse()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_codex_provider_raises_meaningful_error_on_stream_drop(monkeypatch, codex_home):
    """Mid-stream RemoteProtocolError with empty message must not surface
    as ``turn aborted with error: `` (empty after colon)."""
    import httpx

    fake_client = _StreamDropAsyncClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = CodexResponsesProvider(model="codex/gpt-5.5")

    with pytest.raises(RuntimeError) as excinfo:
        await provider.chat(messages=[{"role": "user", "content": "ping"}])

    message = str(excinfo.value)
    assert "Codex Responses stream interrupted" in message
    # Falls back to the exception class name when the underlying message
    # is empty — never leaves the user with a bare colon.
    assert "RemoteProtocolError" in message
    assert message.strip().endswith("RemoteProtocolError")


@pytest.mark.asyncio
async def test_codex_provider_surfaces_backend_error_body(monkeypatch, codex_home):
    import httpx

    fake_client = _FakeErrorAsyncClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)
    provider = CodexResponsesProvider(model="codex/gpt-5.5")

    with pytest.raises(RuntimeError) as excinfo:
        await provider.chat(messages=[{"role": "user", "content": "ping"}])

    message = str(excinfo.value)
    assert "Unsupported parameter: max_output_tokens" in message
    assert "HTTP 400" in message
    # Guard against regression of the `.text` on streaming-response bug —
    # the helper must call ``aread()`` before reading ``.text``, otherwise
    # this string from httpx leaks out and masks the real backend error.
    assert "without having called" not in message
