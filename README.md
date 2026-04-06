# Loom

> *The loom is what the harness belongs to. Claude is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

**Loom** is a harness-first, memory-native, self-directing agent framework. It wraps any LLM with a structured middleware pipeline, a four-type memory system (with vector search), a DAG task engine for parallel tool execution, and an autonomy layer that can trigger, plan, and act without human input.

---

## Core Design Principles

| Principle | Meaning |
|-----------|---------|
| **Harness-first** | Every tool call flows through a middleware pipeline — logging, tracing, and blast-radius control are built in, not bolted on |
| **Memory-native** | Memory is a substrate, not a plugin. Four types (episodic, semantic, procedural, relational) share one SQLite store with vector search |
| **Reflexive** | The agent can observe and reason about its own execution history and skill health |
| **Self-directing** | Cron, event, and condition triggers fire autonomously without human prompting |
| **Model-agnostic** | Routes between cloud and local providers by model name prefix; switch mid-session with `/model` |

---

## Architecture

```
Platform (CLI / TUI / Discord)  →  Cognition  →  Harness  →  Memory
                                             ↘  Autonomy  →  Notify
                                             ↘  Tasks (parallel dispatch)
                                             ↘  Extensibility (Lens / Plugin system)
```

| Layer | What it does |
|-------|-------------|
| **Harness** | `MiddlewarePipeline` with `TraceMiddleware`, `BlastRadiusMiddleware`; three-tier trust (SAFE / GUARDED / CRITICAL) + `ToolCapability` flags (EXEC / NETWORK / AGENT_SPAN / MUTATES) |
| **Memory** | SQLite WAL + sqlite-vec; episodic (auto-compressed), semantic (key→value + embedding vectors), procedural (versioned skills with EMA confidence) |
| **Cognition** | `LLMRouter` (prefix routing, runtime switching), `ContextBudget` (LLM compaction at 80%), `ReflectionAPI`, three-layer `PromptStack` |
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

Create a `.env` file in the project root with at least one provider credential:

```env
# Cloud providers (at least one required, unless using a local provider)
MINIMAX_API_KEY=your_minimax_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
api_key_env=your_embeddings_key_here

# Local providers — set base URL to enable (or use loom.toml, see below)
OLLAMA_BASE_URL=http://localhost:11434/v1
LMSTUDIO_BASE_URL=http://localhost:1234/v1
```

---

## Quick Start

```bash
# Classic CLI mode
loom chat

# TUI mode (Textual dual-pane interface)
loom chat --tui

# Start with a specific model
loom chat --model claude-sonnet-4-6
loom chat --model ollama/llama3.2

# Session management
loom chat --resume                   # resume most recent session
loom chat --session <id>             # resume specific session
loom sessions list
loom sessions show <id>
loom sessions rm <id>

# Discord bot — requires: pip install loom[discord]
loom discord start --token $DISCORD_BOT_TOKEN --channel <channel_id>

# Discord bot + autonomy daemon in one process (recommended)
loom discord start --autonomy --channel <channel_id>
loom discord start --autonomy --channel <channel_id> --notify-channel <notify_id>

# Autonomy daemon (standalone)
loom autonomy start
loom autonomy status
loom autonomy emit <event_name>

# Memory and reflection
loom memory list
loom reflect --session <session_id>
```

### In-session slash commands

Available in **CLI**, **TUI**, and **Discord** — all three frontends have full command parity.

| Command | Effect |
|---------|--------|
| `/model` | Show current model and registered providers |
| `/model <name>` | Switch LLM provider/model mid-session (see [Model Switching](#model-switching)) |
| `/new` | Start a fresh session |
| `/sessions` | Browse and switch sessions |
| `/personality <name>` | Switch cognitive persona |
| `/personality off` | Remove active persona |
| `/think` | Show the full reasoning chain from the last turn |
| `/compact` | LLM-summarize oldest turns to free context |
| `/auto` | Toggle `run_bash` session auto-approve (requires `strict_sandbox = true`) |
| `/pause` | Toggle HITL mode — agent pauses after each tool batch |
| `/stop` | Immediately cancel the current running turn |
| `/budget` | Show context token usage (Discord; TUI has the Budget panel) |
| `/help` | Show all commands |

**HITL pause flow** — when `/pause` is on, after every tool batch the agent suspends and prompts:
- `r` / Enter — resume as-is
- `c` — cancel the rest of this turn
- any text — inject as a redirect message and resume

### TUI keyboard shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Interrupt current generation (`/stop`) |
| `F1` / `Ctrl+K` | Command Palette — fuzzy search all actions |
| `F2` | Cycle Workspace tab (Artifacts → Activity → Budget) |
| `F3` | Toggle verbose tool output |
| `F4` / `Ctrl+B` | Toggle right sidebar |
| `Ctrl+L` | Clear conversation view |
| `Ctrl+C` | Quit |
| `Tab` | Autocomplete slash commands |
| `Y` / `N` | Approve / deny tool confirmation dialogs |

---

## Model Switching

Loom routes by model name prefix. All providers registered at startup; switch any time with `/model`.

| Prefix | Provider | Credential |
|--------|----------|------------|
| `MiniMax-*` | MiniMax (minimax.io) | `MINIMAX_API_KEY` in `.env` |
| `claude-*` | Anthropic | `ANTHROPIC_API_KEY` in `.env` |
| `ollama/<name>` | Local Ollama server | `[providers.ollama]` in `loom.toml` or `OLLAMA_BASE_URL` |
| `lmstudio/<name>` | Local LM Studio | `[providers.lmstudio]` in `loom.toml` or `LMSTUDIO_BASE_URL` |

```
/model                        # show current model + all registered providers
/model claude-sonnet-4-6      # switch to Anthropic mid-session
/model ollama/qwen2.5-coder:7b
/model lmstudio/phi-4
/model MiniMax-M2.7-highspeed
```

Set the session default in `loom.toml`:

```toml
[cognition]
default_model = "ollama/llama3.2"   # used when no --model flag is given
```

---

## Memory System

Loom uses a **multi-fallback recall chain** for language-agnostic retrieval:

```
recall(query)
  ├─ Tier 1: Embedding (cosine similarity via sqlite-vec) — language-agnostic
  ├─ Tier 2: BM25 keyword ranking (SQLite FTS5)          — same-language fast path
  └─ Tier 3: Recency fallback                            — always returns something
```

Embeddings are computed at write-time (`upsert`) and stored in SQLite. Failures fall through silently to BM25.

**Semantic memory** entries carry a confidence score that decays over time (90-day half-life). The `DreamingPlugin` registers a `memory_prune` tool that removes entries whose effective confidence has fallen below a threshold.

### Built-in tools

| Tool | Trust | Capabilities | Description |
|------|-------|-------------|-------------|
| `read_file` | SAFE | — | Read a file inside the workspace |
| `list_dir` | SAFE | — | List directory contents |
| `recall` | SAFE | — | BM25 + embedding search across semantic facts and skills |
| `query_relations` | SAFE | — | Query relational memory triples |
| `fetch_url` | SAFE | NETWORK | Fetch a URL; output wrapped in `<untrusted_external_content>` |
| `write_file` | GUARDED | MUTATES | Write a file (path confined to workspace) |
| `memorize` | GUARDED | MUTATES | Persist a fact to long-term semantic memory |
| `relate` | GUARDED | MUTATES | Store a relationship triple in relational memory |
| `web_search` | GUARDED | NETWORK | Brave Search API top-N results |
| `run_bash` | GUARDED | **EXEC** | Execute a shell command — **re-confirms every call** |
| `spawn_agent` | GUARDED | **AGENT_SPAN** + MUTATES | Spawn a sub-agent — **re-confirms every call** |

> **EXEC and AGENT_SPAN** tools never receive session-level pre-authorization — each call triggers a fresh confirmation, matching CRITICAL semantics regardless of trust level.

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

Tools requiring interactive confirmation (GUARDED not yet approved, CRITICAL, EXEC, or AGENT_SPAN) always run sequentially to avoid interleaved prompts.

---

## Context Management

- **Auto-compact** at 80% context usage (checked at turn start and after each tool loop)
- **Smart compact**: LLM summarizes oldest ½ of conversation into a summary pair — preserves semantic content, does not just truncate
- **Fallback**: turn-boundary drop if LLM summary fails or < 3 user turns exist
- **Manual**: `/compact` slash command

---

## Configuration (`loom.toml`)

```toml
[loom]
name    = "loom"
version = "0.1.0"

[identity]
soul        = "SOUL.md"
agent       = "Agent.md"
personality = ""   # optional: "personalities/adversarial.md"

[cognition]
default_model = "claude-sonnet-4-6"   # supports all routing prefixes
max_tokens    = 8096

[memory]
backend                     = "sqlite"
db_path                     = "~/.loom/memory.db"
episodic_retention_days     = 7
skill_deprecation_threshold = 0.3
episodic_compress_threshold = 10

[harness]
default_trust_level = "guarded"
require_audit_log   = true
strict_sandbox      = false   # set true to confine run_bash to workspace root

# ── Local providers (no API key required) ─────────────────────────────────────
# [providers.ollama]
# enabled       = true
# base_url      = "http://localhost:11434/v1"   # default
# default_model = "llama3.2"

# [providers.lmstudio]
# enabled       = true
# base_url      = "http://localhost:1234/v1"    # default
# default_model = "phi-4"

# ── Autonomy ──────────────────────────────────────────────────────────────────
[autonomy]
enabled  = true
timezone = "Asia/Taipei"

[[autonomy.schedules]]
name        = "morning_briefing"
cron        = "0 9 * * *"
intent      = "Generate daily news briefing and write to news/YYYY-MM-DD/briefing.md"
trust_level = "safe"
notify      = false

# trust_level × notify interaction:
#   safe                   → execute immediately, no confirmation
#   guarded + notify=false → execute immediately, no confirmation
#   guarded + notify=true  → Discord Allow/Deny buttons (60s timeout → deny)
#   critical               → must confirm every time

[notify]
default_channel = "cli"
```

---

## Discord Bot

The Discord bot turns any channel into a full Loom frontend — useful for mobile access and 24/7 availability.

```bash
pip install 'loom[discord]'
```

1. Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents
3. Add to `.env`: `DISCORD_BOT_TOKEN=your-bot-token-here`

```bash
# Bot only
loom discord start --token $DISCORD_BOT_TOKEN --channel <channel_id>

# Bot + autonomy daemon (recommended — one process)
loom discord start --autonomy --channel <channel_id>
```

Each channel thread is a persistent session — context is restored automatically after a bot restart.

**Turn flow:** `⚙️` reaction (acknowledged) → typing indicator → tool activity log → response message → `✅`

All slash commands work in Discord: `/new` `/sessions` `/model` `/think` `/compact` `/personality` `/pause` `/stop` `/budget` `/help`

---

## Extensibility

### Plugin System

Plugins live in `~/.loom/plugins/` and are loaded automatically on session start. The first load of a new file triggers a GUARDED confirmation; approval is stored in relational memory.

```python
# ~/.loom/plugins/my_tools.py
import loom

@loom.tool(trust_level="safe", description="Query internal API")
async def query_internal_api(call):
    ...
```

**Full plugin class** — tools + middleware + lenses:

```python
from loom.extensibility import LoomPlugin
import loom

class GitPlugin(LoomPlugin):
    name = "git"
    version = "1.0"

    def tools(self):      return [git_status_tool, git_diff_tool]
    def middleware(self): return [GitSafetyMiddleware()]

loom.register_plugin(GitPlugin())
```

| Extension point | Method |
|----------------|--------|
| Tools | `tools() -> list[ToolDefinition]` |
| Middleware | `middleware() -> list[Middleware]` |
| Lenses | `lenses() -> list[BaseLens]` |
| Notifiers | `notifiers() -> list[BaseNotifier]` |
| Lifecycle hooks | `on_session_start(session)` / `on_session_stop(session)` |

### Importing external skills

```bash
loom import skills.json                          # Hermes format
loom import tools.json --lens openai_tools
loom import skills.json --dry-run --min-confidence 0.7
```

---

## Running Tests

```bash
python -m pytest tests/          # 374 tests
python -m pytest tests/test_harness.py -v
python -m pytest tests/test_memory.py -v
python -m pytest tests/test_cognition.py -v
python -m pytest tests/test_autonomy.py -v
python -m pytest tests/test_integration.py -v
```

---

## License

MIT
