# Loom

> *The loom is what the harness belongs to. Claude is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

**v0.2.5.1** — TUI v2 + cognition upgrades: Command Palette (Cmd+K), Sidebar Toggle (Cmd+B), Swarm Dashboard replacing ActivityLog, Time-Travel conversation map with session forking, Interactive Micro-Widgets, Native graphic rendering via rich-pixels; `/auto` command for session exec auto-approve (respects workspace escape detection); `AbortController` infra in `loom.core.infra` (Issue #16 Step 1) — standard cancellation signal, zero-closure `bind()` pattern; Counter-factual reflection — `execution_error` failures on SkillGenome-tracked tools trigger async LLM reflection writing anti-patterns to SemanticMemory + RelationalMemory (`loom-self/should_avoid:<tool>`); `MemoryIndex` now surfaces anti-pattern count at session start.

**v0.2.4.0** — Security fixes + sqlite-vec memory upgrade: path traversal closed (`_resolve_workspace_path` now enforces `relative_to(workspace)` for all path types); prompt injection guardrails (`fetch_url`/`web_search` output wrapped in `<untrusted_external_content>` with XML chars escaped); `sqlite-vec` integration pushes cosine vector ranking into SQL (`vec_distance_cosine`), replacing Python-side loop; `SQLiteStore.connect()` auto-loads the extension; `LoomSession` stores `_db_ctx` for correct async context manager teardown.

**v0.2.3.4** — `ToolCapability` flags for fine-grained tool risk classification; `run_bash` and `spawn_agent` now always re-confirm (never session-pre-authorized); `strict_sandbox` config option confines shell execution to the workspace; architecture guardrail tests enforce module import boundaries; Skill Genome early-death protection (`MIN_SAMPLES_BEFORE_DEPRECATION`).

**v0.2.3.3** — Discord + Autonomy merged process (`--autonomy` flag), per-task Discord thread routing (`notify_thread`), configurable episodic compression threshold, memory compression notification in-thread, graceful shutdown with session compression.

**Loom** is a harness-first, memory-native, self-directing agent framework. It wraps any LLM with a structured middleware pipeline, a four-type memory system (with vector search), a DAG task engine for parallel tool execution, and an autonomy layer that can trigger, plan, and act without human input.

---

## Core Design Principles

| Principle | Meaning |
|-----------|---------|
| **Harness-first** | Every tool call flows through a middleware pipeline — logging, tracing, and blast-radius control are built in, not bolted on |
| **Memory-native** | Memory is a substrate, not a plugin. Four types (episodic, semantic, procedural, relational) share one SQLite store with vector search |
| **Reflexive** | The agent can observe and reason about its own execution history and skill health |
| **Self-directing** | Cron, event, and condition triggers fire autonomously without human prompting |
| **Model-agnostic** | Routes between MiniMax, Anthropic, and OpenAI-compatible providers by model name prefix |

---

## Architecture

```
Platform (CLI)  →  Cognition  →  Harness  →  Memory
                            ↘  Autonomy  →  Notify
                            ↘  Tasks (parallel dispatch)
                            ↘  Extensibility (Lens system)
```

| Layer | What it does |
|-------|-------------|
| **Harness** | `MiddlewarePipeline` with `TraceMiddleware`, `BlastRadiusMiddleware`; three-tier trust (SAFE / GUARDED / CRITICAL) + `ToolCapability` flags (EXEC / NETWORK / AGENT_SPAN / MUTATES) |
| **Memory** | SQLite WAL; episodic (auto-compressed), semantic (key→value + embedding vectors), procedural (versioned skills with EMA confidence) |
| **Cognition** | `LLMRouter` (prefix routing), `ContextBudget` (smart LLM compaction at 80%), `ReflectionAPI`, three-layer `PromptStack` |
| **Tasks** | `TaskGraph` (Kahn's topological sort) + `TaskScheduler` — drives **parallel tool dispatch** in `LoomSession` |
| **Autonomy** | `CronTrigger` (5-field cron), `EventTrigger`, `ConditionTrigger`; `ActionPlanner` maps trust level → decision |
| **Notify** | `NotificationRouter` fan-out; `CLINotifier`, `WebhookNotifier`, `TelegramNotifier`, `DiscordBotNotifier`; `ConfirmFlow` with timeout |
| **Extensibility** | `LoomPlugin` unified plugin interface; `HermesLens` / `OpenAIToolsLens`; Skill Import Pipeline; `@loom.tool` + `loom.register_plugin()` |

---

## Installation

```bash
# Requires Python 3.11+
git clone https://github.com/Dakai666/Loom.git
cd Loom

pip install -e ".[dev]"
```

Create a `.env` file in the project root:

```env
MINIMAX_API_KEY=your_minimax_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here   # optional
```

---

## Quick Start

```bash
# Classic CLI mode
loom chat

# TUI mode — auto-resumes last session (v0.2+)
loom chat --tui

# Session management (v0.2.1+)
loom chat --resume                   # resume most recent session (CLI)
loom chat --session <id>             # resume specific session
loom sessions list                   # list recent sessions
loom sessions show <id>              # show session details
loom sessions rm <id>                # delete a session

# Use a specific model
loom chat --model claude-sonnet-4-6
loom chat --tui --model MiniMax-M2.7

# Discord bot (v0.2.3.1+) — requires: pip install loom[discord]
loom discord start --token $DISCORD_BOT_TOKEN --channel <channel_id>

# Discord bot + autonomy daemon in one process (v0.2.3.3+)
loom discord start --autonomy --channel <channel_id>
loom discord start --autonomy --channel <channel_id> --notify-channel <notify_id>

# Inspect memory
loom memory list

# Session reflection
loom reflect --session <session_id>

# Autonomy daemon
loom autonomy start              # foreground daemon
loom autonomy status             # show loaded triggers
loom autonomy emit <event_name>  # manually fire an EventTrigger
```

### In-session slash commands

Available in **CLI**, **TUI**, and **Discord** — all three frontends have full command parity.

| Command | Effect |
|---------|--------|
| `/think` | Show the full reasoning chain (`<think>…</think>`) from the last turn |
| `/compact` | LLM-summarize oldest conversation turns to free context |
| `/sessions` | Browse and switch sessions |
| `/personality <name>` | Switch cognitive persona (adversarial / minimalist / architect / researcher / operator) |
| `/personality off` | Remove active persona |
| `/auto` | Toggle `run_bash` session auto-approve — requires `strict_sandbox = true`; absolute paths escaping the workspace still re-confirm |
| `/pause` | Toggle HITL mode — agent pauses after each tool batch, awaiting your input |
| `/stop` | **Immediately** cancel the current running turn (no waiting for a boundary) |
| `/budget` | Show context token usage (Discord only; TUI has the Budget panel) |
| `/new` | Start a fresh session |
| `/help` | Show all commands |

**HITL pause flow** — when `/pause` mode is on, after every tool batch the agent suspends and prompts:
- `r` / Enter — resume as-is
- `c` — cancel the rest of this turn
- any text — inject as a redirect message and resume

### TUI keyboard shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Same as `/stop` — immediately interrupt current generation |
| `F1` | Toggle verbose tool output |
| `F2` | Cycle Workspace tab (Artifacts → Activity → Budget) |
| `Ctrl+L` | Clear conversation view |
| `Ctrl+C` | Quit |
| `Tab` | Autocomplete slash commands |
| `Y` / `N` | Approve / deny tool confirmation dialogs |

---

## Memory System

Loom uses a **multi-fallback recall chain** for language-agnostic retrieval:

```
recall(query)
  ├─ Tier 1: Embedding (cosine similarity, MiniMax embo-01) — language-agnostic
  ├─ Tier 2: BM25 keyword ranking                           — same-language fast path
  └─ Tier 3: Recency fallback                               — always returns something
```

Embeddings are computed at write-time (`upsert`) and stored in SQLite. Failures fall through silently to BM25.

### Built-in tools

| Tool | Trust | Capabilities | Description |
|------|-------|-------------|-------------|
| `read_file` | SAFE | — | Read a file inside the workspace |
| `list_dir` | SAFE | — | List directory contents |
| `recall` | SAFE | — | BM25 + embedding search across semantic facts and skills |
| `query_relations` | SAFE | — | Query relational memory triples |
| `fetch_url` | SAFE | NETWORK | Fetch a URL, strip HTML noise, return title + body |
| `write_file` | GUARDED | MUTATES | Write a file (path always confined to workspace) |
| `memorize` | GUARDED | MUTATES | Persist a fact to long-term semantic memory |
| `relate` | GUARDED | MUTATES | Store a relationship triple in relational memory |
| `web_search` | GUARDED | NETWORK | Brave Search API top-N results |
| `run_bash` | GUARDED | **EXEC** | Execute a shell command — **re-confirms every call** |
| `spawn_agent` | GUARDED | **AGENT_SPAN** + MUTATES | Spawn a sub-agent — **re-confirms every call** |

> **EXEC and AGENT_SPAN** tools never receive session-level pre-authorization. Each call triggers a fresh confirmation prompt, matching CRITICAL semantics even at GUARDED trust level.

---

## Parallel Tool Execution

When the LLM requests multiple tools simultaneously, Loom runs them concurrently via `TaskGraph`:

```
LLM response: [read_file, list_dir, recall]  ← all SAFE / pre-authorized
  │
  └─ TaskGraph: one level, asyncio.gather
       ├─ read_file   → result A
       ├─ list_dir    → result B
       └─ recall      → result C
```

Tools requiring interactive confirmation (GUARDED not yet approved, CRITICAL, or EXEC/AGENT_SPAN capability) always run sequentially to avoid interleaved prompts.

---

## Context Management

- **Auto-compact** at 80% context usage (checked at turn start and after each tool loop)
- **Smart compact** (`_smart_compact`): LLM summarizes oldest ½ of conversation into a 2-message summary pair — preserves semantic content, not just truncates
- **Fallback**: safe turn-boundary drop if LLM summary fails
- **Manual**: `/compact` slash command

---

## Configuration (`loom.toml`)

```toml
[loom]
name = "loom"
version = "0.1.0"

[identity]
soul        = "SOUL.md"
agent       = "Agent.md"
personality = "personalities/adversarial.md"   # optional default

[cognition]
default_model = "MiniMax-M2.7"
max_tokens = 8096

[memory]
backend = "sqlite"
db_path = "~/.loom/memory.db"
episodic_retention_days = 7
skill_deprecation_threshold = 0.3
episodic_compress_threshold = 10   # compress to semantic after N episodic entries

[harness]
default_trust_level = "guarded"
require_audit_log   = true

# Confine run_bash to workspace root (cwd=workspace).
# File I/O tools always enforce workspace boundaries regardless.
# Enable this together with /auto for the recommended balanced workflow:
# run_bash auto-approved for workspace-relative commands, but still
# re-confirmed when an absolute path escaping the workspace is detected.
strict_sandbox = false

[autonomy]
enabled = true
timezone = "Asia/Taipei"

[[autonomy.schedules]]
name = "morning_briefing"
cron = "0 9 * * *"
intent = "Generate daily news briefing and write to news/YYYY-MM-DD/briefing.md"
trust_level = "safe"     # safe = auto-execute, no confirmation needed
notify = false
notify_thread = 0        # Discord thread ID for results (0 = default notify channel)

[[autonomy.schedules]]
name = "daily_journal"
cron = "30 17 * * *"
intent = "Write today's journal: work done, obstacles, decisions"
trust_level = "safe"
notify = false
notify_thread = 0        # separate thread for journal entries

# trust_level + notify interaction:
#   safe                  → execute immediately, no confirmation
#   guarded + notify=false → execute immediately, no confirmation
#   guarded + notify=true  → Discord Allow/Deny buttons (60s timeout)
#   critical               → must confirm every time

[notify]
default_channel = "cli"
```

---

## Discord Bot

The Discord bot turns any Discord channel into a full Loom frontend — useful for mobile access and 24/7 availability.

### Setup

```bash
pip install 'loom[discord]'
```

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents
3. Add to `.env`:

```env
DISCORD_BOT_TOKEN=your-bot-token-here
DISCORD_CHANNEL_ID=123456789   # optional: restrict to one channel
```

```bash
# Bot only
loom discord start --token $DISCORD_BOT_TOKEN --channel <channel_id>

# Bot + autonomy daemon (recommended — one process)
loom discord start --autonomy --channel <channel_id>
```

### Usage

Send a message in the allowed channel to start a new thread, or continue an existing one. Each thread is a persistent session — context is restored automatically after a bot restart (`~/.loom/discord_threads.json`).

**Turn flow:**
1. `⚙️` reaction on your message — acknowledged
2. *Bot is typing…* indicator — working
3. Tool activity log message (if any tools ran): `⟳ name — "arg" ✓ Xms` per tool
4. Response as a fresh message — full Markdown rendering, URL embeds stable
5. `⚙️` → `✅` — done

All slash commands work in Discord chat: `/new` `/sessions` `/think` `/compact` `/personality` `/pause` `/stop` `/budget` `/help`

**Tool confirmation** — GUARDED/CRITICAL tools trigger a message with Allow / Deny buttons (60s timeout → auto-deny).

**HITL pause** — `/pause` on: after each tool batch the bot posts a pause prompt; reply `r` / `c` / redirect text.

**`/stop`** — cancels the running turn immediately; partial response is sent as a new message.

**Memory compression** — when episodic entries hit the threshold, the thread receives a small status line:
```
🧠 記憶壓縮：5 條事實已存入語意記憶
```

---

## Extensibility

### Plugin System

Loom can extend itself at runtime through a unified plugin interface.  Plugins live in `~/.loom/plugins/` — Loom (or you) can drop files there and they are loaded automatically on the next session start.

**First run:** a new plugin file triggers a GUARDED confirmation prompt. Approval is stored in relational memory so future sessions load it silently.

**Simple tool plugin** — just drop a `.py` file:

```python
# ~/.loom/plugins/my_tools.py
import loom

@loom.tool(trust_level="safe", description="Query our internal API")
async def query_internal_api(call):
    url = call.args["url"]
    # ... your implementation
```

**Full plugin class** — for tools + middleware + lenses together:

```python
# ~/.loom/plugins/git_tools.py
import loom
from loom.extensibility import LoomPlugin
from loom.core.harness.registry import ToolDefinition
from loom.core.harness.permissions import TrustLevel

class GitPlugin(LoomPlugin):
    name = "git"
    version = "1.0"

    def tools(self):
        return [git_status_tool, git_diff_tool]   # ToolDefinition instances

    def middleware(self):
        return [GitSafetyMiddleware()]             # Middleware subclass instances

loom.register_plugin(GitPlugin())
```

| Extension point | Method |
|----------------|--------|
| Tools | `tools() -> list[ToolDefinition]` |
| Middleware | `middleware() -> list[Middleware]` |
| Lenses | `lenses() -> list[BaseLens]` |
| Notifiers | `notifiers() -> list[BaseNotifier]` |
| Lifecycle hooks | `on_session_start(session)` / `on_session_stop(session)` |

### Importing external skills and tools

```bash
# Import skills from a Hermes-format JSON
loom import skills.json

# Import OpenAI-compatible tool definitions
loom import tools.json --lens openai_tools

# Preview without writing
loom import skills.json --dry-run --min-confidence 0.7
```

---

## Running Tests

```bash
# All tests (371 total)
python -m pytest tests/

# Single layer
python -m pytest tests/test_harness.py -v
python -m pytest tests/test_memory.py -v
python -m pytest tests/test_memory_search.py -v
python -m pytest tests/test_cognition.py -v
python -m pytest tests/test_tasks.py -v
python -m pytest tests/test_autonomy.py -v
python -m pytest tests/test_integration.py -v
```

---

## Project Status

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Harness + Memory | ✅ Complete |
| Phase 2 | Cognition + Tasks | ✅ Complete |
| Phase 3 | Autonomy + Notify | ✅ Complete |
| Phase 4 | Learning Layer (Prompt Stack, Memory-as-Attention, Lens System) | ✅ Complete |
| Phase 4.5 | CLI Platform maturity (streaming, smart compact, parallel tools) | ✅ Complete |
| Phase 4C | Extensibility: Lens system, Skill Import Pipeline, `@loom.tool` | ✅ Complete |
| Phase 4D | Plugin system: `LoomPlugin`, `~/.loom/plugins/` auto-scan, approval gate | ✅ Complete |
| Phase 5A | Ecosystem foundations (REST API, Discord, memory search, skill eval) | ✅ Complete |
| Phase 5B | Textual TUI (`loom chat --tui`) — dual-space interface, modal confirm | ✅ Complete (v0.2) |
| Phase 5C | Session management (`--resume`, `/sessions` picker, TUI auto-resume) | ✅ Complete (v0.2.1) |
| Phase 5D | Web tools (`fetch_url`, `web_search`) + workspace sandbox | ✅ Complete (v0.2.1) |
| Phase 5D+ | Memory: timestamps in recall, periodic compression, datetime context | ✅ Complete (v0.2.1) |
| Phase 5E | Sub-agent (`spawn_agent`) — isolated child LoomSession with trust inheritance | ✅ Complete |
| Phase 5F | Architecture hardening: failure taxonomy, confidence decay, memory provenance | ✅ Complete |
| Phase UI | `<think>` reasoning collapse, `/think` command, streaming cursor improvements | ✅ Complete |
| Phase TUI-2 | TUI overhaul: Parchment theme, AgentState indicator, Markdown rendering, Budget panel, Activity Log, HelpModal, IDE-safe keys | ✅ Complete (v0.2.3) |
| Phase 5G | HITL pause/resume/redirect (`/pause`, `/stop`); Discord bot frontend; three-frontend command parity | ✅ Complete (v0.2.3.1) |
| Phase 5H | Action visibility (tool events inline in conversation); Discord display overhaul (reactions, typing indicator, split send, session persistence) | ✅ Complete (v0.2.3.2) |
| Phase 5I | Discord + Autonomy merged process (`--autonomy`); per-task thread routing (`notify_thread`); configurable episodic compress threshold; graceful shutdown with compression; `CompressDone` in-thread notification | ✅ Complete (v0.2.3.3) |
| Phase 5J | `ToolCapability` flags (EXEC / NETWORK / AGENT_SPAN / MUTATES); EXEC+AGENT_SPAN always re-confirm; `strict_sandbox` workspace confinement; architecture guardrail tests; Skill Genome early-death protection | ✅ Complete (v0.2.3.4) |

---

## License

MIT
