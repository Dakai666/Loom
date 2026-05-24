import base64
import json
import logging
import time

import pytest

from loom.core.cognition.xai_auth import (
    DEFAULT_XAI_BASE_URL,
    XAIAuthError,
    load_xai_oauth_credential,
    run_xai_oauth_login,
    save_xai_oauth_state,
    validate_xai_base_url,
)


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


def test_load_xai_oauth_credential_reads_saved_oauth_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
        },
        "base_url": DEFAULT_XAI_BASE_URL,
    })

    credential = load_xai_oauth_credential(refresh=False)

    assert credential is not None
    assert credential.source == "xai_oauth"
    assert credential.token == token
    assert credential.base_url == DEFAULT_XAI_BASE_URL


def test_load_xai_oauth_credential_ignores_xai_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    monkeypatch.setenv("XAI_API_KEY", "xai-api-key-that-must-not-be-used")

    assert load_xai_oauth_credential(refresh=False) is None


def test_load_xai_oauth_credential_refreshes_expired_token(tmp_path, monkeypatch):
    from loom.core.cognition import xai_auth

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    expired = _jwt(int(time.time()) - 10)
    refreshed = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": expired,
            "refresh_token": "refresh-secret",
        },
        "discovery": {"token_endpoint": "https://auth.x.ai/oauth2/token"},
        "base_url": DEFAULT_XAI_BASE_URL,
    })
    posts = []

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": refreshed,
                "refresh_token": "rotated-refresh-secret",
                "token_type": "Bearer",
            }

    def fake_post(url, headers=None, data=None, timeout=None):
        posts.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(xai_auth.httpx, "post", fake_post)

    credential = load_xai_oauth_credential()

    assert credential is not None
    assert credential.token == refreshed
    assert posts[0]["url"] == "https://auth.x.ai/oauth2/token"
    assert posts[0]["data"]["grant_type"] == "refresh_token"
    assert posts[0]["data"]["refresh_token"] == "refresh-secret"
    auth_text = (tmp_path / "loom-home" / "auth.json").read_text(encoding="utf-8")
    assert "rotated-refresh-secret" in auth_text


def test_load_xai_oauth_credential_rejects_non_xai_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    token = _jwt(int(time.time()) + 3600)
    save_xai_oauth_state({
        "tokens": {
            "access_token": token,
            "refresh_token": "refresh-secret",
        },
        "base_url": "https://attacker.example/v1",
    })

    credential = load_xai_oauth_credential(refresh=False)

    assert credential is not None
    assert credential.base_url == DEFAULT_XAI_BASE_URL


def test_validate_xai_base_url_warns_when_rejecting_unsafe_override(caplog):
    caplog.set_level(logging.WARNING, logger="loom.core.cognition.xai_auth")

    assert validate_xai_base_url("https://attacker.example/v1/responses") == DEFAULT_XAI_BASE_URL

    assert "Refusing xAI base_url override" in caplog.text
    assert "attacker.example" in caplog.text


def test_start_callback_server_reports_port_binding_failure(monkeypatch):
    from loom.core.cognition import xai_auth

    class _BindFailureServer:
        def __init__(self, *args, **kwargs):
            raise OSError("address already in use")

    monkeypatch.setattr(xai_auth, "ThreadingHTTPServer", _BindFailureServer)

    with pytest.raises(XAIAuthError, match="port 56121"):
        xai_auth._start_callback_server()


def test_run_xai_oauth_login_uses_pkce_callback_and_token_exchange(monkeypatch):
    from loom.core.cognition import xai_auth

    monkeypatch.setattr(
        xai_auth,
        "xai_oauth_discovery",
        lambda timeout_seconds: {
            "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
    )
    monkeypatch.setattr(
        xai_auth,
        "_start_callback_server",
        lambda: (object(), object(), {}, "http://127.0.0.1:56121/callback"),
    )
    captured = {}

    def fake_build_authorize_url(**kwargs):
        captured.update(kwargs)
        return "https://auth.x.ai/authorize"

    def fake_wait_for_callback(server, thread, result, *, timeout_seconds):
        return {"code": "auth-code", "state": captured["state"]}

    def fake_exchange(**kwargs):
        captured["exchange"] = kwargs
        return {
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "token_type": "Bearer",
        }

    monkeypatch.setattr(xai_auth, "build_xai_authorize_url", fake_build_authorize_url)
    monkeypatch.setattr(xai_auth, "_wait_for_callback", fake_wait_for_callback)
    monkeypatch.setattr(xai_auth, "exchange_xai_code_for_tokens", fake_exchange)

    state = run_xai_oauth_login(open_browser=False, timeout_seconds=12)

    assert captured["authorization_endpoint"] == "https://auth.x.ai/oauth2/auth"
    assert captured["redirect_uri"] == "http://127.0.0.1:56121/callback"
    assert captured["code_challenge"]
    assert captured["nonce"]
    assert captured["exchange"]["code"] == "auth-code"
    assert captured["exchange"]["code_verifier"]
    assert captured["exchange"]["code_challenge"] == captured["code_challenge"]
    assert state["tokens"]["access_token"] == "oauth-access-token"
    assert state["tokens"]["refresh_token"] == "oauth-refresh-token"
    assert state["base_url"] == DEFAULT_XAI_BASE_URL
