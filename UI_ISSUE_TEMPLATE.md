# Issue: Loom CLI UI Upgrade — Next-Generation Terminal Interface

**Assignee:** UI Development Agent  
**Branch:** `feature/ui-next`  
**Target:** `master` (PR review by main dev agent)  
**Reference files:** `CLI_UI_DESIGN.md`, `loom/platform/cli/ui.py`, `loom/platform/cli/main.py`

---

## Background

Loom currently has a functional but minimal CLI UI (`loom/platform/cli/ui.py`).
The design research in `CLI_UI_DESIGN.md` identifies concrete improvements inspired by
Claude Code and OpenCode. This issue tracks the incremental UI upgrade.

The main dev agent is concurrently developing core features (memory, autonomy, task engine).
**Your work must not touch core business logic** — only the rendering layer.

---

## Strict Boundary: What You Can and Cannot Touch

### ✅ You OWN these files

| File | Notes |
|------|-------|
| `loom/platform/cli/ui.py` | Full ownership — redesign freely |
| `loom/platform/cli/_run_streaming_turn()` in `main.py` | Only the rendering loop — not the session logic |
| New files under `loom/platform/cli/` | e.g. `components.py`, `theme.py`, `keybindings.py` |

### ❌ Never touch these

| File / Symbol | Why |
|---------------|-----|
| `LoomSession` class | Core business logic, memory, agent loop |
| `stream_turn()` generator | Event contract — changing breaks all consumers |
| `_dispatch_parallel()` | TaskGraph wiring |
| `_smart_compact()` / `_compress_context()` | Context management |
| `_confirm_tool()` | Trust/permission flow |
| `loom/core/` (entire directory) | Framework internals |
| `loom/autonomy/` | Autonomy engine |
| `loom/notify/` | Notification layer |
| `tests/` | Test files |
| `pyproject.toml` | Only add new UI dependencies here after discussion |

---

## The Event Contract (Do Not Break)

`LoomSession.stream_turn()` is an async generator that yields these typed events.
Your rendering code consumes them — **never modify the producer**.

```python
@dataclass
class TextChunk:
    text: str                  # partial LLM text, stream in progress

@dataclass
class ToolBegin:
    name: str                  # tool name
    args: dict                 # tool arguments
    call_id: str               # unique call ID

@dataclass
class ToolEnd:
    name: str
    success: bool
    output: str                # truncated to 200 chars
    duration_ms: float
    call_id: str               # matches the ToolBegin

@dataclass
class TurnDone:
    tool_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
```

The rendering function signature to preserve:
```python
async def _run_streaming_turn(session: "LoomSession", user_input: str) -> None:
    ...
```

---

## Current UI State (Baseline)

```python
# loom/platform/cli/ui.py — current implementation
make_prompt_session()      # PromptSession with InMemoryHistory + SlashCompleter
render_header(model, db)   # Rich Panel header
tool_begin_line(name, args) -> Text   # "  ~> name(args...)"
tool_end_line(name, success, ms) -> Text  # "     ok/!! name  Xms"
```

Current `_run_streaming_turn()` rendering:
```
[Rule]  loom  |  context X%
{TextChunk tokens printed directly with console.print(end="")}
~> tool_name(args...)
   ok/!! tool_name  Xms
[Rule]  context X%  |  Nin/Nout  |  Xs
```

Current slash commands: `/personality`, `/compact`, `/help`

---

## Deliverables

Implement the improvements from `CLI_UI_DESIGN.md` **incrementally**.
Each PR should be one self-contained improvement. Suggested order:

### PR 1 — Streaming cursor + tool state machine
- Replace bare `console.print(end="")` streaming with a cursor indicator (`▌`)
- Tool calls: three visual states — pending → running (spinner) → done (✓/✗)
- Keep the same event consumption loop, just improve rendering

**Acceptance:** streaming text ends with visible cursor; tools show spinner while running

---

### PR 2 — Status bar (bottom of each turn)
Redesign the closing Rule into a richer status line:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 context 45% [▓▓▓▓░░░░░░]  |  1.2k in / 340 out  |  2.3s  |  3 tools
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Color-code context bar: green < 60%, yellow 60–85%, red > 85%.

**Acceptance:** status bar renders after every turn with correct values from `TurnDone`

---

### PR 3 — Tool output verbosity toggle
- Default: collapsed tool output (just name + duration)
- `Ctrl+O` or `/verbose` slash command: expand tool output (show truncated result)
- Store toggle state in a module-level variable in `ui.py`

**Acceptance:** toggle works; verbose mode shows `output` from `ToolEnd`

---

### PR 4 — Keyboard shortcuts
Add to `make_prompt_session()`:

| Key | Action |
|-----|--------|
| `Ctrl+L` | Clear screen, reprint header |
| `Ctrl+C` (during input) | Cancel input, print `[cancelled]` |
| `Ctrl+O` | Toggle tool output verbosity |

Use `prompt_toolkit` key bindings (`KeyBindings` class).

**Acceptance:** all three shortcuts work without breaking existing Tab/history

---

### PR 5 — Observability panel (optional, advanced)
After `TurnDone`, print a compact observability summary:

```
  tools: read_file(45ms ✓)  list_dir(12ms ✓)  recall(8ms ✓)   ← parallel batch
  memory: 128 facts  |  session: 2.3k tokens used
```

Pull tool timing from `ToolEnd.duration_ms`. Parallel batches (multiple tools between
one `TextChunk` sequence) should show on one line.

**Acceptance:** panel appears only when ≥2 tools were used in the turn

---

## Technical Constraints

### Windows cp950 compatibility
The target terminal may use Traditional Chinese Windows (cp950 codepage).
These Unicode characters are **known to fail** — do not use them:

| Character | Description |
|-----------|-------------|
| `⚙` U+2699 | Gear |
| `✓` U+2713 | Check mark |
| `✗` U+2717 | Cross mark |
| `•` U+2022 | Bullet |
| `→` U+2192 | Arrow |
| `⚠` U+26A0 | Warning |

**Safe alternatives already in use:** `ok`, `!!`, `~>`, `[compress]`

For the spinner, use ASCII frames: `[- ]`, `[\ ]`, `[| ]`, `[/ ]`  
or Rich's built-in spinner (it degrades gracefully).

### No Rich Live for interactive flows
`Rich Live` conflicts with `prompt_toolkit` input on Windows — its background
render thread blocks stdin. Do not use `Live` in any path that might trigger
a confirmation prompt (`_confirm_tool`).

Rich `Live` is acceptable for pure display (non-interactive turns) but must be
exited before any `prompt_toolkit` call.

### Dependencies
Currently used: `rich`, `prompt_toolkit`  
If you need a new dependency, note it clearly in the PR description.
The main dev agent will decide whether to add it to `pyproject.toml`.

---

## PR Requirements

Every PR must:

1. **Pass all existing tests** — `python -m pytest tests/ -q` must show green
2. **Not import from `loom/core/`** in `ui.py` (no circular dependencies)
3. **Not modify `LoomSession`** or any method listed in the "Never touch" table
4. **Include a brief test** if adding new functions to `ui.py` (pure rendering helpers are exempt)
5. **Target branch:** `feature/ui-next` → PR into `master`

---

## How to Start

```bash
# 1. Clone and set up
git clone https://github.com/Dakai666/Loom.git
cd Loom
pip install -e ".[dev]"

# 2. Create feature branch
git checkout -b feature/ui-next

# 3. Read the design spec
# Open CLI_UI_DESIGN.md — this is your primary design reference

# 4. Run existing tests to establish baseline
python -m pytest tests/ -q

# 5. Start with PR 1 (streaming cursor + tool state machine)
# Edit: loom/platform/cli/ui.py
#       loom/platform/cli/main.py (_run_streaming_turn only)

# 6. Open PR targeting master when ready
gh pr create --title "UI: streaming cursor + tool state machine" \
  --body "Implements PR 1 from UI_ISSUE_TEMPLATE.md"
```

---

## Questions / Blockers

If you are unsure whether a change is within scope, err on the side of **smaller**.
Add a comment in your PR description explaining the trade-off.
The main dev agent will review and guide on anything ambiguous.
