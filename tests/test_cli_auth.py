from click.testing import CliRunner

from loom.platform.cli.main import _set_env_value, cli


def test_set_env_value_adds_key(tmp_path):
    env_path = tmp_path / ".env"

    _set_env_value(env_path, "OPENAI_API_KEY", "sk-test")

    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-test\n"


def test_set_env_value_replaces_key_and_preserves_other_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment\nOPENAI_API_KEY=old\nANTHROPIC_API_KEY=ant\n",
        encoding="utf-8",
    )

    _set_env_value(env_path, "OPENAI_API_KEY", "new")

    assert env_path.read_text(encoding="utf-8") == (
        "# comment\nOPENAI_API_KEY=new\nANTHROPIC_API_KEY=ant\n"
    )


def test_auth_openai_api_key_is_non_interactive(tmp_path):
    env_path = tmp_path / ".env"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "auth",
            "openai",
            "--api-key",
            "sk-test",
            "--env-file",
            str(env_path),
        ],
    )

    assert result.exit_code == 0
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-test\n"


def test_auth_xai_runs_oauth_only_login(tmp_path, monkeypatch):
    from loom.core.cognition import xai_auth

    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    monkeypatch.setenv("XAI_API_KEY", "xai-api-key-that-must-not-be-used")
    calls = []

    def fake_login(*, open_browser, timeout_seconds):
        calls.append({"open_browser": open_browser, "timeout_seconds": timeout_seconds})
        return {
            "tokens": {
                "access_token": "oauth-access-token",
                "refresh_token": "oauth-refresh-token",
            },
            "base_url": xai_auth.DEFAULT_XAI_BASE_URL,
        }

    monkeypatch.setattr(xai_auth, "run_xai_oauth_login", fake_login)
    runner = CliRunner()

    result = runner.invoke(cli, ["auth", "xai", "--no-browser", "--timeout", "12"])

    assert result.exit_code == 0
    assert calls == [{"open_browser": False, "timeout_seconds": 12.0}]
    auth_text = (tmp_path / "loom-home" / "auth.json").read_text(encoding="utf-8")
    assert "oauth-access-token" in auth_text
    assert "xai-api-key-that-must-not-be-used" not in auth_text
    assert "XAI_API_KEY" not in result.output
