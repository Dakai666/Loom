# Loom

> *The loom is what the harness belongs to. Claude Code is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

**v0.2** — Textual TUI interface now available (`loom chat --tui`). Basic conversation, tool execution with modal confirmation, workspace panel (Artifacts + Knowledge Graph), and real-time status bar.

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
| **Harness** | `MiddlewarePipeline` with `TraceMiddleware`, `BlastRadiusMiddleware`; three-tier trust (SAFE / GUARDED / CRITICAL) |
| **Memory** | SQLite WAL; episodic (auto-compressed), semantic (key→value + embedding vectors), procedural (versioned skills with EMA confidence) |
| **Cognition** | `LLMRouter` (prefix routing), `ContextBudget` (smart LLM compaction at 80%), `ReflectionAPI`, three-layer `PromptStack` |
| **Tasks** | `TaskGraph` (Kahn's topological sort) + `TaskScheduler` — drives **parallel tool dispatch** in `LoomSession` |
| **Autonomy** | `CronTrigger` (5-field cron), `EventTrigger`, `ConditionTrigger`; `ActionPlanner` maps trust level → decision |
| **Notify** | `NotificationRouter` fan-out; `CLINotifier`, `WebhookNotifier`, `TelegramNotifier`; `ConfirmFlow` with timeout |
| **Extensibility** | `BaseLens` + `HermesLens` / `ClawCodeLens`; Skill Import Pipeline; `@loom.tool` adapter registry |

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

# Textual TUI mode (v0.2+)
loom chat --tui

# Use a specific model
loom chat --model claude-sonnet-4-6
loom chat --tui --model MiniMax-M2.7

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

| Command | Effect |
|---------|--------|
| `/compact` | LLM-summarize oldest conversation turns to free context |
| `/personality <name>` | Switch cognitive persona (adversarial / minimalist / architect / researcher / operator) |
| `/personality off` | Remove active persona |
| `/verbose` | Toggle tool output verbosity |
| `/help` | Show all commands |

### TUI keyboard shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Ctrl+L` | Clear conversation |
| `Ctrl+O` | Toggle tool output verbosity |
| `Ctrl+W` | Toggle Workspace tab (Artifacts ↔ Knowledge) |
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

### Memory tools (agent-callable)

| Tool | Trust | Description |
|------|-------|-------------|
| `recall(query, type, limit)` | SAFE | BM25 + embedding search across semantic facts and skills |
| `memorize(key, value, confidence)` | GUARDED | Persist a fact to long-term semantic memory |

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

Tools requiring interactive confirmation (GUARDED not yet approved, CRITICAL) always run sequentially to avoid interleaved prompts.

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

[harness]
default_trust_level = "guarded"
require_audit_log = true

[autonomy]
enabled = false          # set true to activate daemon
timezone = "Asia/Taipei"

[[autonomy.schedules]]
name = "daily_review"
cron = "0 9 * * 1-5"    # weekdays 09:00
intent = "Review progress and surface priority tasks"
trust_level = "guarded"
notify = true

[notify]
default_channel = "cli"
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
| Phase 5A | Ecosystem foundations (REST API, Discord, memory search, skill eval) | ✅ Complete |
| Phase 5B | Textual TUI (`loom chat --tui`) — dual-space interface, modal confirm | ✅ Complete (v0.2) |
| Phase 5C | Session management (`--resume`, `loom sessions list/show`) | 🔄 Next |

---

## License

MIT
