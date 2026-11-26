"""OpenCode Go provider tests.

OpenCode Go (provider id ``opencode-go``) is DK's OpenCode Console
subscription gateway: an OpenAI-compatible endpoint at
``https://opencode.ai/inference/go/openai/v1`` fronting DeepSeek / GLM /
Qwen / Kimi / MiniMax / … models with one personal API key.

Rules under test:

* OpenAI-compatible streaming (text, tool calls, usage) matching the
  behaviour of the other ``_OpenAICompatibleBase`` providers.
* Auth comes ONLY from ``OPENCODE_API_KEY``; a missing/blank key raises a
  clear configuration error and never sends a request with an empty key.
* Every request carries Loom's User-Agent and a stable
  ``x-opencode-session`` header (per conversation) as the docs require.
* Model ids pass through verbatim — Loom only strips its own ``opencode/``
  routing prefix, and the provider does not hard-code a model list.
* 401 / 403 / 429 surface readable errors that never echo the key; the
  429 message mentions the Go allowance and the usage endpoint.

All tests are offline: ``openai.AsyncOpenAI`` is replaced with a fake.
"""

from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import Any, ClassVar

import httpx
import openai
import pytest

from loom.core.cognition.providers import OpenCodeGoProvider
from loom.core.cognition.router import LLMRouter

OPENCGO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
FAKE_KEY = "sk-test-opencode-fake-key"


# ---------------------------------------------------------------------------
# Offline fake for openai.AsyncOpenAI
# ---------------------------------------------------------------------------

class _FakeStream:
    """Minimal stand-in for the openai SDK's async completion stream."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self._iter: Any = None
        self.closed = False

    def __aiter__(self) -> "_FakeStream":
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self.closed = True


class _FakeAsyncOpenAI:
    """Records constructor kwargs and chat.completions.create() kwargs."""

    # Per-test configuration (applied to the next instance created).
    chunks: ClassVar[list[Any]] = []
    error: ClassVar[BaseException | None] = None
    instances: ClassVar[list["_FakeAsyncOpenAI"]] = []

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: Any = None,
        default_headers: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.default_headers = default_headers
        self.extra_kwargs = kwargs
        self.requests: list[dict] = []
        self._chunks = list(type(self).chunks)
        self._error = type(self).error
        type(self).instances.append(self)

    @property
    def chat(self) -> Any:
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> _FakeStream:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return _FakeStream(self._chunks)


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncOpenAI]:
    """Patch ``openai.AsyncOpenAI`` so no test ever touches the network."""
    _FakeAsyncOpenAI.chunks = []
    _FakeAsyncOpenAI.error = None
    _FakeAsyncOpenAI.instances = []
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)
    return _FakeAsyncOpenAI


# ---------------------------------------------------------------------------
# Chunk builders (OpenAI streaming shapes)
# ---------------------------------------------------------------------------

def _delta(content: str | None = None, tool_calls: list | None = None) -> Any:
    return SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=None
    )


def _text_chunk(text: str, finish: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(delta=_delta(content=text), finish_reason=finish)
        ],
        usage=None,
    )


def _tool_chunk(
    index: int,
    id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    finish: str | None = None,
) -> Any:
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=index, id=id, function=fn)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=_delta(tool_calls=[tc]), finish_reason=finish
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int) -> Any:
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _status_error(cls: type, status: int, message: str) -> Exception:
    """Build an openai SDK status error without touching the network."""
    request = httpx.Request("POST", f"{OPENCGO_BASE_URL}/chat/completions")
    response = httpx.Response(
        status, request=request, json={"error": {"message": message}}
    )
    return cls(message, response=response, body=None)


async def _collect(provider: OpenCodeGoProvider, **kwargs: Any) -> list[tuple]:
    messages = kwargs.pop("messages", [{"role": "user", "content": "hi"}])
    return [
        item async for item in provider.stream_chat(messages=messages, **kwargs)
    ]


# ---------------------------------------------------------------------------
# Streaming text / tool calls / usage
# ---------------------------------------------------------------------------

async def test_streaming_text_yields_chunks_then_final_response(fake_openai):
    fake_openai.chunks = [
        _text_chunk("哈囉，"),
        _text_chunk("世界"),
        _text_chunk("", finish="stop"),
        _usage_chunk(prompt=120, completion=45),
    ]
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="sessAAAA")

    items = await _collect(provider)

    assert items[0] == ("哈囉，", None)
    assert items[1] == ("世界", None)
    assert items[-1][0] == ""
    final = items[-1][1]
    assert final.text == "哈囉，世界"
    assert final.tool_uses == []
    assert final.stop_reason == "end_turn"
    assert final.input_tokens == 120
    assert final.output_tokens == 45


async def test_streaming_tool_call_accumulates_into_tool_uses(fake_openai):
    fake_openai.chunks = [
        _tool_chunk(0, id="call_1", name="run_bash", arguments='{"c'),
        _tool_chunk(0, arguments='ode": "ls"}'),
        _tool_chunk(0, arguments="", finish="tool_calls"),
        _usage_chunk(prompt=88, completion=22),
    ]
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="sessBBBB")

    items = await _collect(provider)
    final = items[-1][1]

    assert final.text is None
    assert len(final.tool_uses) == 1
    use = final.tool_uses[0]
    assert use.id == "call_1"
    assert use.name == "run_bash"
    assert use.args == {"code": "ls"}
    assert final.stop_reason == "tool_use"
    assert final.raw_message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_bash", "arguments": '{"code": "ls"}'},
        }
    ]


async def test_chat_returns_collected_response_like_other_providers(fake_openai):
    fake_openai.chunks = [_text_chunk("plain"), _usage_chunk(10, 5)]
    provider = OpenCodeGoProvider(api_key=FAKE_KEY)

    response = await provider.chat([{"role": "user", "content": "hi"}])

    assert response.text == "plain"
    assert response.input_tokens == 10
    assert response.output_tokens == 5


# ---------------------------------------------------------------------------
# Wire details: model id passthrough, headers, base URL
# ---------------------------------------------------------------------------

async def test_model_id_passes_through_after_stripping_loom_prefix(fake_openai):
    fake_openai.chunks = [_text_chunk("ok")]
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, model="opencode/glm-5.3")

    await _collect(provider)

    assert provider.model == "opencode/glm-5.3"
    sent = fake_openai.instances[-1].requests[0]["model"]
    assert sent == "glm-5.3"


def test_any_model_id_is_accepted_without_a_hardcoded_list(fake_openai):
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, model="opencode/longcat-2.0")
    assert provider._api_model() == "longcat-2.0"
    provider2 = OpenCodeGoProvider(
        api_key=FAKE_KEY, model="opencode/deepseek-v4.1-flash"
    )
    assert provider2._api_model() == "deepseek-v4.1-flash"


def test_sends_loom_user_agent_and_stable_session_header(fake_openai):
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="sessC0FFEE")

    client = fake_openai.instances[-1]
    assert client.default_headers["x-opencode-session"] == "sessC0FFEE"
    ua = client.default_headers["User-Agent"]
    assert ua.startswith("loom-agent/")
    # The docs forbid the generic SDK/HTTP-library User-Agent.
    assert "openai" not in ua.lower()
    assert "httpx" not in ua.lower()


def test_session_header_is_generated_and_stable_when_not_given(fake_openai):
    provider = OpenCodeGoProvider(api_key=FAKE_KEY)

    client = fake_openai.instances[-1]
    header = client.default_headers["x-opencode-session"]
    assert header  # non-empty
    assert client.default_headers["x-opencode-session"] == header


def test_default_base_url_points_at_opencode_go_gateway(fake_openai):
    OpenCodeGoProvider(api_key=FAKE_KEY)
    client = fake_openai.instances[-1]
    assert client.base_url == OPENCGO_BASE_URL
    assert client.api_key == FAKE_KEY


# ---------------------------------------------------------------------------
# Errors: 401 / 403 / 429 — readable, never echoing the key
# ---------------------------------------------------------------------------

async def test_401_raises_readable_error_without_leaking_key(fake_openai):
    fake_openai.error = _status_error(
        openai.AuthenticationError, 401, "Invalid API key"
    )
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="s")

    with pytest.raises(RuntimeError) as caught:
        await _collect(provider)

    msg = str(caught.value)
    assert "401" in msg
    assert "OPENCODE_API_KEY" in msg
    assert FAKE_KEY not in msg


async def test_403_raises_readable_entitlement_error(fake_openai):
    fake_openai.error = _status_error(
        openai.PermissionDeniedError, 403, "subscription does not cover this model"
    )
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="s")

    with pytest.raises(RuntimeError) as caught:
        await _collect(provider)

    msg = str(caught.value)
    assert "403" in msg
    assert "OpenCode Go" in msg
    assert FAKE_KEY not in msg


async def test_429_raises_readable_quota_error_mentioning_allowance(fake_openai):
    fake_openai.error = _status_error(
        openai.RateLimitError, 429, "quota exhausted"
    )
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="s")

    with pytest.raises(RuntimeError) as caught:
        await _collect(provider)

    msg = str(caught.value)
    assert "429" in msg
    assert "allowance" in msg
    assert "/v1/usage" in msg
    assert FAKE_KEY not in msg


async def test_other_status_errors_pass_through_unchanged(fake_openai):
    fake_openai.error = _status_error(
        openai.InternalServerError, 500, "boom"
    )
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, session_id="s")

    with pytest.raises(openai.InternalServerError):
        await _collect(provider)


# ---------------------------------------------------------------------------
# Missing key: refuse loudly, never send an empty key
# ---------------------------------------------------------------------------

def test_blank_key_raises_clear_config_error(fake_openai):
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        OpenCodeGoProvider(api_key="")
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        OpenCodeGoProvider(api_key="   ")
    # No client was ever constructed — no request can be sent.
    assert fake_openai.instances == []


def test_none_key_raises_clear_config_error(fake_openai):
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        OpenCodeGoProvider(api_key=None)
    assert fake_openai.instances == []


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------

def test_router_maps_opencode_prefix_to_opencode_go():
    router = LLMRouter()
    provider = OpenCodeGoProvider(api_key=FAKE_KEY, model="opencode/glm-5.3")
    router.register(provider, default=True)

    assert router.get_provider("opencode/glm-5.3") is provider


def test_bare_deepseek_id_still_routes_to_deepseek_provider():
    """Guard: the OpenCode Go prefix must not shadow the deepseek- routing."""
    assert LLMRouter()._provider_name_for_model("deepseek-v4.1-flash") == "deepseek"
    assert (
        LLMRouter()._provider_name_for_model("opencode/deepseek-v4.1-flash")
        == "opencode-go"
    )


def test_build_router_registers_opencode_go_when_key_present(monkeypatch):
    from loom.core import session as session_module

    monkeypatch.setattr(
        session_module, "_load_env",
        lambda: {"OPENCODE_API_KEY": FAKE_KEY, "MINIMAX_API_KEY": "sk-test-minimax"},
    )
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {
        "cognition": {"default_model": "MiniMax-M3.1-Flash"},
        "providers": {"opencode_go": {"default_model": "glm-5.3"}},
    })

    router = session_module.build_router()

    provider = router.get_provider("opencode/glm-5.3")
    assert provider.name == "opencode-go"
    assert provider.model == "opencode/glm-5.3"
    # The current default model must not have been changed.
    assert router.get_provider("MiniMax-M3.1-Flash").name == "minimax"


def test_build_router_serves_opencode_default_and_marks_it_default(monkeypatch):
    from loom.core import session as session_module

    monkeypatch.setattr(
        session_module, "_load_env", lambda: {"OPENCODE_API_KEY": FAKE_KEY}
    )
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {
        "cognition": {"default_model": "opencode/deepseek-v4.1-flash"},
        "providers": {},
    })

    router = session_module.build_router()

    provider = router.get_provider("unrecognized-model-name")
    assert provider.name == "opencode-go"
    assert provider.model == "opencode/deepseek-v4.1-flash"


def test_build_router_passes_session_id_into_session_header(monkeypatch, fake_openai):
    from loom.core import session as session_module

    monkeypatch.setattr(
        session_module, "_load_env", lambda: {"OPENCODE_API_KEY": FAKE_KEY}
    )
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})

    router = session_module.build_router(session_id="sessDK42")
    provider = router.get_provider("opencode/deepseek-v4.1-flash")

    assert provider._async_client.default_headers["x-opencode-session"] == "sessDK42"


def test_build_router_without_key_does_not_register_opencode_go(monkeypatch):
    from loom.core import session as session_module

    monkeypatch.setattr(
        session_module, "_load_env", lambda: {"MINIMAX_API_KEY": "sk-test-minimax"}
    )
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {
        "cognition": {"default_model": "MiniMax-M3.1-Flash"},
        "providers": {},
    })

    router = session_module.build_router()

    with pytest.raises(RuntimeError, match="not registered"):
        router.get_provider("opencode/glm-5.3")


def test_loom_session_wires_its_session_id_into_the_header(monkeypatch, fake_openai, tmp_path):
    """End-to-end: a real LoomSession passes its session id to the provider."""
    from loom.core import session as session_module
    from loom.core.session import LoomSession

    monkeypatch.setattr(
        session_module, "_load_env", lambda: {"OPENCODE_API_KEY": FAKE_KEY}
    )
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})

    session = LoomSession(
        model="opencode/glm-5.3",
        db_path=str(tmp_path / "loom.db"),
        workspace=tmp_path,
    )
    provider = session.router.get_provider("opencode/glm-5.3")

    assert provider._async_client.default_headers["x-opencode-session"] == (
        session.session_id
    )
