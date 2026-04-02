# Loom

> *The loom is what the harness belongs to. Claude Code is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

**Loom** is a harness-first, memory-native, self-directing agent framework. It wraps any LLM with a structured middleware pipeline, a four-type memory system, a DAG task engine, and an autonomy layer that can trigger, plan, and act without human input.

---

## Core Design Principles

| Principle | Meaning |
|-----------|---------|
| **Harness-first** | Every tool call flows through a middleware pipeline — logging, tracing, and blast-radius control are built in, not bolted on |
| **Memory-native** | Memory is a substrate, not a plugin. Four types (episodic, semantic, procedural, relational) share one SQLite store |
| **Reflexive** | The agent can observe and reason about its own execution history and skill health |
| **Self-directing** | Cron, event, and condition triggers fire autonomously without human prompting |
| **Model-agnostic** | Routes between MiniMax, Anthropic, and OpenAI-compatible providers by model name prefix |

---

## Architecture

```
Platform (CLI)  →  Cognition  →  Harness  →  Memory
                            ↘  Autonomy  →  Notify
                            ↘  Tasks
```

| Layer | What it does |
|-------|-------------|
| **Harness** | `MiddlewarePipeline` with `LogMiddleware`, `TraceMiddleware`, `BlastRadiusMiddleware`; three-tier trust (SAFE / GUARDED / CRITICAL) |
| **Memory** | SQLite WAL; episodic (auto-compressed), semantic (key→value facts), procedural (versioned skills with EMA confidence) |
| **Cognition** | `LLMRouter` (prefix routing), `ContextBudget` (80% compression trigger), `ReflectionAPI` |
| **Tasks** | `TaskGraph` (Kahn's topological sort), `TaskScheduler` (`asyncio.gather` per parallel level) |
| **Autonomy** | `CronTrigger` (5-field cron), `EventTrigger`, `ConditionTrigger`; `ActionPlanner` maps trust level → decision |
| **Notify** | `NotificationRouter` fan-out; `CLINotifier`, `WebhookNotifier`, `TelegramNotifier`; `ConfirmFlow` with timeout |

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
# Interactive agent session (MiniMax-M2.7 by default)
loom chat

# Use a different model
loom chat --model claude-sonnet-4-6

# Inspect memory
loom memory list

# Session reflection
loom reflect --session <session_id>

# Autonomy daemon
loom autonomy start              # foreground daemon
loom autonomy status             # show loaded triggers
loom autonomy emit <event_name>  # manually fire an EventTrigger
```

---

## Configuration (`loom.toml`)

```toml
[loom]
name = "loom"
version = "0.1.0"

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
# All tests (206 total)
python -m pytest tests/

# Single layer
python -m pytest tests/test_harness.py -v
python -m pytest tests/test_memory.py -v
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
| Phase 4 | Learning Layer (Prompt Stack, Memory-as-Attention, Lens System) | 🔄 In progress |
| Phase 5 | Ecosystem (REST API, Discord/Slack, IDE Extension) | ⬜ Planned |

---

## License

MIT
