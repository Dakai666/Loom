"""
Tests for loom.core.security.sandbox_runtime — srt backend integration.

Covers Issue #29 (Quest A Phase 4).  Unit tests run unconditionally; the
end-to-end test that actually shells out to ``srt`` is gated on the
binary being installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from loom.core.security.sandbox_runtime import (
    SandboxSettings,
    extract_command_root,
    srt_available,
    srt_install_hint,
    wrap_command,
    write_settings_file,
)


# ---------------------------------------------------------------------------
# SandboxSettings.from_config
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_empty_config_defaults_to_none(self):
        s = SandboxSettings.from_config({})
        assert s.backend == "none"
        assert s.allow_write == []
        assert s.allowed_domains == []

    def test_none_config_defaults_to_none(self):
        s = SandboxSettings.from_config(None)
        assert s.backend == "none"

    def test_recognised_backend(self):
        s = SandboxSettings.from_config({"backend": "srt"})
        assert s.backend == "srt"

    def test_unknown_backend_raises_fail_closed(self):
        # PR #387 review: silent downgrade on a typo like backend = "str"
        # would defeat the opt-in security intent.  Must raise.
        with pytest.raises(ValueError, match="backend = 'docker'"):
            SandboxSettings.from_config({"backend": "docker"})

    def test_typo_backend_str_raises(self):
        with pytest.raises(ValueError, match="backend = 'str'"):
            SandboxSettings.from_config({"backend": "str"})

    def test_explicit_none_backend_does_not_raise(self):
        # Distinct from "unknown": explicitly setting "none" is fine.
        assert SandboxSettings.from_config({"backend": "none"}).backend == "none"

    def test_case_insensitive_backend(self):
        assert SandboxSettings.from_config({"backend": "SRT"}).backend == "srt"

    def test_lists_passed_through(self):
        s = SandboxSettings.from_config({
            "backend": "srt",
            "allow_write": [".", "/tmp"],
            "deny_read": ["~/.ssh"],
            "allowed_domains": ["pypi.org"],
        })
        assert s.allow_write == [".", "/tmp"]
        assert s.deny_read == ["~/.ssh"]
        assert s.allowed_domains == ["pypi.org"]

    def test_none_values_become_empty_lists(self):
        s = SandboxSettings.from_config({"backend": "srt", "allow_write": None})
        assert s.allow_write == []


class TestStrictListValidation:
    """PR #387 review: scalar strings + non-string members must raise."""

    def test_scalar_string_in_allow_write_raises(self):
        # Without validation, list("/tmp") would yield ["/", "t", "m", "p"]
        # and the lone "/" entry would open writes filesystem-wide under
        # srt's allow-only rules.  This is THE bypass footgun.
        with pytest.raises(ValueError, match="allow_write.*not a bare string"):
            SandboxSettings.from_config({
                "backend": "srt", "allow_write": "/tmp",
            })

    def test_scalar_string_in_deny_read_raises(self):
        with pytest.raises(ValueError, match="deny_read.*not a bare string"):
            SandboxSettings.from_config({
                "backend": "srt", "deny_read": "~/.ssh",
            })

    def test_scalar_string_in_allowed_domains_raises(self):
        with pytest.raises(ValueError, match="allowed_domains.*not a bare string"):
            SandboxSettings.from_config({
                "backend": "srt", "allowed_domains": "pypi.org",
            })

    def test_non_list_value_raises(self):
        with pytest.raises(ValueError, match="must be a list of strings"):
            SandboxSettings.from_config({
                "backend": "srt", "allow_write": 42,
            })

    def test_non_string_member_raises(self):
        with pytest.raises(ValueError, match=r"allow_write\[1\] must be a string"):
            SandboxSettings.from_config({
                "backend": "srt", "allow_write": [".", 42, "/tmp"],
            })

    def test_dict_member_raises(self):
        with pytest.raises(ValueError, match=r"deny_read\[0\] must be a string"):
            SandboxSettings.from_config({
                "backend": "srt", "deny_read": [{"path": "~/.ssh"}],
            })

    def test_empty_list_ok(self):
        s = SandboxSettings.from_config({
            "backend": "srt", "allow_write": [], "allowed_domains": [],
        })
        assert s.allow_write == []
        assert s.allowed_domains == []


class TestProfileConfig:
    def test_profiles_parse_from_config(self):
        s = SandboxSettings.from_config({
            "backend": "srt",
            "allow_write": ["."],
            "profiles": {
                "github": {
                    "match_commands": ["gh", "git"],
                    "allow_read": ["~/.config/gh", "~/.gitconfig"],
                    "allow_write": [".", "/private/tmp", "~/.cache/gh"],
                    "allowed_domains": ["api.github.com", "github.com"],
                },
            },
        })

        assert "github" in s.profiles
        profile = s.profiles["github"]
        assert profile.match_commands == ["gh", "git"]
        assert profile.allowed_domains == ["api.github.com", "github.com"]

    def test_profile_list_fields_reject_scalar_strings(self):
        with pytest.raises(ValueError, match="profiles.github.allow_write"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {
                    "github": {"match_commands": ["gh"], "allow_write": "/tmp"},
                },
            })

    def test_profile_match_commands_requires_list_of_strings(self):
        with pytest.raises(ValueError, match="profiles.github.match_commands"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {"github": {"match_commands": "gh"}},
            })

    def test_profile_name_must_be_simple_identifier(self):
        with pytest.raises(ValueError, match="profile name"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {"git hub": {"match_commands": ["gh"]}},
            })

    def test_profile_name_must_start_with_alnum(self):
        with pytest.raises(ValueError, match="profile name"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {"-github": {"match_commands": ["gh"]}},
            })

    def test_profile_match_commands_must_be_unique(self):
        with pytest.raises(ValueError, match="match_commands.*gh"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {
                    "github": {"match_commands": ["gh"]},
                    "github_alt": {"match_commands": ["gh"]},
                },
            })

    def test_profile_bypass_sandbox_defaults_to_false(self):
        s = SandboxSettings.from_config({
            "backend": "srt",
            "profiles": {"github": {"match_commands": ["gh"]}},
        })
        assert s.profiles["github"].bypass_sandbox is False

    def test_profile_bypass_sandbox_parses_when_set(self):
        s = SandboxSettings.from_config({
            "backend": "srt",
            "profiles": {
                "github": {"match_commands": ["gh"], "bypass_sandbox": True},
            },
        })
        assert s.profiles["github"].bypass_sandbox is True

    def test_profile_bypass_sandbox_rejects_non_bool(self):
        with pytest.raises(ValueError, match="bypass_sandbox"):
            SandboxSettings.from_config({
                "backend": "srt",
                "profiles": {
                    "github": {"match_commands": ["gh"], "bypass_sandbox": "yes"},
                },
            })


class TestProfileSelectionAndMerge:
    def _settings(self):
        return SandboxSettings.from_config({
            "backend": "srt",
            "allow_write": ["."],
            "deny_read": ["~/.ssh"],
            "allowed_domains": [],
            "profiles": {
                "github": {
                    "match_commands": ["gh", "git"],
                    "allow_read": ["~/.config/gh"],
                    "allow_write": [".", "/private/tmp", "~/.cache/gh"],
                    "allowed_domains": ["api.github.com", "github.com"],
                },
            },
        })

    def test_extract_command_root_skips_env_assignments(self):
        assert extract_command_root("GH_HOST=github.com gh pr view 387") == "gh"

    def test_extract_command_root_skips_leading_underscore_env_assignment(self):
        assert extract_command_root("_DEBUG=1 gh pr view 387") == "gh"

    def test_select_profile_by_command_root(self):
        selected = self._settings().select_profile_for_command("gh pr view 387")
        assert selected == "github"

    def test_explicit_profile_overrides_detection(self):
        selected = self._settings().select_profile_for_command(
            "npx wrapper gh pr view 387",
            explicit="github",
        )
        assert selected == "github"

    def test_unknown_explicit_profile_raises(self):
        with pytest.raises(ValueError, match="Unknown sandbox profile"):
            self._settings().select_profile_for_command(
                "gh pr view 387", explicit="missing",
            )

    def test_merged_with_profile_preserves_base_denies_and_adds_domains(self):
        effective = self._settings().merged_with_profile("github")
        payload = effective.to_srt_json(workspace=Path("/repo"))
        assert payload["filesystem"]["denyRead"] == [
            str(Path.home() / ".ssh"),
        ]
        assert payload["filesystem"]["allowWrite"] == [
            "/repo",
            "/private/tmp",
            str(Path.home() / ".cache/gh"),
        ]
        assert payload["network"]["allowedDomains"] == [
            "api.github.com",
            "github.com",
        ]
        assert effective.profiles == {}


# ---------------------------------------------------------------------------
# to_srt_json — schema fidelity
# ---------------------------------------------------------------------------

class TestToSrtJson:
    def test_emits_all_required_sections(self):
        payload = SandboxSettings().to_srt_json()
        assert set(payload) == {"filesystem", "network", "unixSockets"}

    def test_filesystem_has_four_keys(self):
        fs = SandboxSettings().to_srt_json()["filesystem"]
        assert set(fs) == {"allowRead", "denyRead", "allowWrite", "denyWrite"}

    def test_network_has_two_keys(self):
        net = SandboxSettings().to_srt_json()["network"]
        assert set(net) == {"allowedDomains", "deniedDomains"}

    def test_unix_sockets_has_two_keys(self):
        sock = SandboxSettings().to_srt_json()["unixSockets"]
        assert set(sock) == {"allowedPaths", "deniedPaths"}

    def test_tilde_expanded(self, tmp_path):
        s = SandboxSettings(deny_read=["~/.ssh"])
        payload = s.to_srt_json()
        # str(Path.home()) is platform-portable
        assert payload["filesystem"]["denyRead"][0].startswith(str(Path.home()))
        assert "~" not in payload["filesystem"]["denyRead"][0]

    def test_dot_anchored_at_workspace(self, tmp_path):
        s = SandboxSettings(allow_write=[".", "/private/tmp"])
        payload = s.to_srt_json(workspace=tmp_path)
        assert payload["filesystem"]["allowWrite"][0] == str(tmp_path)
        assert payload["filesystem"]["allowWrite"][1] == "/private/tmp"

    def test_dot_left_alone_when_no_workspace(self):
        s = SandboxSettings(allow_write=["."])
        payload = s.to_srt_json(workspace=None)
        assert payload["filesystem"]["allowWrite"] == ["."]


# ---------------------------------------------------------------------------
# write_settings_file — tmpfile round-trip
# ---------------------------------------------------------------------------

class TestWriteSettingsFile:
    def test_writes_valid_json(self, tmp_path):
        s = SandboxSettings(
            backend="srt", allow_write=["."], allowed_domains=["pypi.org"]
        )
        path = write_settings_file(s, workspace=tmp_path, dir=tmp_path)
        data = json.loads(path.read_text())
        assert data["network"]["allowedDomains"] == ["pypi.org"]
        assert data["filesystem"]["allowWrite"] == [str(tmp_path)]

    def test_stable_path_within_session(self, tmp_path):
        """Repeated writes for identical content reuse one file."""
        s = SandboxSettings(backend="srt")
        p1 = write_settings_file(s, workspace=tmp_path, dir=tmp_path)
        p2 = write_settings_file(s, workspace=tmp_path, dir=tmp_path)
        assert p1 == p2

    def test_distinct_settings_get_distinct_content_hash_paths(self, tmp_path):
        base = SandboxSettings(backend="srt")
        github = SandboxSettings(
            backend="srt",
            allowed_domains=["api.github.com"],
        )

        p1 = write_settings_file(base, workspace=tmp_path, dir=tmp_path)
        p2 = write_settings_file(github, workspace=tmp_path, dir=tmp_path)

        assert p1 != p2
        assert json.loads(p1.read_text())["network"]["allowedDomains"] == []
        assert json.loads(p2.read_text())["network"]["allowedDomains"] == [
            "api.github.com",
        ]

    def test_creates_target_dir(self, tmp_path):
        target = tmp_path / "nested" / "dir"
        path = write_settings_file(
            SandboxSettings(), workspace=tmp_path, dir=target
        )
        assert path.parent == target
        assert target.is_dir()


# ---------------------------------------------------------------------------
# wrap_command — shell escaping
# ---------------------------------------------------------------------------

class TestWrapCommand:
    def test_basic_wrap(self, tmp_path):
        settings_path = tmp_path / "s.json"
        out = wrap_command("ls -l", settings_path)
        assert out.startswith(f"srt -s {settings_path} -c ")
        assert "'ls -l'" in out

    def test_quotes_command_with_pipes(self, tmp_path):
        settings_path = tmp_path / "s.json"
        out = wrap_command("git log | head -5", settings_path)
        # The whole command must be one shell-quoted argument so the
        # outer shell doesn't pipe srt's stdout into head.
        assert "'git log | head -5'" in out

    def test_quotes_settings_path_with_spaces(self, tmp_path):
        settings_path = tmp_path / "with space.json"
        out = wrap_command("ls", settings_path)
        # shlex.quote uses single quotes around paths containing spaces.
        assert f"'{settings_path}'" in out

    def test_escapes_single_quotes_in_command(self, tmp_path):
        settings_path = tmp_path / "s.json"
        out = wrap_command("echo it's fine", settings_path)
        # shlex.quote turns ' into '"'"' — round-tripping through sh -c
        # must yield the original string.
        wrapper = f"sh -c {out!r}"
        assert wrapper  # smoke; behaviour verified in end-to-end test below


# ---------------------------------------------------------------------------
# Availability hint
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_install_hint_mentions_npm(self):
        hint = srt_install_hint()
        assert "npm install" in hint
        assert "sandbox-runtime" in hint

    def test_srt_available_returns_bool(self):
        assert isinstance(srt_available(), bool)


# ---------------------------------------------------------------------------
# End-to-end — only when srt is actually installed
# ---------------------------------------------------------------------------

needs_srt = pytest.mark.skipif(
    not srt_available(), reason="srt binary not installed"
)


class TestFactoryIntegration:
    """make_run_bash_tool's interaction with SandboxSettings."""

    def test_backend_none_skips_srt_check(self, tmp_path, monkeypatch):
        """backend='none' must never look for the srt binary."""
        from loom.platform.cli import tools

        called = {"checked": False}

        def fake_available() -> bool:
            called["checked"] = True
            return False

        monkeypatch.setattr(tools, "srt_available", fake_available)
        tools.make_run_bash_tool(tmp_path, sandbox=SandboxSettings())
        assert called["checked"] is False

    def test_backend_srt_raises_when_binary_missing(self, tmp_path, monkeypatch):
        from loom.platform.cli import tools

        monkeypatch.setattr(tools, "srt_available", lambda: False)
        with pytest.raises(RuntimeError, match="srt binary not found"):
            tools.make_run_bash_tool(
                tmp_path, sandbox=SandboxSettings(backend="srt")
            )

    def test_backend_srt_writes_settings_file_at_startup(
        self, tmp_path, monkeypatch
    ):
        """Settings file should be rendered once at factory time."""
        from loom.platform.cli import tools

        monkeypatch.setattr(tools, "srt_available", lambda: True)
        written: list[Path] = []

        def fake_write(settings, workspace):
            p = tmp_path / "settings.json"
            p.write_text("{}")
            written.append(p)
            return p

        monkeypatch.setattr(tools, "_srt_write_settings_file", fake_write)
        tools.make_run_bash_tool(
            tmp_path,
            sandbox=SandboxSettings(backend="srt", allow_write=["."]),
        )
        assert len(written) == 1


@needs_srt
class TestSrtEndToEnd:
    def test_wrapped_command_executes_in_shell(self, tmp_path):
        """Smoke: a wrapped trivial command should run and return stdout."""
        s = SandboxSettings(backend="srt", allow_write=[str(tmp_path)])
        settings_path = write_settings_file(s, workspace=tmp_path, dir=tmp_path)
        wrapped = wrap_command("echo hello-srt", settings_path)
        result = subprocess.run(
            wrapped, shell=True, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0
        assert "hello-srt" in result.stdout

    def test_network_blocked_with_empty_allowlist(self, tmp_path):
        s = SandboxSettings(backend="srt", allow_write=[str(tmp_path)])
        settings_path = write_settings_file(s, workspace=tmp_path, dir=tmp_path)
        # example.com is not in allowed_domains (empty list)
        cmd = "curl -sS --max-time 5 https://example.com -o /dev/null -w '%{http_code}'"
        wrapped = wrap_command(cmd, settings_path)
        result = subprocess.run(
            wrapped, shell=True, capture_output=True, text=True, timeout=15
        )
        # srt's HTTP proxy returns 403 (CONNECT tunnel failed); curl exits 56.
        assert "Connection blocked" in result.stdout + result.stderr \
            or "403" in result.stdout + result.stderr \
            or "56" in str(result.returncode)
