import base64
import hashlib
import io
import json
import multiprocessing
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from loom.core.cognition import chatgpt_auth as mod
from loom.core.cognition.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, PLAN_SCOPE


def save_account(auth, *, expired=False, key="oaiapp_test", token="old-access"):
    with auth._locked():
        store = auth._read()
        store["host_id"] = "urn:uuid:test-host"
        store["active_account"] = key
        store["accounts"][key] = {
            "client_id": key, "subject": "user-1", "label": key, "access_token": token,
            "refresh_token": "refresh-1", "expires_at": time.time() + (-10 if expired else 3600),
            "scopes": [PLAN_SCOPE],
        }
        auth._write(store)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def install_login(monkeypatch, signing_key, *, scopes=mod.SCOPES, wrong_nonce=False,
                  wrong_signature=False, subject="user-1", client_id="oaiapp_test", code_failure=False,
                  callback_state=None, claims_override=None):
    context = {}
    class Server:
        server_port = 54321
        timeout = 1
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler = handler
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def handle_request(self):
            handler = object.__new__(self.handler)
            handler.path = "/auth/callback?" + mod.urlencode({
                "state": callback_state or context["params"]["state"][0],
                "code": "one-use-code", "client_id": client_id})
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: None
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.do_GET()
    monkeypatch.setattr(mod, "HTTPServer", Server)
    monkeypatch.setattr(mod.webbrowser, "open", lambda url: context.update(opened=url))
    def on_url(url):
        context["params"] = parse_qs(urlsplit(url).query)
    def transport(request):
        if str(request.url) == mod.JWKS_URL:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
            jwk["kid"] = "test"
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == mod.TOKEN_URL
        params = parse_qs(request.content.decode())
        context["exchange"] = params
        assert params["client_id"] == [client_id]
        assert params["resource"] == [mod.RESOURCE]
        verifier = params["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert challenge == context["params"]["code_challenge"][0]
        assert params["redirect_uri"] == context["params"]["redirect_uri"]
        if code_failure:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048) if wrong_signature else signing_key
        claims = {"sub": subject, "aud": client_id, "iss": mod.ISSUER, "iat": now,
                            "exp": now+3600, "nonce": "wrong" if wrong_nonce else context["params"]["nonce"][0],
                            "email": "test@example.com", **(claims_override or {})}
        token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test"})
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                                        "scope": scopes, "expires_in": 3600, "id_token": token})
    return context, on_url, httpx.MockTransport(transport)


def test_real_signed_oidc_pkce_login_and_private_atomic_store(tmp_path, monkeypatch, signing_key):
    context, on_url, transport = install_login(monkeypatch, signing_key)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    result = auth.login(on_url=on_url, open_browser=False)
    assert result["plan_usage"] is True
    assert auth.access_token() == "new-access"
    assert context["params"]["client_id"] == ["dynamic_agent_client"]
    assert context["params"]["agent_name_hint"] == ["Loom"]
    assert "one-use-code" not in auth.path.read_text()
    assert auth.path.stat().st_mode & 0o777 == 0o600
    assert "access_token" not in json.dumps(auth.accounts())


@pytest.mark.parametrize("option", ["wrong_nonce", "wrong_signature"])
def test_invalid_identity_never_replaces_active_account(tmp_path, monkeypatch, signing_key, option):
    _, on_url, transport = install_login(monkeypatch, signing_key, **{option: True})
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    save_account(auth, key="oaiapp_old")
    with pytest.raises(ChatGPTAuthError, match="verification failed"):
        auth.login(new_account=True, on_url=on_url, open_browser=False)
    assert auth.active_account() == "oaiapp_old"
    assert auth.access_token() == "old-access"


def test_declined_plan_permission_saves_identity_but_blocks_inference(tmp_path, monkeypatch, signing_key):
    _, on_url, transport = install_login(monkeypatch, signing_key, scopes="openid profile email")
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    assert auth.login(on_url=on_url, open_browser=False)["plan_usage"] is False
    with pytest.raises(ChatGPTAuthError, match="not authorized"):
        auth.access_token()


@pytest.mark.parametrize("claims", [{"iss": "https://evil.example"}, {"aud": "different-client"}, {"exp": 1}])
def test_oidc_checks_issuer_audience_and_expiry(tmp_path, monkeypatch, signing_key, claims):
    _, on_url, transport = install_login(monkeypatch, signing_key, claims_override=claims)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    with pytest.raises(ChatGPTAuthError, match="verification failed"):
        auth.login(on_url=on_url, open_browser=False)
    assert not auth.accounts()[0]["signed_in"]


def test_wrong_callback_state_never_exchanges_code(tmp_path, monkeypatch, signing_key):
    context, on_url, transport = install_login(monkeypatch, signing_key, callback_state="wrong")
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    with pytest.raises(ChatGPTAuthError, match="timed out"):
        auth.login(on_url=on_url, open_browser=False, timeout=.01)
    assert "exchange" not in context
    assert auth.accounts() == []


def test_returning_account_cannot_change_client_or_identity(tmp_path, monkeypatch, signing_key):
    _, on_url, transport = install_login(monkeypatch, signing_key, client_id="oaiapp_other")
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    save_account(auth)
    before = auth.path.read_bytes()
    with pytest.raises(ChatGPTAuthError, match="different client ID"):
        auth.login(on_url=on_url, open_browser=False)
    assert auth.path.read_bytes() == before
    _, on_url, transport = install_login(monkeypatch, signing_key, subject="different-user")
    auth.transport = transport
    with pytest.raises(ChatGPTAuthError, match="identity differs"):
        auth.login(on_url=on_url, open_browser=False)
    assert auth.path.read_bytes() == before


def test_reauthorization_reuses_client_host_and_hides_token_hint(tmp_path, monkeypatch, signing_key):
    context, on_url, transport = install_login(monkeypatch, signing_key)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    auth.login(on_url=on_url, open_browser=False)
    host = context["params"]["ext_agent_host_id"]
    auth.login(on_url=on_url, open_browser=True)
    assert context["params"]["client_id"] == ["oaiapp_test"]
    assert context["params"]["ext_agent_host_id"] == host
    assert "id_token_hint" not in context["params"]
    assert "id_token_hint" in parse_qs(urlsplit(context["opened"]).query)
    assert "agent_name_hint" not in context["params"]


def test_issued_client_survives_interrupted_exchange(tmp_path, monkeypatch, signing_key):
    context, on_url, transport = install_login(monkeypatch, signing_key, code_failure=True)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=transport)
    for _ in range(2):
        with pytest.raises(ChatGPTAuthError, match="invalid_grant"):
            auth.login(on_url=on_url, open_browser=False)
    assert context["params"]["client_id"] == ["oaiapp_test"]
    assert not auth.accounts()[0]["signed_in"]


def test_refresh_rotation_and_resource_scope(tmp_path):
    requests = []
    def transport(request):
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rotated",
                                        "scope": mod.SCOPES, "expires_in": 3600})
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth, expired=True)
    assert auth.access_token() == "fresh"
    assert auth.access_token() == "fresh"
    assert len(requests) == 1
    assert requests[0]["refresh_token"] == ["refresh-1"]
    assert requests[0]["resource"] == [mod.RESOURCE]
    assert "scope" not in requests[0]
    assert auth._read()["accounts"]["oaiapp_test"]["refresh_token"] == "rotated"


@pytest.mark.parametrize("error,status,cleared", [("invalid_grant", 400, True), ("temporarily_unavailable", 503, False),
                                                 ("invalid_client", 401, False)])
def test_refresh_error_preserves_registration_and_never_uses_api_key(tmp_path, monkeypatch, error, status, cleared):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-use")
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"error": error})))
    save_account(auth, expired=True)
    with pytest.raises(ChatGPTAuthError, match=error):
        auth.access_token()
    record = auth._read()["accounts"]["oaiapp_test"]
    assert record["client_id"] == "oaiapp_test"
    assert ("refresh_token" not in record) == cleared
    assert "must-not-use" not in auth.path.read_text()


def test_network_refresh_failure_does_not_erase_credentials(tmp_path):
    def transport(request): raise httpx.ConnectError("offline", request=request)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth, expired=True)
    before = auth.path.read_bytes()
    with pytest.raises(ChatGPTAuthError, match="network"):
        auth.access_token()
    assert auth.path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="fork-based real cross-process refresh test")
def test_two_processes_redeem_rotating_refresh_token_only_once(tmp_path):
    path, counter = tmp_path / "auth.json", tmp_path / "calls"
    save_account(ChatGPTAuth(path), expired=True)
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    def worker():
        def transport(request):
            with counter.open("a") as out: out.write("refresh\n")
            time.sleep(.1)
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rotated",
                                            "scope": mod.SCOPES, "expires_in": 3600})
        auth = ChatGPTAuth(path, transport=httpx.MockTransport(transport))
        barrier.wait(timeout=5)
        results.put(auth.access_token())
    children = [ctx.Process(target=worker) for _ in range(2)]
    for child in children: child.start()
    for child in children:
        child.join(8)
        if child.is_alive(): child.terminate()
        assert child.exitcode == 0
    assert [results.get(timeout=2) for _ in children] == ["fresh", "fresh"]
    assert counter.read_text() == "refresh\n"


def test_discovery_is_account_specific_and_visibility_filtered(tmp_path):
    seen = []
    def transport(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"models": [
            {"slug": "a", "visibility": "list", "context_window": 100000},
            {"slug": "hidden", "visibility": "hide"}]})
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth, key="account-a", token="token-a")
    save_account(auth, key="account-b", token="token-b")
    assert auth.catalog("account-a", refresh=True)[0]["slug"] == "a"
    assert seen == ["Bearer token-a"]
    assert auth.catalog("account-b") == []
    assert auth.active_account() == "account-b"


def test_logout_revokes_but_keeps_host_and_client_mapping(tmp_path):
    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json={"revocation_endpoint": mod.ISSUER + "/oauth/revoke"})
        assert parse_qs(request.content.decode())["client_id"] == ["oaiapp_test"]
        return httpx.Response(200)
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth)
    assert auth.logout()
    assert auth._read()["host_id"] == "urn:uuid:test-host"
    assert auth.active_account() == "oaiapp_test"
    assert not auth.accounts()[0]["signed_in"]


def test_logout_never_sends_refresh_token_to_untrusted_discovery_url(tmp_path):
    seen = []
    def transport(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"revocation_endpoint": "https://evil.example/revoke"})
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth)
    assert not auth.logout()
    assert len(seen) == 1
    assert not auth.accounts()[0]["signed_in"]


@pytest.mark.parametrize("optional", [
    {"scope": mod.SCOPES}, {"refresh_token": "rotated"}, {},
])
def test_refresh_preserves_only_omitted_optional_fields(tmp_path, optional):
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600, **optional})))
    save_account(auth, expired=True)
    assert auth.access_token() == "fresh"
    assert auth.access_token() == "fresh"
    record = auth._read()["accounts"]["oaiapp_test"]
    assert record["refresh_token"] == optional.get("refresh_token", "refresh-1")
    assert record["scopes"] == optional.get("scope", PLAN_SCOPE).split()


@pytest.mark.parametrize("scope", ["", "openid"])
def test_refresh_explicit_scope_removal_is_persisted_and_blocks_inference(tmp_path, scope):
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600, "scope": scope})
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(transport))
    save_account(auth, expired=True)
    with pytest.raises(ChatGPTAuthError, match="no longer granted"):
        auth.access_token()
    with pytest.raises(ChatGPTAuthError, match="not authorized"):
        auth.access_token()
    assert len(calls) == 1
    record = auth._read()["accounts"]["oaiapp_test"]
    assert record["scopes"] == scope.split()
    assert record["refresh_token"] == "refresh-1"


@pytest.mark.parametrize("body", [
    b"not-json", b"[]", b'{"access_token":"fresh","expires_in":3600,"refresh_token":null}',
    b'{"access_token":"fresh","expires_in":3600,"refresh_token":""}',
    b'{"access_token":"fresh","expires_in":3600,"scope":null}',
])
def test_invalid_successful_refresh_is_typed_and_preserves_credentials(tmp_path, body):
    auth = ChatGPTAuth(tmp_path / "auth.json", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    save_account(auth, expired=True)
    before = auth.path.read_bytes()
    with pytest.raises(ChatGPTAuthError, match="preserved"):
        auth.access_token()
    assert auth.path.read_bytes() == before


def test_initial_exchange_still_requires_refresh_token(tmp_path):
    auth = ChatGPTAuth(tmp_path / "auth.json")
    with pytest.raises(ChatGPTAuthError, match="incomplete token set"):
        auth._token_fields({"access_token": "fresh", "expires_in": 3600, "scope": PLAN_SCOPE})


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_process", [False, True])
async def test_cached_context_does_not_block_event_loop_during_refresh(tmp_path, cross_process):
    import asyncio
    import queue
    import threading
    from loom.core.cognition.chatgpt_provider import ChatGPTResponsesProvider

    if cross_process and os.name == "nt":
        pytest.skip("fork-based process contention test")
    path = tmp_path / "auth.json"
    auth = ChatGPTAuth(path)
    save_account(auth, expired=True)
    with auth._locked():
        store = auth._read()
        store["accounts"]["oaiapp_test"]["models"] = [{"slug": "test", "context_window": 128000}]
        auth._write(store)
    ctx = multiprocessing.get_context("fork") if cross_process else threading
    entered, release = ctx.Event(), ctx.Event()
    results = ctx.Queue() if cross_process else queue.Queue()
    def worker():
        def transport(request):
            entered.set()  # access_token owns both transaction locks here.
            if not release.wait(5):
                raise RuntimeError("refresh gate timed out")
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        try:
            results.put(ChatGPTAuth(path, transport=httpx.MockTransport(transport)).access_token())
        except Exception as exc:
            results.put(type(exc).__name__)
    child = ctx.Process(target=worker) if cross_process else threading.Thread(target=worker)
    child.start()
    watchdog = threading.Timer(2, release.set)  # Old locking code fails without hanging pytest.
    watchdog.start()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        async def heartbeat():
            await asyncio.sleep(.01)
            return not release.is_set()
        beat = asyncio.create_task(heartbeat())
        provider = ChatGPTResponsesProvider(auth=auth)
        for _ in range(10):
            assert provider.native_context_window("chatgpt/test") == 128000
        assert await beat, "catalog lookup waited for the OAuth HTTP request"
        assert not release.is_set()
    finally:
        release.set()
        watchdog.cancel()
        await asyncio.to_thread(child.join, 3)
    assert not child.is_alive()
    assert results.get(timeout=1) == "fresh"
