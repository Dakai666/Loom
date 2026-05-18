# Sandbox Profile Scope Grants Design

Date: 2026-05-18

## Goal

Make OS-level sandboxing practical for real CLI workflows without turning the
global sandbox allowlist into a permanent hole.

The sandbox remains the safety wall. Human authorization remains the control
surface. A CLI such as `gh`, `opencli`, or an Obsidian-related CLI can request a
named sandbox profile, and the user can approve it once, for 30 minutes, for
the current session, or deny it. Grants are runtime-only and disappear when the
session ends or Loom restarts.

## Non-Goals

- No persistent profile authorization across sessions.
- No unsandboxed automatic escape hatch for CLI tools.
- No broad global domain/path allowlist as the recommended workflow.
- No deep shell parser in the first version.

## User Model

When the agent asks to run:

```bash
gh pr comment 387 --body "..."
```

Loom detects that `gh` matches the `github` sandbox profile and prompts:

```text
Allow run_bash with sandbox profile "github"?

This profile adds:
  network: api.github.com, github.com, raw.githubusercontent.com, ...
  read: ~/.config/gh, ~/.gitconfig
  write: ~/.cache/gh, workspace, /private/tmp

Options: once / 30 min / this session / deny
```

This mirrors existing guarded tool behavior: the agent can propose the action,
but the user controls whether the profile is available.

## Configuration

Base sandbox config stays strict:

```toml
[security.sandbox]
backend = "srt"
allow_write = [".", "/private/tmp"]
deny_read = ["~/.ssh", "~/.aws", "~/.gnupg", ".env"]
allowed_domains = []
```

Profiles live under the same block:

```toml
[security.sandbox.profiles.github]
match_commands = ["gh", "git"]
allow_read = ["~/.config/gh", "~/.gitconfig"]
allow_write = [".", "/private/tmp", "~/.cache/gh"]
allowed_domains = [
  "api.github.com",
  "github.com",
  "raw.githubusercontent.com",
  "objects.githubusercontent.com",
  "codeload.github.com",
]
```

Future CLIs are added as data, not code:

```toml
[security.sandbox.profiles.opencli]
match_commands = ["opencli"]
allow_read = ["~/.config/opencli"]
allow_write = [".", "/private/tmp", "~/.cache/opencli"]
allowed_domains = ["api.openai.com"]
```

## Detection

The first version supports two ways to select a profile:

1. Automatic detection from the shell command's executable token.
2. Explicit `run_bash` argument, for wrapper commands or ambiguous shells:

```json
{
  "command": "npx some-wrapper gh pr view 387",
  "sandbox_profile": "github"
}
```

Explicit selection does not bypass authorization. It only tells Loom which
profile to request.

The first version intentionally keeps parsing shallow:

- Parse the first executable token after leading environment assignments.
- Match against `profiles.*.match_commands`.
- If the command contains complex shell forms that hide the executable, rely on
  explicit `sandbox_profile`.
- If multiple profiles match, fail closed and ask for explicit selection.

## Permission Flow

`run_bash` scope resolution gains a sandbox profile requirement:

```text
ScopeRequirement(
  resource="sandbox_profile",
  action="use",
  selector="github",
  constraints={
    "network": [...],
    "allow_read": [...],
    "allow_write": [...],
  },
)
```

This requirement goes through the existing `PermissionContext` and confirm
middleware:

- Approve once: grants only this call.
- 30 minutes: creates a TTL scope grant.
- This session: creates a non-persistent session grant.
- Deny: command does not run.

Profile grants live only in memory. They are not written to `loom.toml` and are
not restored after restart.

## Sandbox Merge

Execution uses a per-call effective sandbox:

```text
effective = base_sandbox + approved_profile
```

Merge rules:

- Lists are unioned and de-duplicated while preserving stable order.
- Base deny rules remain present.
- Profile allow rules may add domains, reads, writes, and sockets.
- Unknown profile, invalid profile config, or settings render failure is a hard
  error before subprocess execution.
- If a profile is detected but not approved, do not run under base settings as
  a fallback, because that turns an authorization denial into a confusing
  sandbox failure.

The merged settings file should be generated per call or cached by a stable
hash of the effective settings.

## UI and Messaging

The confirmation prompt should make profile expansion visible in plain terms:

- Profile name.
- Matched command.
- Added network domains.
- Added readable paths.
- Added writable paths.
- Grant duration options.

If srt blocks a command due to missing network/path permission, Loom should
surface a helpful hint:

```text
Sandbox blocked network access to api.example.com.
Add it to a sandbox profile and approve that profile for run_bash.
```

This is a follow-up nicety, not required for the first merge.

## Security Properties

- Profiles are not automatic allowlists; they are permission requests.
- Human approval is required before profile privileges affect execution.
- The sandbox remains active for all profile-based commands.
- Existing `CommandScanner`, `SelfTerminationGuard`, and run_bash scope checks
  still run.
- Deny means deny: no unsandboxed fallback and no global allowlist mutation.
- Grants are scoped by profile and session lifetime, not by arbitrary command
  string alone.

## Test Plan

Unit tests:

- Profile config parsing rejects scalar strings and unknown profile references.
- Command root detection maps `gh ...` to `github`.
- Explicit `sandbox_profile` selects the requested profile.
- Ambiguous multiple matches fail closed.
- Effective sandbox merge preserves base denies and adds profile allow rules.

Permission tests:

- Unapproved profile requirement prompts.
- Once approval applies only to one call.
- TTL approval expires.
- Session approval is available during the same `PermissionContext`.
- New session has no previous profile grants.

Integration tests:

- `gh` profile renders srt settings with GitHub domains and gh config paths.
- `run_bash` foreground and async paths both use merged settings.
- Denied profile does not execute the command.
- Unknown profile produces a clear tool error before subprocess execution.

## Open Questions

- Whether profile grants should imply `exec: workspace` or remain a separate
  requirement shown alongside profile use.
- Whether to provide built-in profile templates for `github`, `python-package`,
  and `node-package`, or keep all profiles user-defined in the first version.
- Whether `/scope` should group active grants by profile for easier scanning.
