#!/usr/bin/env bash
# Orca worktree setup: give a new Loom worktree the same local project context
# as the main tree, by symlinking (never copying) the gitignored files that only
# live in the main tree, plus Claude Code's path-keyed project memory.
#
# Run by Orca from orca.yaml (scripts.setup) with cwd = the new worktree.
# Idempotent: re-running is a no-op. Never overwrites a tracked file or an
# existing real file/dir; it only reports what it skipped.
#
# Override the main tree with LOOM_MAIN_TREE=/path/to/main/checkout.
set -u

WT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "[setup] not inside a git worktree" >&2; exit 1; }
WT="$(cd "$WT" && pwd -P)"
COMMON="$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir)"
MAIN="${LOOM_MAIN_TREE:-$(cd "$(dirname "$COMMON")" && pwd -P)}"

if [ "$WT" = "$MAIN" ]; then
  echo "[setup] running in the main tree ($MAIN); nothing to link"
  exit 0
fi

linked=0; skipped=0

# link <source> <target> <label>
link() {
  local src="$1" dst="$2" label="$3"
  if [ ! -e "$src" ]; then
    echo "[setup] skip $label: missing in main tree ($src)"; return
  fi
  if [ -L "$dst" ]; then
    if [ "$(readlink "$dst")" = "$src" ]; then return; fi
    echo "[setup] skip $label: already a symlink to $(readlink "$dst")"; skipped=$((skipped+1)); return
  fi
  if [ -e "$dst" ]; then
    echo "[setup] skip $label: $dst already exists"; skipped=$((skipped+1)); return
  fi
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst" && linked=$((linked+1)) && echo "[setup] linked $label"
}

# link_repo_path <repo-relative path>: refuses tracked paths.
link_repo_path() {
  local rel="$1"
  if git -C "$WT" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
    echo "[setup] skip $rel: tracked by git"; skipped=$((skipped+1)); return
  fi
  link "$MAIN/$rel" "$WT/$rel" "$rel"
}

# Repo-local context that is gitignored (or excluded) in the main tree.
for rel in CLAUDE.md AGENTS.md .mcp.json .gitnexus \
           .claude/settings.json .claude/settings.local.json .claude/skills; do
  link_repo_path "$rel"
done

# Claude Code project memory is keyed by the absolute path of the project:
# every non-alphanumeric character becomes '-'. Point this worktree's memory
# folder at the main tree's so all workers share one memory.
slug() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }
PROJECTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"
MAIN_MEM="$PROJECTS/$(slug "$MAIN")/memory"
WT_MEM="$PROJECTS/$(slug "$WT")/memory"

if [ -d "$WT_MEM" ] && [ ! -L "$WT_MEM" ] && [ -z "$(ls -A "$WT_MEM")" ]; then
  rmdir "$WT_MEM"   # empty placeholder created by an early session start
fi
link "$MAIN_MEM" "$WT_MEM" "claude memory"

echo "[setup] done: $linked linked, $skipped skipped (main tree: $MAIN)"
