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
