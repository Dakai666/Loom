import base64
import json
import time

from loom.core.cognition.openai_auth import (
    load_codex_oauth_credential,
    resolve_openai_credential,
)


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}."


def test_load_codex_oauth_credential_reads_unexpired_token(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({
            "tokens": {
                "access_token": _jwt(int(time.time()) + 3600),
                "refresh_token": "refresh-secret",
                "account_id": "acct",
            }
        }),
        encoding="utf-8",
    )

    credential = load_codex_oauth_credential(auth_file)

    assert credential is not None
    assert credential.source == "codex_oauth"
    assert credential.account_id == "acct"


def test_load_codex_oauth_credential_ignores_expired_token(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"tokens": {"access_token": _jwt(int(time.time()) - 10)}}),
        encoding="utf-8",
    )

    assert load_codex_oauth_credential(auth_file) is None


def test_resolve_openai_credential_falls_back_to_api_key(tmp_path):
    missing_codex = tmp_path / "missing.json"

    credential = resolve_openai_credential(
        {"OPENAI_API_KEY": "sk-test"},
        codex_auth_file=missing_codex,
    )

    assert credential is not None
    assert credential.source == "api_key"
    assert credential.token == "sk-test"
