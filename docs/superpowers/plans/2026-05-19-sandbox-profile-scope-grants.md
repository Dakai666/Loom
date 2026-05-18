# Sandbox Profile Scope Grants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `run_bash` use named srt sandbox profiles for CLIs such as `gh` only after the user approves the profile through the existing once / 30-minute / session / deny permission flow.

**Architecture:** Extend `SandboxSettings` with named `SandboxProfile` entries, detect or explicitly select a profile per `run_bash` call, emit a `sandbox_profile:use:<name>` scope requirement, and merge the approved profile into the per-call srt settings. The sandbox stays enabled for execution; authorization lives in `PermissionContext` only and disappears when the session ends.

**Tech Stack:** Python dataclasses, existing `PermissionContext` / `ScopeGrant` / `BlastRadiusMiddleware`, existing `run_bash` tool factory, pytest.

---

## File Structure

- Modify `loom/core/security/sandbox_runtime.py`
  - Owns `SandboxProfile`, profile parsing, command-root detection, profile selection, and sandbox settings merge.
- Modify `loom/platform/cli/tools.py`
  - Adds `sandbox_profile` input arg, wires profile-aware `run_bash` scope resolution, checks authorization metadata before applying a profile, and renders per-call srt settings.
- Modify `loom/core/harness/middleware.py`
  - Corrects scope-aware `ConfirmDecision.ONCE` semantics so "once" does not add a reusable grant.
- Modify `loom/core/harness/scope.py`
  - Adds a named exact matcher entry for `sandbox_profile` so the resource type is explicit.
- Modify `loom/core/session.py`
  - Passes profile-aware sandbox settings into the profile-aware run_bash resolver through the existing tool factory.
- Modify `loom.toml.example`, `doc/37-loom-toml-參考.md`, `doc/55-Sandbox-Runtime.md`
  - Documents profile config and runtime-only grant behavior.
- Modify `tests/test_sandbox_runtime.py`
  - Tests profile parsing, strict validation, command detection, and merge behavior.
- Modify `tests/test_scope_phase_b.py`
  - Tests `sandbox_profile` scope requirements and once/lease/session authorization behavior.
- Create `tests/test_run_bash_sandbox_profiles.py`
  - Tests foreground and async `run_bash` use merged per-call settings without invoking real srt.

---

### Task 1: Sandbox Profile Data Model and Merge

**Files:**
- Modify: `loom/core/security/sandbox_runtime.py`
- Test: `tests/test_sandbox_runtime.py`

- [ ] **Step 1: Run GitNexus impact analysis before editing symbols**

Run:

```bash
npx gitnexus impact SandboxSettings --direction upstream
```

Expected: report direct callers and risk. If risk is HIGH or CRITICAL, stop and warn the user before editing.

- [ ] **Step 2: Write failing tests for profile parsing and validation**

Add to `tests/test_sandbox_runtime.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest -q tests/test_sandbox_runtime.py::TestProfileConfig -q
```

Expected: FAIL because `SandboxProfile` and `profiles` do not exist yet.

- [ ] **Step 4: Implement profile dataclass and config parsing**

In `loom/core/security/sandbox_runtime.py`, add imports and helpers near `_as_path_list`:

```python
import re

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _profile_key(profile_name: str, field_name: str) -> str:
    return f"profiles.{profile_name}.{field_name}"


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"[security.sandbox] profile name must match "
            f"[A-Za-z0-9_.-]+, got {name!r}."
        )
    return name
```

Add this dataclass before `SandboxSettings`:

```python
@dataclass(slots=True)
class SandboxProfile:
    """Named per-CLI sandbox expansion, activated only through permission grants."""

    match_commands: list[str] = field(default_factory=list)
    allow_read: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    allowed_sockets: list[str] = field(default_factory=list)
    denied_sockets: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any] | None) -> "SandboxProfile":
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            raise ValueError(
                f"[security.sandbox] profiles.{name} must be a table, "
                f"got {type(cfg).__name__}: {cfg!r}."
            )
        return cls(
            match_commands=_as_path_list(
                _profile_key(name, "match_commands"),
                cfg.get("match_commands"),
            ),
            allow_read=_as_path_list(_profile_key(name, "allow_read"), cfg.get("allow_read")),
            deny_read=_as_path_list(_profile_key(name, "deny_read"), cfg.get("deny_read")),
            allow_write=_as_path_list(_profile_key(name, "allow_write"), cfg.get("allow_write")),
            deny_write=_as_path_list(_profile_key(name, "deny_write"), cfg.get("deny_write")),
            allowed_domains=_as_path_list(
                _profile_key(name, "allowed_domains"),
                cfg.get("allowed_domains"),
            ),
            denied_domains=_as_path_list(
                _profile_key(name, "denied_domains"),
                cfg.get("denied_domains"),
            ),
            allowed_sockets=_as_path_list(
                _profile_key(name, "allowed_sockets"),
                cfg.get("allowed_sockets"),
            ),
            denied_sockets=_as_path_list(
                _profile_key(name, "denied_sockets"),
                cfg.get("denied_sockets"),
            ),
        )
```

Extend `SandboxSettings`:

```python
    profiles: dict[str, SandboxProfile] = field(default_factory=dict)
```

In `SandboxSettings.from_config`, parse profiles:

```python
        raw_profiles = cfg.get("profiles", {}) or {}
        if not isinstance(raw_profiles, dict):
            raise ValueError(
                f"[security.sandbox] profiles must be a table, got "
                f"{type(raw_profiles).__name__}: {raw_profiles!r}."
            )
        profiles = {
            _validate_profile_name(name): SandboxProfile.from_config(name, value)
            for name, value in raw_profiles.items()
        }
```

Pass `profiles=profiles` into the returned `cls(...)`.

- [ ] **Step 5: Run profile parsing tests**

Run:

```bash
pytest -q tests/test_sandbox_runtime.py::TestProfileConfig
```

Expected: PASS.

- [ ] **Step 6: Write failing tests for command detection and merge**

Add to `tests/test_sandbox_runtime.py`:

```python
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
            self._settings().select_profile_for_command("gh pr view 387", explicit="missing")

    def test_merged_with_profile_preserves_base_denies_and_adds_domains(self):
        effective = self._settings().merged_with_profile("github")
        payload = effective.to_srt_json(workspace=Path("/repo"))
        assert payload["filesystem"]["denyRead"] == [str(Path.home() / ".ssh")]
        assert payload["filesystem"]["allowWrite"] == ["/repo", "/private/tmp", str(Path.home() / ".cache/gh")]
        assert payload["network"]["allowedDomains"] == ["api.github.com", "github.com"]
        assert effective.profiles == {}
```

- [ ] **Step 7: Run tests to verify they fail**

Run:

```bash
pytest -q tests/test_sandbox_runtime.py::TestProfileSelectionAndMerge
```

Expected: FAIL because selection and merge helpers do not exist.

- [ ] **Step 8: Implement command detection, profile selection, and merge**

Add these helpers to `loom/core/security/sandbox_runtime.py`:

```python
def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def extract_command_root(command: str) -> str | None:
    """Return the first executable token after leading KEY=value assignments."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for token in tokens:
        if "=" in token and not token.startswith("="):
            key, _value = token.split("=", 1)
            if key.replace("_", "").isalnum() and key[0].isalpha():
                continue
        return Path(token).name
    return None
```

Add methods to `SandboxSettings`:

```python
    def select_profile_for_command(
        self, command: str, *, explicit: str | None = None,
    ) -> str | None:
        if explicit:
            if explicit not in self.profiles:
                raise ValueError(f"Unknown sandbox profile {explicit!r}.")
            return explicit

        root = extract_command_root(command)
        if not root:
            return None
        matches = [
            name for name, profile in self.profiles.items()
            if root in profile.match_commands
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Command {root!r} matches multiple sandbox profiles: "
                + ", ".join(sorted(matches))
            )
        return matches[0] if matches else None

    def merged_with_profile(self, profile_name: str) -> "SandboxSettings":
        if profile_name not in self.profiles:
            raise ValueError(f"Unknown sandbox profile {profile_name!r}.")
        p = self.profiles[profile_name]
        return SandboxSettings(
            backend=self.backend,
            allow_read=_dedupe(self.allow_read + p.allow_read),
            deny_read=_dedupe(self.deny_read + p.deny_read),
            allow_write=_dedupe(self.allow_write + p.allow_write),
            deny_write=_dedupe(self.deny_write + p.deny_write),
            allowed_domains=_dedupe(self.allowed_domains + p.allowed_domains),
            denied_domains=_dedupe(self.denied_domains + p.denied_domains),
            allowed_sockets=_dedupe(self.allowed_sockets + p.allowed_sockets),
            denied_sockets=_dedupe(self.denied_sockets + p.denied_sockets),
            profiles={},
        )
```

Export `SandboxProfile` and `extract_command_root` from `loom/core/security/__init__.py`.

- [ ] **Step 9: Run sandbox runtime tests**

Run:

```bash
pytest -q tests/test_sandbox_runtime.py
```

Expected: PASS. If srt is installed in a sandboxed developer environment, run with the local escalation path already used for PR #387 so the srt e2e can bind its localhost proxy.

- [ ] **Step 10: Commit Task 1**

Run:

```bash
git add loom/core/security/sandbox_runtime.py loom/core/security/__init__.py tests/test_sandbox_runtime.py
git commit -m "feat(sandbox): add CLI sandbox profiles"
```

Expected: commit succeeds.

---

### Task 2: Profile-Aware Scope Requirements

**Files:**
- Modify: `loom/platform/cli/tools.py`
- Modify: `loom/core/harness/scope.py`
- Test: `tests/test_scope_phase_b.py`

- [ ] **Step 1: Run GitNexus impact analysis before editing symbols**

Run:

```bash
npx gitnexus impact _make_run_bash_resolver --direction upstream
npx gitnexus impact get_matcher --direction upstream
```

Expected: report direct callers and risk. If risk is HIGH or CRITICAL, stop and warn the user before editing.

- [ ] **Step 2: Write failing resolver tests**

Modify the `_bash_resolver` setup in `tests/test_scope_phase_b.py` so existing tests keep using no profile settings:

```python
_bash_resolver = _make_run_bash_resolver(_WORKSPACE)
```

Then add:

```python
from loom.core.security.sandbox_runtime import SandboxSettings


def _profile_settings():
    return SandboxSettings.from_config({
        "backend": "srt",
        "profiles": {
            "github": {
                "match_commands": ["gh"],
                "allow_read": ["~/.config/gh"],
                "allow_write": [".", "/private/tmp", "~/.cache/gh"],
                "allowed_domains": ["api.github.com", "github.com"],
            },
        },
    })


class TestRunBashSandboxProfileResolver:
    def test_auto_detects_github_profile(self):
        resolver = _make_run_bash_resolver(_WORKSPACE, sandbox=_profile_settings())
        call = _call("run_bash", {"command": "gh pr view 387"}, caps=ToolCapability.EXEC)
        req = resolver(call)

        profile_reqs = [r for r in req.requirements if r.resource == "sandbox_profile"]
        assert len(profile_reqs) == 1
        r = profile_reqs[0]
        assert r.action == "use"
        assert r.selector == "github"
        assert r.constraints["allowed_domains"] == ["api.github.com", "github.com"]
        assert req.metadata["sandbox_profile"] == "github"

    def test_explicit_profile_argument_selects_profile(self):
        resolver = _make_run_bash_resolver(_WORKSPACE, sandbox=_profile_settings())
        call = _call(
            "run_bash",
            {"command": "npx wrapper gh pr view 387", "sandbox_profile": "github"},
            caps=ToolCapability.EXEC,
        )
        req = resolver(call)
        assert req.metadata["sandbox_profile"] == "github"

    def test_unknown_explicit_profile_marks_request_as_invalid(self):
        resolver = _make_run_bash_resolver(_WORKSPACE, sandbox=_profile_settings())
        call = _call(
            "run_bash",
            {"command": "gh pr view 387", "sandbox_profile": "missing"},
            caps=ToolCapability.EXEC,
        )
        with pytest.raises(ValueError, match="Unknown sandbox profile"):
            resolver(call)
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest -q tests/test_scope_phase_b.py::TestRunBashSandboxProfileResolver
```

Expected: FAIL because `_make_run_bash_resolver` does not accept `sandbox`.

- [ ] **Step 4: Add explicit sandbox profile matcher**

In `loom/core/harness/scope.py`, add:

```python
class SandboxProfileMatcher:
    """Exact profile-name matcher for per-session sandbox profile grants."""
    def covers(self, grant: _HasScopeFields, requirement: _HasScopeFields) -> bool:
        if grant.action != requirement.action:
            return False
        return grant.selector == requirement.selector
```

Then extend `_MATCHERS`:

```python
    "sandbox_profile": SandboxProfileMatcher(),
```

- [ ] **Step 5: Extend `_make_run_bash_resolver`**

In `loom/platform/cli/tools.py`, change the signature:

```python
def _make_run_bash_resolver(workspace: Path, sandbox: SandboxSettings | None = None):
```

Inside `_resolve`, after the existing exec requirement is built, append profile requirement:

```python
        requirements = [
            ScopeRequirement(
                resource="exec",
                action="execute",
                selector="workspace",
                constraints={"has_absolute_paths": has_absolute},
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ]

        profile_name: str | None = None
        if sandbox is not None and sandbox.backend == "srt" and sandbox.profiles:
            explicit_profile = (call.args.get("sandbox_profile") or "").strip() or None
            profile_name = sandbox.select_profile_for_command(
                command, explicit=explicit_profile,
            )
            if profile_name:
                profile = sandbox.profiles[profile_name]
                requirements.append(ScopeRequirement(
                    resource="sandbox_profile",
                    action="use",
                    selector=profile_name,
                    constraints={
                        "allow_read": list(profile.allow_read),
                        "allow_write": list(profile.allow_write),
                        "allowed_domains": list(profile.allowed_domains),
                        "allowed_sockets": list(profile.allowed_sockets),
                    },
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ))

        metadata = {"sandbox_profile": profile_name} if profile_name else {}
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=requirements,
            metadata=metadata,
        )
```

Preserve the existing `scope_unknown` branch by adding the same profile requirement there when a profile is selected. If parsing is too ambiguous to auto-detect, explicit `sandbox_profile` is still honored.

- [ ] **Step 6: Run resolver tests**

Run:

```bash
pytest -q tests/test_scope_phase_b.py::TestRunBashResolver tests/test_scope_phase_b.py::TestRunBashSandboxProfileResolver
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add loom/platform/cli/tools.py loom/core/harness/scope.py tests/test_scope_phase_b.py
git commit -m "feat(sandbox): request profile grants for run_bash"
```

Expected: commit succeeds.

---

### Task 3: Correct Once vs Lease vs Session Semantics

**Files:**
- Modify: `loom/core/harness/middleware.py`
- Test: `tests/test_scope_phase_b.py`

- [ ] **Step 1: Run GitNexus impact analysis before editing symbol**

Run:

```bash
npx gitnexus impact _scope_aware_process --direction upstream
```

Expected: report direct callers and risk. If risk is HIGH or CRITICAL, stop and warn the user before editing.

- [ ] **Step 2: Write failing tests for ONCE not creating reusable grants**

Add to `tests/test_scope_phase_b.py`:

```python
from loom.core.harness.scope import ConfirmDecision


class TestScopeConfirmDurations:
    async def test_once_confirmation_does_not_create_reusable_scope_grant(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file",
            caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/once.md"}, caps=ToolCapability.MUTATES)

        result, confirm_fn, handler = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.ONCE, registry=reg,
        )

        assert result.success
        confirm_fn.assert_called_once()
        handler.assert_called_once()
        assert perm.grants == []
        assert call.metadata["confirm_decision"] == "once"

    async def test_scope_confirmation_creates_ttl_grant(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file",
            caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/lease.md"}, caps=ToolCapability.MUTATES)

        result, _confirm_fn, _handler = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.SCOPE, registry=reg,
        )

        assert result.success
        assert len(perm.grants) == 1
        assert perm.grants[0].source == "lease"
        assert perm.grants[0].valid_until > 0

    async def test_auto_confirmation_creates_session_grant(self):
        perm = PermissionContext(session_id="test")
        reg = _make_registry(_tool_def(
            "write_file",
            caps=ToolCapability.MUTATES,
            scope_resolver=_make_write_file_resolver(_WORKSPACE),
        ))
        call = _call("write_file", {"path": "doc/session.md"}, caps=ToolCapability.MUTATES)

        result, _confirm_fn, _handler = await _run_middleware(
            perm, call, confirm_result=ConfirmDecision.AUTO, registry=reg,
        )

        assert result.success
        assert len(perm.grants) == 1
        assert perm.grants[0].source == "auto_approve"
        assert perm.grants[0].valid_until == 0
```

Update the existing `test_grants_created_after_confirm` so it passes `ConfirmDecision.SCOPE` instead of bare `True`:

```python
await _run_middleware(perm, call, confirm_result=ConfirmDecision.SCOPE, registry=reg)
```

- [ ] **Step 3: Run tests to verify ONCE test fails**

Run:

```bash
pytest -q tests/test_scope_phase_b.py::TestScopeConfirmDurations tests/test_scope_phase_b.py::TestBlastRadiusScopeAware::test_grants_created_after_confirm
```

Expected: FAIL because ONCE currently adds a reusable grant.

- [ ] **Step 4: Change scope-aware ONCE handling**

In `loom/core/harness/middleware.py`, replace the ONCE branch:

```python
        if decision == ConfirmDecision.ONCE:
            call.metadata["once_authorized"] = True
        elif decision == ConfirmDecision.SCOPE:
            self._request_to_grants(
                scope_request, source="lease",
                valid_until=_time.time() + self._SCOPE_LEASE_TTL,
            )
        elif decision == ConfirmDecision.AUTO:
            self._request_to_grants(scope_request, source="auto_approve")
```

Leave legacy-path behavior unchanged in this task unless a test shows legacy UI semantics break. This task targets scope-aware calls, which `run_bash` uses.

- [ ] **Step 5: Run scope duration tests**

Run:

```bash
pytest -q tests/test_scope_phase_b.py::TestScopeConfirmDurations tests/test_scope_phase_b.py::TestBlastRadiusScopeAware
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add loom/core/harness/middleware.py tests/test_scope_phase_b.py
git commit -m "fix(harness): make scope once approvals single-use"
```

Expected: commit succeeds.

---

### Task 4: Apply Approved Profiles in run_bash Execution

**Files:**
- Modify: `loom/platform/cli/tools.py`
- Test: create `tests/test_run_bash_sandbox_profiles.py`

- [ ] **Step 1: Run GitNexus impact analysis before editing symbol**

Run:

```bash
npx gitnexus impact make_run_bash_tool --direction upstream
```

Expected: report direct callers and risk. If risk is HIGH or CRITICAL, stop and warn the user before editing.

- [ ] **Step 2: Write failing tests for per-call profile settings**

Create `tests/test_run_bash_sandbox_profiles.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.scope import PermissionVerdict, ScopeRequirement, ScopeRequest
from loom.core.security.sandbox_runtime import SandboxSettings
from loom.platform.cli.tools import make_run_bash_tool


def _settings():
    return SandboxSettings.from_config({
        "backend": "srt",
        "allow_write": ["."],
        "profiles": {
            "github": {
                "match_commands": ["gh"],
                "allow_write": [".", "/private/tmp", "~/.cache/gh"],
                "allowed_domains": ["api.github.com"],
            },
        },
    })


def _call(args, *, authorized=True):
    call = ToolCall(
        tool_name="run_bash",
        args=args,
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.EXEC,
        session_id="s1",
    )
    if authorized:
        call.metadata["scope_verdict"] = PermissionVerdict.ALLOW
        call.metadata["scope_request"] = ScopeRequest(
            tool_name="run_bash",
            capabilities=ToolCapability.EXEC,
            requirements=[
                ScopeRequirement(
                    resource="sandbox_profile",
                    action="use",
                    selector="github",
                    tool_name="run_bash",
                    capabilities=ToolCapability.EXEC,
                ),
            ],
            metadata={"sandbox_profile": "github"},
        )
    return call


async def test_profile_command_uses_merged_settings(tmp_path, monkeypatch):
    from loom.platform.cli import tools

    monkeypatch.setattr(tools, "srt_available", lambda: True)
    written = []

    def fake_write(settings, workspace):
        written.append(settings)
        return tmp_path / f"settings-{len(written)}.json"

    monkeypatch.setattr(tools, "_srt_write_settings_file", fake_write)
    monkeypatch.setattr(tools, "_srt_wrap_command", lambda cmd, path: f"srt:{path}:{cmd}")

    captured = {}

    class FakeProc:
        returncode = 0
        async def communicate(self):
            return b"ok", None

    async def fake_shell(command, **kwargs):
        captured["command"] = command
        return FakeProc()

    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(tools, "_terminate_proc_group", AsyncMock())

    tool = make_run_bash_tool(tmp_path, sandbox=_settings())
    result = await tool.executor(_call({"command": "gh pr view 387"}))

    assert result.success is True
    assert "srt:" in captured["command"]
    assert len(written) == 2
    effective = written[-1]
    assert effective.allowed_domains == ["api.github.com"]
    assert "/private/tmp" in effective.allow_write


async def test_profile_command_without_scope_metadata_fails_closed(tmp_path, monkeypatch):
    from loom.platform.cli import tools

    monkeypatch.setattr(tools, "srt_available", lambda: True)
    monkeypatch.setattr(tools, "_srt_write_settings_file", lambda settings, workspace: tmp_path / "settings.json")

    tool = make_run_bash_tool(tmp_path, sandbox=_settings())
    result = await tool.executor(_call({"command": "gh pr view 387"}, authorized=False))

    assert result.success is False
    assert result.failure_type == "permission_denied"
    assert "sandbox profile 'github' was not authorized" in result.error
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
pytest -q tests/test_run_bash_sandbox_profiles.py
```

Expected: FAIL because executor does not create per-call merged settings or check profile authorization metadata.

- [ ] **Step 4: Add profile authorization helper in `make_run_bash_tool`**

Inside `make_run_bash_tool`, add:

```python
    def _selected_profile_for_call(call: ToolCall) -> str | None:
        if not _sandbox_active or sandbox is None:
            return None
        explicit_profile = (call.args.get("sandbox_profile") or "").strip() or None
        return sandbox.select_profile_for_command(
            call.args.get("command", ""), explicit=explicit_profile,
        )

    def _profile_authorized_for_call(call: ToolCall, profile_name: str) -> bool:
        scope_request = call.metadata.get("scope_request")
        if scope_request is None:
            return False
        for req in getattr(scope_request, "requirements", []):
            if (
                req.resource == "sandbox_profile"
                and req.action == "use"
                and req.selector == profile_name
            ):
                return bool(
                    call.metadata.get("once_authorized")
                    or call.metadata.get("user_decision")
                    or call.metadata.get("scope_verdict")
                )
        return False
```

Change `_maybe_wrap` to accept `call`:

```python
    def _maybe_wrap(cmd: str, call: ToolCall) -> tuple[str, str | None]:
        if not _sandbox_active or _sandbox_settings_path is None or sandbox is None:
            return cmd, None
        profile_name = _selected_profile_for_call(call)
        settings_path = _sandbox_settings_path
        if profile_name:
            if not _profile_authorized_for_call(call, profile_name):
                raise PermissionError(
                    f"sandbox profile {profile_name!r} was not authorized for this run_bash call"
                )
            effective = sandbox.merged_with_profile(profile_name)
            settings_path = _srt_write_settings_file(effective, workspace)
        return _srt_wrap_command(cmd, settings_path), profile_name
```

In the foreground path:

```python
            wrapped_command, profile_name = _maybe_wrap(command, call)
            proc = await asyncio.create_subprocess_shell(
                wrapped_command,
                ...
            )
```

In the async path, call `_maybe_wrap` before `jobstore.submit`; if it raises `PermissionError`, return a denied `ToolResult` instead of submitting:

```python
            try:
                wrapped_command, profile_name = _maybe_wrap(command, call)
            except PermissionError as exc:
                return ToolResult(
                    call_id=call.id,
                    tool_name=call.tool_name,
                    success=False,
                    error=str(exc),
                    failure_type="permission_denied",
                )
            job_id = jobstore.submit(
                "run_bash",
                {"command": command, "timeout": timeout, "sandbox_profile": profile_name},
                lambda: _run_bash_job(wrapped_command, cwd, timeout, scratchpad),
            )
```

Apply the same `PermissionError` handling around foreground wrapping.

- [ ] **Step 5: Add `sandbox_profile` to tool schema and wire resolver settings**

In `make_run_bash_tool` input schema, add:

```python
                "sandbox_profile": {
                    "type": "string",
                    "description": (
                        "Optional named sandbox profile to request for this command. "
                        "Still requires human approval through the normal permission flow."
                    ),
                },
```

At the return `ToolDefinition`, change:

```python
        scope_resolver=_make_run_bash_resolver(workspace, sandbox=sandbox),
```

- [ ] **Step 6: Run run_bash profile tests**

Run:

```bash
pytest -q tests/test_run_bash_sandbox_profiles.py
```

Expected: PASS.

- [ ] **Step 7: Run existing integration tests for run_bash**

Run:

```bash
pytest -q tests/test_integration.py::TestTools::test_run_bash_success tests/test_integration.py::TestTools::test_run_bash_timeout
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add loom/platform/cli/tools.py tests/test_run_bash_sandbox_profiles.py tests/test_integration.py
git commit -m "feat(sandbox): apply approved profiles to run_bash"
```

Expected: commit succeeds.

---

### Task 5: Session Wiring, Documentation, and Config Examples

**Files:**
- Modify: `loom/core/session.py`
- Modify: `loom.toml.example`
- Modify: `doc/37-loom-toml-參考.md`
- Modify: `doc/55-Sandbox-Runtime.md`
- Test: `tests/test_sandbox_runtime.py`

- [ ] **Step 1: Run GitNexus impact analysis before editing session wiring**

Run:

```bash
npx gitnexus impact LoomSession --direction upstream
```

Expected: report direct callers and risk. If risk is HIGH or CRITICAL, stop and warn the user before editing.

- [ ] **Step 2: Confirm session wiring already passes `SandboxSettings`**

Inspect `loom/core/session.py` and verify it still passes `sandbox=_sandbox_settings` into `make_run_bash_tool`. If Task 4 only changed the resolver factory inside `make_run_bash_tool`, no session code change is needed.

Run:

```bash
rg -n "make_run_bash_tool|sandbox=_sandbox_settings" loom/core/session.py
```

Expected: both strings appear near the run_bash registration.

- [ ] **Step 3: Add docs for profiles**

Add to `loom.toml.example` under `[security.sandbox]`:

```toml
# Optional CLI-specific profiles. Profiles do not grant access by
# themselves; they are requested by run_bash and must be approved through
# the normal permission prompt (once / 30 min / session / deny).
#
# [security.sandbox.profiles.github]
# match_commands = ["gh", "git"]
# allow_read = ["~/.config/gh", "~/.gitconfig"]
# allow_write = [".", "/private/tmp", "~/.cache/gh"]
# allowed_domains = [
#   "api.github.com",
#   "github.com",
#   "raw.githubusercontent.com",
#   "objects.githubusercontent.com",
#   "codeload.github.com",
# ]
```

Add equivalent concise text to `doc/37-loom-toml-參考.md`:

```markdown
### Sandbox profiles

Profiles describe CLI-specific sandbox expansions. A profile is not an
automatic allowlist; `run_bash` requests it and the user approves once, for
30 minutes, for the current session, or denies it.
```

Add operational guidance to `doc/55-Sandbox-Runtime.md`:

```markdown
## CLI profiles

Use profiles for tools such as `gh`, `opencli`, `npm`, `pip`, or project
wrappers that need a predictable set of domains and config/cache paths.
Profiles are runtime grants, not persistent authorization.
```

- [ ] **Step 4: Run docs/config grep checks**

Run:

```bash
rg -n "sandbox.profiles|match_commands|once / 30 min / session / deny" loom.toml.example doc/37-loom-toml-參考.md doc/55-Sandbox-Runtime.md
```

Expected: all three docs mention profile runtime authorization.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add loom.toml.example doc/37-loom-toml-參考.md doc/55-Sandbox-Runtime.md loom/core/session.py
git commit -m "docs(sandbox): document CLI profile grants"
```

Expected: commit succeeds.

---

### Task 6: Final Verification and Review Comment

**Files:**
- No source edits expected unless verification exposes a bug.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest -q tests/test_sandbox_runtime.py tests/test_scope_phase_b.py tests/test_run_bash_sandbox_profiles.py
```

Expected: PASS.

- [ ] **Step 2: Run neighboring security/run_bash test slice**

Run:

```bash
pytest -q tests -k "sandbox or security or run_bash or command_scanner or self_termination"
```

Expected: PASS. If local Codex sandbox blocks srt localhost proxy e2e, rerun with the approved local escalation used during PR #387 review.

- [ ] **Step 3: Run formatting and diff checks**

Run:

```bash
python -m py_compile loom/core/security/sandbox_runtime.py loom/platform/cli/tools.py loom/core/harness/scope.py loom/core/harness/middleware.py
git diff --check
```

Expected: no output from `git diff --check`, py_compile exits 0.

- [ ] **Step 4: Run GitNexus change detection**

Run:

```bash
npx gitnexus detect-changes --scope compare --base-ref master
```

Expected: risk level is low or medium and affected flows are limited to run_bash / sandbox / permission paths. If high or critical, stop and review the affected process list before creating a PR.

- [ ] **Step 5: Manual smoke through middleware**

Run this focused smoke script:

```bash
python - <<'PY'
from pathlib import Path
from unittest.mock import AsyncMock

import asyncio

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.scope import ConfirmDecision
from loom.core.security.sandbox_runtime import SandboxSettings
from loom.platform.cli.tools import _make_run_bash_resolver

workspace = Path.cwd()
settings = SandboxSettings.from_config({
    "backend": "srt",
    "profiles": {
        "github": {
            "match_commands": ["gh"],
            "allowed_domains": ["api.github.com"],
            "allow_write": [".", "/private/tmp"],
        },
    },
})

async def run_once(decision):
    perm = PermissionContext(session_id="smoke")
    call = ToolCall(
        tool_name="run_bash",
        args={"command": "gh pr view 387"},
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.EXEC,
        session_id="smoke",
    )
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="run_bash",
        description="smoke",
        trust_level=TrustLevel.GUARDED,
        input_schema={},
        executor=AsyncMock(return_value=ToolResult(call_id=call.id, tool_name="run_bash", success=True)),
        capabilities=ToolCapability.EXEC,
        scope_resolver=_make_run_bash_resolver(workspace, sandbox=settings),
    ))
    handler = AsyncMock(return_value=ToolResult(call_id=call.id, tool_name="run_bash", success=True))
    mw = BlastRadiusMiddleware(
        perm_ctx=perm,
        confirm_fn=AsyncMock(return_value=decision),
        registry=reg,
    )
    result = await mw.process(call, handler)
    return result, perm, handler

async def main():
    once_result, once_perm, once_handler = await run_once(ConfirmDecision.ONCE)
    assert once_result.success
    assert once_handler.called
    assert once_perm.grants == []

    scope_result, scope_perm, _ = await run_once(ConfirmDecision.SCOPE)
    assert scope_result.success
    assert len(scope_perm.grants) == 2
    assert any(g.resource == "sandbox_profile" and g.valid_until > 0 for g in scope_perm.grants)

    session_result, session_perm, _ = await run_once(ConfirmDecision.AUTO)
    assert session_result.success
    assert len(session_perm.grants) == 2
    assert any(g.resource == "sandbox_profile" and g.valid_until == 0 for g in session_perm.grants)

    fresh = PermissionContext(session_id="fresh")
    assert fresh.grants == []
    print("sandbox profile permission smoke passed")

asyncio.run(main())
PY
```

Expected: `sandbox profile permission smoke passed`.

- [ ] **Step 6: Prepare PR summary**

Use this PR body:

```markdown
## Summary

- add named srt sandbox profiles for CLI-specific domains and config/cache paths
- route profile use through existing run_bash scope grants and human confirmation
- apply approved profiles per call without changing global sandbox settings
- fix scope-aware ONCE semantics so one-time approval is single-use

## Verification

- `pytest -q tests/test_sandbox_runtime.py tests/test_scope_phase_b.py tests/test_run_bash_sandbox_profiles.py`
- `pytest -q tests -k "sandbox or security or run_bash or command_scanner or self_termination"`
- `python -m py_compile loom/core/security/sandbox_runtime.py loom/platform/cli/tools.py loom/core/harness/scope.py loom/core/harness/middleware.py`
- `git diff --check`
- `npx gitnexus detect-changes --scope compare --base-ref master`
```
