"""xAI OAuth credential helpers.

This module is intentionally OAuth-only. It never reads ``XAI_API_KEY`` so an
``xai/...`` OAuth path cannot silently spend API-key quota.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"


@dataclass(frozen=True)
class XAICredential:
    """Bearer credential plus non-secret provenance."""

    token: str
    source: str
    base_url: str
    expires_at: int | None = None


class XAIAuthError(RuntimeError):
    """xAI OAuth setup failed."""


def loom_auth_path() -> Path:
    """Return Loom's auth store path."""

    home = Path(os.environ.get("LOOM_HOME", Path.home() / ".loom"))
    return home / "auth.json"


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_auth_store(path: Path | None = None) -> dict[str, Any]:
    auth_path = path or loom_auth_path()
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_auth_store(store: dict[str, Any], path: Path | None = None) -> Path:
    auth_path = path or loom_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.parent.chmod(0o700)
    tmp_path = auth_path.with_suffix(auth_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(auth_path)
    return auth_path


def save_xai_oauth_state(state: dict[str, Any], path: Path | None = None) -> Path:
    store = _read_auth_store(path)
    store["xai_oauth"] = state
    return _write_auth_store(store, path)


def _validate_xai_oauth_endpoint(url: str, *, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        raise XAIAuthError(f"xAI OIDC discovery returned invalid {field}: {url!r}")
    return url


def xai_oauth_discovery(timeout_seconds: float = 15.0) -> dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=max(5.0, timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise XAIAuthError(f"xAI OIDC discovery failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise XAIAuthError("xAI OIDC discovery response was not a JSON object.")
    authorization_endpoint = str(payload.get("authorization_endpoint", "") or "").strip()
    token_endpoint = str(payload.get("token_endpoint", "") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise XAIAuthError("xAI OIDC discovery response was missing required endpoints.")
    return {
        "authorization_endpoint": _validate_xai_oauth_endpoint(
            authorization_endpoint, field="authorization_endpoint"
        ),
        "token_endpoint": _validate_xai_oauth_endpoint(token_endpoint, field="token_endpoint"),
    }


def _oauth_pkce_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _oauth_pkce_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_xai_authorize_url(
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "loom",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def exchange_xai_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Exchange an authorization code for xAI tokens.

    xAI's OAuth flow accepts the S256 challenge fields on the token request;
    Loom sends them together with the required ``code_verifier`` for parity
    with xAI's browser CLI flow.
    """

    response = httpx.post(
        _validate_xai_oauth_endpoint(token_endpoint, field="token_endpoint"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": XAI_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        timeout=max(20.0, timeout_seconds),
    )
    if response.status_code != 200:
        body = response.text.strip()
        raise XAIAuthError(
            f"xAI token exchange failed (HTTP {response.status_code})."
            + (f" Response: {body}" if body else "")
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
        raise XAIAuthError("xAI token exchange did not return access_token and refresh_token.")
    return payload


def _make_callback_handler(expected_path: str) -> tuple[type[BaseHTTPRequestHandler], dict[str, Any]]:
    result: dict[str, Any] = {"code": None, "state": None, "error": None, "error_description": None}
    result_lock = threading.Lock()

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            incoming = {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
                "error": params.get("error", [None])[0],
                "error_description": params.get("error_description", [None])[0],
            }
            with result_lock:
                if not (result["code"] or result["error"]):
                    result.update(incoming)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            title = "xAI authorization failed." if incoming["error"] else "xAI authorization received."
            self.wfile.write(f"<html><body><h1>{title}</h1>You can close this tab.</body></html>".encode())

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _CallbackHandler, result


def _start_callback_server() -> tuple[HTTPServer, threading.Thread, dict[str, Any], str]:
    handler_cls, result = _make_callback_handler(XAI_OAUTH_REDIRECT_PATH)

    class _ReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        server = _ReuseHTTPServer((XAI_OAUTH_REDIRECT_HOST, XAI_OAUTH_REDIRECT_PORT), handler_cls)
    except OSError as exc:
        raise XAIAuthError(
            f"xAI OAuth callback port {XAI_OAUTH_REDIRECT_PORT} is unavailable. "
            "Close the process using it, then run `loom auth xai` again."
        ) from exc
    redirect_uri = f"http://{XAI_OAUTH_REDIRECT_HOST}:{server.server_address[1]}{XAI_OAUTH_REDIRECT_PATH}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return server, thread, result, redirect_uri


def _wait_for_callback(
    server: HTTPServer,
    thread: threading.Thread,
    result: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(30.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
    raise XAIAuthError("xAI authorization timed out waiting for the local callback.")


def run_xai_oauth_login(
    *,
    open_browser: bool = True,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run browser-based xAI OAuth login and return unsaved auth state.

    The caller is responsible for persisting the returned state with
    ``save_xai_oauth_state()``.
    """

    discovery = xai_oauth_discovery(timeout_seconds)
    server, thread, callback_result, redirect_uri = _start_callback_server()
    code_verifier = _oauth_pkce_code_verifier()
    code_challenge = _oauth_pkce_code_challenge(code_verifier)
    state = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    authorize_url = build_xai_authorize_url(
        authorization_endpoint=discovery["authorization_endpoint"],
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        nonce=nonce,
    )
    print("Open this URL to authorize Loom with xAI:")
    print(authorize_url)
    print()
    print(f"Waiting for callback on {redirect_uri}")
    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass
    callback = _wait_for_callback(
        server,
        thread,
        callback_result,
        timeout_seconds=timeout_seconds,
    )
    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise XAIAuthError(f"xAI authorization failed: {detail}")
    if callback.get("state") != state:
        raise XAIAuthError("xAI authorization failed: state mismatch.")
    code = str(callback.get("code") or "").strip()
    if not code:
        raise XAIAuthError("xAI authorization failed: missing authorization code.")
    payload = exchange_xai_code_for_tokens(
        token_endpoint=discovery["token_endpoint"],
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        timeout_seconds=timeout_seconds,
    )
    return {
        "tokens": {
            "access_token": str(payload["access_token"]).strip(),
            "refresh_token": str(payload["refresh_token"]).strip(),
            "id_token": str(payload.get("id_token", "") or "").strip(),
            "expires_in": payload.get("expires_in"),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        },
        "discovery": discovery,
        "redirect_uri": redirect_uri,
        "base_url": DEFAULT_XAI_BASE_URL,
        "last_refresh": _utc_now(),
    }


def load_xai_oauth_state(path: Path | None = None) -> dict[str, Any] | None:
    state = _read_auth_store(path).get("xai_oauth")
    return state if isinstance(state, dict) else None


def validate_xai_base_url(value: str, *, fallback: str = DEFAULT_XAI_BASE_URL) -> str:
    """Return an HTTPS xAI-origin base URL, ignoring unsafe overrides."""

    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        logger.warning("Refusing xAI base_url override %r; using %s", candidate, fallback)
        return fallback
    return candidate


def refresh_xai_oauth_state(
    state: dict[str, Any],
    *,
    path: Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise XAIAuthError("xAI OAuth state is missing refresh_token. Run `loom auth xai` again.")
    discovery = state.get("discovery") if isinstance(state.get("discovery"), dict) else {}
    token_endpoint = str(discovery.get("token_endpoint", "") or "").strip()
    if not token_endpoint:
        discovery = xai_oauth_discovery(timeout_seconds)
        token_endpoint = discovery["token_endpoint"]
    response = httpx.post(
        _validate_xai_oauth_endpoint(token_endpoint, field="token_endpoint"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        timeout=max(20.0, timeout_seconds),
    )
    if response.status_code != 200:
        body = response.text.strip()
        raise XAIAuthError(
            f"xAI token refresh failed (HTTP {response.status_code})."
            + (f" Response: {body}" if body else "")
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise XAIAuthError("xAI token refresh did not return access_token.")
    updated = dict(state)
    updated["tokens"] = dict(tokens)
    updated["tokens"]["access_token"] = str(payload["access_token"]).strip()
    updated["tokens"]["refresh_token"] = str(payload.get("refresh_token") or refresh_token).strip()
    if payload.get("id_token"):
        updated["tokens"]["id_token"] = str(payload["id_token"]).strip()
    if payload.get("expires_in") is not None:
        updated["tokens"]["expires_in"] = payload.get("expires_in")
    updated["tokens"]["token_type"] = str(payload.get("token_type") or "Bearer").strip() or "Bearer"
    updated["discovery"] = discovery
    updated["last_refresh"] = _utc_now()
    save_xai_oauth_state(updated, path)
    return updated


def load_xai_oauth_credential(
    path: Path | None = None,
    *,
    refresh: bool = True,
) -> XAICredential | None:
    """Load an unexpired xAI OAuth bearer if one is available."""

    state = load_xai_oauth_state(path)
    if not state:
        return None
    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
    access_token = str(tokens.get("access_token", "") or "").strip()
    if not access_token:
        return None
    expires_at = _jwt_exp(access_token)
    if refresh and expires_at is not None and expires_at <= time.time():
        state = refresh_xai_oauth_state(state, path=path)
        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        access_token = str(tokens.get("access_token", "") or "").strip()
        expires_at = _jwt_exp(access_token)
    if expires_at is not None and expires_at <= time.time():
        return None
    base_url = validate_xai_base_url(str(state.get("base_url", "") or ""))
    return XAICredential(
        token=access_token,
        source="xai_oauth",
        base_url=base_url,
        expires_at=expires_at,
    )
