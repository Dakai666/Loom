"""OpenAI credential helpers.

This module intentionally treats Codex CLI credentials as an experimental
credential source.  The file format is owned by Codex, so callers must be
prepared to fall back to a normal ``OPENAI_API_KEY`` when the token is absent,
expired, or no longer shaped as expected.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OpenAICredential:
    """Bearer credential plus non-secret provenance."""

    token: str
    source: str
    account_id: str | None = None
    expires_at: int | None = None


def codex_auth_path() -> Path:
    """Return the default Codex CLI auth file path."""

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return home / "auth.json"


def _jwt_exp(token: str) -> int | None:
    """Best-effort JWT exp extraction without validating the signature."""

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


def load_codex_oauth_credential(
    path: Path | None = None,
    *,
    now: float | None = None,
    min_ttl_seconds: int = 60,
) -> OpenAICredential | None:
    """Load an unexpired Codex CLI OAuth access token if one is available."""

    auth_path = path or codex_auth_path()
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    expires_at = _jwt_exp(access_token)
    current = time.time() if now is None else now
    if expires_at is not None and expires_at <= current + min_ttl_seconds:
        return None
    account_id = tokens.get("account_id")
    return OpenAICredential(
        token=access_token,
        source="codex_oauth",
        account_id=account_id if isinstance(account_id, str) else None,
        expires_at=expires_at,
    )


def resolve_openai_credential(
    env: dict[str, str] | None = None,
    *,
    prefer_codex: bool = True,
    require_codex: bool = False,
    codex_auth_file: Path | None = None,
) -> OpenAICredential | None:
    """Resolve an OpenAI bearer credential without exposing secret values."""

    env_map: dict[str, Any] = env if env is not None else os.environ
    if prefer_codex:
        codex = load_codex_oauth_credential(codex_auth_file)
        if codex is not None:
            return codex
        if require_codex:
            return None
    api_key = env_map.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        return OpenAICredential(token=api_key, source="api_key")
    return None
