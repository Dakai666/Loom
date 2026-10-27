"""Official Sign in with ChatGPT for Loom (public OSS client, PKCE + OIDC).

Credentials belong to Loom, never to Codex or OPENAI_API_KEY. The file lock
covers read/refresh/write so rotating refresh tokens are safe across processes.
See https://developers.openai.com/siwc/token-sharing-open-source/sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
_THREAD_LOCK = threading.RLock()
_TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalid", "refresh_token_invalidated", "refresh_token_reused",
}


class ChatGPTAuthError(RuntimeError):
    """Sanitized OAuth error; never includes a credential or authorization URL."""


def default_auth_path() -> Path:
    return Path(os.environ.get("LOOM_HOME", Path.home() / ".loom")) / "chatgpt-auth.json"


def _safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        code = body.get("error", "oauth_error")
        if isinstance(code, dict):
            code = code.get("code", "oauth_error")
    except (ValueError, AttributeError):
        code = "oauth_error"
    return str(code) if re.fullmatch(r"[a-zA-Z0-9_]{1,100}", str(code)) else "oauth_error"


class ChatGPTAuth:
    def __init__(self, path: Path | None = None, *, transport: httpx.BaseTransport | None = None):
        self.path = path or default_auth_path()
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=30.0, follow_redirects=False, transport=self.transport)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _THREAD_LOCK:
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "accounts": {}}
        except (OSError, ValueError) as exc:
            raise ChatGPTAuthError("Cannot read Loom ChatGPT credentials; file was left unchanged.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise ChatGPTAuthError("Invalid Loom ChatGPT credential store; file was left unchanged.")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".chatgpt-", dir=self.path.parent)
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def accounts(self) -> list[dict[str, Any]]:
        """Return only non-secret account status suitable for CLI output."""
        with self._locked():
            store = self._read()
            return [{
                "account": key, "label": value.get("label", key),
                "email": value.get("email", ""), "active": key == store.get("active_account"),
                "plan_usage": PLAN_SCOPE in value.get("scopes", []),
                "signed_in": bool(value.get("refresh_token")),
            } for key, value in store["accounts"].items()]

    def _account(self, store: dict[str, Any], account: str | None = None) -> tuple[str, dict[str, Any]]:
        key = account or store.get("active_account")
        if key not in store["accounts"]:
            matches = [k for k, v in store["accounts"].items() if v.get("label") == key]
            if len(matches) != 1:
                raise ChatGPTAuthError("Select a saved account or run `loom auth chatgpt login`.")
            key = matches[0]
        return key, store["accounts"][key]

    def select(self, account: str) -> None:
        with self._locked():
            store = self._read()
            key, record = self._account(store, account)
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("This account needs to sign in again.")
            store["active_account"] = key
            self._write(store)

    def active_account(self) -> str:
        with self._locked():
            return self._account(self._read())[0]

    def catalog(self, account: str | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
        with self._locked():
            key, record = self._account(self._read(), account)
            if not refresh:
                return list(record.get("models", []))
        token = self.access_token(key)
        try:
            with self._client() as client:
                response = client.get(f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"})
                if response.status_code != 200:
                    raise ChatGPTAuthError(f"ChatGPT model discovery failed (HTTP {response.status_code}).")
                models = response.json().get("models")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ChatGPTAuthError("ChatGPT model discovery failed; cached catalog was preserved.") from exc
        if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
            raise ChatGPTAuthError("ChatGPT returned an unexpected model catalog.")
        visible = [m for m in models if m.get("visibility") == "list" and isinstance(m.get("slug"), str)]
        with self._locked():
            store = self._read()
            self._account(store, key)[1]["models"] = visible
            self._write(store)
        return visible

    def _validate_identity(self, token: str, client_id: str, nonce: str) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(JWKS_URL)
                response.raise_for_status()
                keys = jwt.PyJWKSet.from_dict(response.json()).keys
            header = jwt.get_unverified_header(token)
            key = next(k for k in keys if k.key_id == header.get("kid"))
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
            if not claims["sub"] or not hmac.compare_digest(str(claims["nonce"]), nonce):
                raise ValueError("invalid identity")
            return claims
        except (jwt.PyJWTError, ValueError, StopIteration, httpx.HTTPError) as exc:
            raise ChatGPTAuthError("OpenAI identity verification failed; credentials were not activated.") from exc

    def _token_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        if not all(isinstance(data.get(k), str) and data[k] for k in ("access_token", "refresh_token")):
            raise ChatGPTAuthError("OpenAI returned an incomplete token set; sign in again.")
        try:
            ttl = float(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatGPTAuthError("OpenAI did not return a valid token expiry.") from exc
        if not 0 < ttl < float("inf"):
            raise ChatGPTAuthError("OpenAI returned an invalid token expiry.")
        return {"access_token": data["access_token"], "refresh_token": data["refresh_token"],
                "expires_at": time.time() + ttl, "scopes": str(data.get("scope", "")).split()}

    def login(self, *, account: str | None = None, new_account: bool = False,
              label: str | None = None, open_browser: bool = True, timeout: float = 180,
              on_url: Callable[[str], None] | None = None) -> dict[str, Any]:
        if timeout <= 0:
            raise ChatGPTAuthError("Login timeout must be positive.")
        with self._locked():
            store = self._read()
            store.setdefault("host_id", f"urn:uuid:{uuid.uuid4()}")
            if not new_account and (account or store.get("active_account")):
                _, prior = self._account(store, account)
                prior = dict(prior)
            else:
                # Preserve a client issued before an interrupted first exchange.
                pending = [v for v in store["accounts"].values() if v.get("pending")]
                prior = dict(pending[-1]) if pending and not new_account else {}
            host_id = store["host_id"]
            self._write(store)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        callback: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # OAuth callback contains secrets.

            def do_GET(self):
                parsed = urlsplit(self.path)
                values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
                valid = (parsed.path == "/auth/callback"
                         and all(len(v) == 1 for v in values.values())
                         and hmac.compare_digest(values.get("state", [""])[0], state))
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Return to Loom to finish signing in." if valid else b"Invalid OAuth callback.")
                if valid:
                    callback.update({k: v[0] for k, v in values.items()})

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = min(1.0, timeout)
            redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
            params = {"client_id": prior.get("client_id", "dynamic_agent_client"),
                      "ext_agent_host_id": host_id, "redirect_uri": redirect_uri,
                      "response_type": "code", "scope": SCOPES, "resource": RESOURCE,
                      "state": state, "nonce": nonce, "code_challenge_method": "S256",
                      "code_challenge": challenge}
            if prior.get("client_id"):
                if prior.get("id_token"):
                    params["id_token_hint"] = prior["id_token"]
                if PLAN_SCOPE not in prior.get("scopes", []):
                    params["prompt"] = "consent"
            else:
                params["agent_name_hint"] = "Loom"
            url = AUTHORIZE_URL + "?" + urlencode(params)
            # Do not print URLs containing id_token_hint, including in --no-browser mode.
            if on_url:
                printable = dict(params)
                printable.pop("id_token_hint", None)
                on_url(AUTHORIZE_URL + "?" + urlencode(printable))
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while not callback and time.monotonic() < deadline:
                server.handle_request()
        if not callback:
            raise ChatGPTAuthError("ChatGPT login timed out; no active credentials were replaced.")
        if callback.get("error"):
            raise ChatGPTAuthError("ChatGPT authorization was declined or failed.")
        client_id = callback.get("client_id") or prior.get("client_id")
        if not client_id or client_id == "dynamic_agent_client" or not callback.get("code"):
            raise ChatGPTAuthError("ChatGPT registration did not return a code and issued client ID.")
        if prior.get("client_id") and client_id != prior["client_id"]:
            raise ChatGPTAuthError("ChatGPT returned a different client ID; account was left unchanged.")
        with self._locked():
            store = self._read()
            store["accounts"].setdefault(client_id, {"client_id": client_id, "pending": True})
            self._write(store)
        try:
            with self._client() as client:
                response = client.post(TOKEN_URL, data={
                    "grant_type": "authorization_code", "client_id": client_id,
                    "code": callback["code"], "code_verifier": verifier,
                    "redirect_uri": redirect_uri, "resource": RESOURCE,
                })
                if response.status_code != 200:
                    raise ChatGPTAuthError(f"ChatGPT code exchange failed ({_safe_error(response)}); sign in again.")
                data = response.json()
            identity = self._validate_identity(data.get("id_token", ""), client_id, nonce)
            if prior.get("subject") and identity["sub"] != prior["subject"]:
                raise ChatGPTAuthError("Signed-in identity differs from the selected account.")
            fields = self._token_fields(data)
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError("ChatGPT login network error; retry signing in.") from exc
        record = {"client_id": client_id, "subject": identity["sub"], "issuer": ISSUER,
                  "email": identity.get("email", ""), "label": label or prior.get("label") or client_id,
                  "id_token": data["id_token"], **fields}
        with self._locked():
            store = self._read()
            store["accounts"][client_id] = record
            store["active_account"] = client_id
            self._write(store)
        return {"account": client_id, "email": record["email"], "plan_usage": PLAN_SCOPE in fields["scopes"]}

    def access_token(self, account: str | None = None) -> str:
        with self._locked():
            store = self._read()
            key, record = self._account(store, account)
            if PLAN_SCOPE not in record.get("scopes", []):
                raise ChatGPTAuthError("ChatGPT plan usage is not authorized. Run `loom auth chatgpt login`.")
            if record.get("access_token") and record.get("expires_at", 0) > time.time() + 60:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT needs to sign in again: `loom auth chatgpt login`.")
            try:
                with self._client() as client:
                    response = client.post(TOKEN_URL, data={
                        "grant_type": "refresh_token", "client_id": key,
                        "refresh_token": record["refresh_token"], "resource": RESOURCE,
                    })
            except httpx.HTTPError as exc:
                raise ChatGPTAuthError("ChatGPT token refresh network error; credentials were preserved.") from exc
            if response.status_code != 200:
                code = _safe_error(response)
                if code in _TERMINAL_REFRESH_ERRORS:
                    for field in ("access_token", "refresh_token", "id_token"):
                        record.pop(field, None)
                    self._write(store)
                raise ChatGPTAuthError(f"ChatGPT token refresh failed ({code}); retry or sign in again.")
            fields = self._token_fields(response.json())
            record.update(fields)
            self._write(store)
            if PLAN_SCOPE not in fields["scopes"]:
                raise ChatGPTAuthError("ChatGPT plan usage permission is no longer granted; sign in again.")
            return record["access_token"]

    def logout(self, account: str | None = None) -> bool:
        """Revoke renewable session, clear local tokens, keep registration and host."""
        with self._locked():
            store = self._read()
            key, record = self._account(store, account)
            confirmed = not bool(record.get("refresh_token"))
            if record.get("refresh_token"):
                try:
                    with self._client() as client:
                        discovery = client.get(f"{ISSUER}/.well-known/openid-configuration")
                        discovery.raise_for_status()
                        endpoint = discovery.json()["revocation_endpoint"]
                        parsed = urlsplit(endpoint)
                        if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
                            raise ValueError("unexpected revocation endpoint")
                        response = client.post(endpoint, data={"token": record["refresh_token"],
                                               "token_type_hint": "refresh_token", "client_id": key})
                        confirmed = response.status_code == 200
                except (httpx.HTTPError, ValueError, KeyError):
                    confirmed = False
            for field in ("access_token", "refresh_token", "id_token"):
                record.pop(field, None)
            self._write(store)
            return confirmed
