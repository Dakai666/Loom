# Loom

> *The loom is what the harness belongs to. Claude is one thread; Loom is the machine that weaves any thread into the same quality fabric.*

**Loom** is a harness-first, memory-native, self-directing agent framework. It wraps any LLM with a structured middleware pipeline, a four-type memory system (with vector search), a DAG task engine for parallel tool execution, and an autonomy layer that can trigger, plan, and act without human input.

---

## Core Design Principles

| Principle | Meaning |
|-----------|---------|
| **Harness-first** | Every tool call flows through a middleware pipeline — logging, tracing, and blast-radius control are built in, not bolted on |
| **Memory-native** | Memory is a substrate, not a plugin. Four types (episodic, semantic, procedural, relational) share one SQLite store with vector search |
| **Reflexive** | The agent observes its own execution history, self-assesses skill quality after each turn, and evolves low-performing skills automatically |
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
| **Harness** | `MiddlewarePipeline` with `TraceMiddleware`, `BlastRadiusMiddleware` (scope-aware + legacy fallback), `LifecycleMiddleware` + `LifecycleGateMiddleware`; three-tier trust (SAFE / GUARDED / CRITICAL) + `ToolCapability` flags (EXEC / NETWORK / AGENT_SPAN / MUTATES); **scope-aware permission substrate** — resource-level authorization with automatic grant management; full Action Lifecycle state machine with real-time control gates |
| **Memory** | SQLite WAL + sqlite-vec; episodic (auto-compressed), semantic (key→value + embedding vectors + trust-tier governance), procedural (versioned skills with quality-gradient EMA + self-assessment); always-on `MemoryGovernor` (contradiction detection, admission gate, decay cycle) |
| **Cognition** | `LLMRouter` (prefix routing, runtime switching), `ContextBudget` (LLM compaction at 80%), `ReflectionAPI`, three-layer `PromptStack` |
| **Tasks** | `TaskGraph` (Kahn's topological sort) + `TaskScheduler` — drives **parallel tool dispatch** in `LoomSession` |
| **Autonomy** | `CronTrigger` (5-field cron), `EventTrigger`, `ConditionTrigger`; `ActionPlanner` maps trust level → decision; unified pipeline with `origin`-aware authorization (`allowed_tools` + `scope_grants` per schedule) |
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
| `/auto` | Toggle `run_bash` auto-approve — injects a workspace-scoped exec grant; absolute-path commands still require confirmation |
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

**Semantic memory** entries carry a confidence score that decays over time (90-day half-life). All writes pass through the **Memory Governance** layer:

```
write request
  ├─ classify_source()  → trust tier (user_explicit 1.0 → external 0.5)
  ├─ confidence floor   = max(entry.confidence, tier_confidence)
  ├─ ContradictionDetector — REPLACE / KEEP / SUPERSEDE by trust tier
  └─ SemanticMemory.upsert() or skip

session.stop()
  └─ Decay Cycle — episodic TTL prune + semantic low-confidence prune + relational dreaming decay
```

**Trust tiers** (highest → lowest): `user_explicit` (1.0) → `tool_verified` (0.9) → `agent_memorize` (0.85) → `session_compress` (0.8) → `counter_factual` (0.75) → `agent_inferred` (0.7) → `skill_evolution` (0.65) → `dreaming` (0.6) → `external/unknown` (0.5)

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
| `run_bash` | GUARDED | **EXEC** | Execute a shell command — scope-aware: auto-approves within granted workspace scope |
| `spawn_agent` | GUARDED | **AGENT_SPAN** + MUTATES | Spawn a sub-agent — scope-aware: tracks spawn budget |
| `load_skill` | SAFE | — | Load a skill's full instructions on demand (Agent Skills spec Tier 2) |

> **Scope-aware authorization** (v0.2.9.3): tools with a `scope_resolver` are authorized at the resource level — once you approve "write under `/workspace/doc/`", subsequent writes under that path auto-approve without re-prompting. Tools without a resolver fall back to the legacy tool-name authorization.

---

## Skills — Procedural Memory with Self-Assessment

Skills are versioned, self-evaluating instruction sets stored in `SkillGenome`. They follow the [Agent Skills spec](https://agentskills.io/specification) three-tier progressive disclosure model:

| Tier | When | Content |
|------|------|---------|
| **Tier 1** | Session startup | `<available_skills>` XML injected into system prompt — name + description only |
| **Tier 2** | On demand | `load_skill(name)` → full SKILL.md body + evolution hints + resource list |
| **Tier 3** | As needed | Agent reads bundled `scripts/` / `references/` / `assets/` files directly |

### Auto-import

Drop a `SKILL.md` into the `skills/` directory — Loom auto-imports it at session start:

```
<workspace>/skills/<skill-name>/SKILL.md   ← project-level (priority)
~/.loom/skills/<skill-name>/SKILL.md       ← user-level
```

```markdown
---
name: loom-engineer
description: Full implementation cycle from issue to PR.
tags: [git, python, engineering]
precondition_checks:
  - ref: checks.require_git_repo
    applies_to: [run_bash, write_file]
    description: "Must be inside a git repository"
  - ref: checks.reject_force_push
    applies_to: [run_bash]
    description: "Block git push --force"
---

# Loom Engineer
Skill body here...
```

### Self-Assessment Feedback Loop

After each turn where a skill was used, Loom triggers a background LLM self-assessment (1–5 score). The result feeds into the skill's `confidence` via EMA (α=0.15):

```
load_skill() → Agent executes task → TurnDone
    → LLM self-rates quality (1–5)
    → EMA update: confidence = 0.85 × old + 0.15 × (score/5)
    → outcome persisted to SemanticMemory
```

At session end, `SkillEvolutionHook` analyses low-confidence skills (confidence < 0.6, usage ≥ 3) and writes improvement suggestions. Next `load_skill()` surfaces these as `<evolution_hints>`.

Skills whose `confidence` drops below `skill_deprecation_threshold` (default 0.3) are automatically deprecated and removed from the Tier 1 catalog.

### Skill Precondition Checks — Framework-Enforced Safety Rails

SKILL.md tells the LLM what it *should* do. But LLMs are probabilistic — a strongly worded "never run `rm -rf`" in a skill document is a suggestion, not a guarantee. **Precondition checks** close this gap by moving safety rules from text into code that the framework executes before every tool call.

**Why this matters:**

| Without precondition checks | With precondition checks |
|----------------------------|--------------------------|
| "Don't run destructive commands" — LLM *might* comply | `reject_destructive_commands()` — framework **blocks** `rm -rf` before shell sees it |
| "This skill is read-only" — LLM *usually* respects it | `reject_write_operations()` — `write_file` is **impossible** while skill is loaded |
| "Only modify files in skills/" — LLM *tries* to follow | `require_skills_dir_target()` — paths outside `skills/` are **rejected** at PREPARED gate |

**How it works:**

Skills declare checks in their SKILL.md frontmatter. Each check is an async Python function in the skill's `checks.py` that returns `True` (allow) or `False` (block):

```yaml
# skills/security_assessment/SKILL.md
---
name: security_assessment
precondition_checks:
  - ref: checks.reject_destructive_commands
    applies_to: [run_bash]
    description: "Block rm -rf, DROP TABLE, dd, etc."
  - ref: checks.reject_production_env
    applies_to: [run_bash]
    description: "Block execution in production environments"
---
```

```python
# skills/security_assessment/checks.py
async def reject_destructive_commands(call) -> bool:
    cmd = call.args.get("command", "")
    destructive = ["rm -rf", "DROP TABLE", "dd if="]
    return not any(p in cmd.lower() for p in destructive)
```

**Lifecycle integration:**

```
load_skill("security_assessment")
  → First load: user sees confirmation panel listing all checks → approves
  → Approval stored in RelationalMemory (one-time)
  → checks.py functions mounted onto target ToolDefinitions
  → Every run_bash call now passes through PREPARED gate checks

load_skill("loom_engineer")
  → Previous skill's checks auto-unmounted (no cross-skill contamination)
  → loom_engineer's own checks mounted instead
```

Checks are evaluated at the `PREPARED` stage of the [Action Lifecycle](#action-lifecycle) — after authorization but before execution. All checks must pass; any failure aborts the tool call with zero side effects.

**Built-in skill checks:**

| Skill | Check | Protects against |
|-------|-------|-----------------|
| `loom_engineer` | `require_git_repo` | Running git commands outside a repository |
| `loom_engineer` | `reject_force_push` | Accidental `git push --force` |
| `systematic_code_analyst` | `reject_write_operations` | Analysis skill writing files |
| `meta-skill-engineer` | `require_skills_dir_target` | Skill engineer modifying framework code |
| `security_assessment` | `reject_destructive_commands` | Destructive commands during security scans |
| `security_assessment` | `reject_production_env` | Running scans in production |
| `memory_hygiene` | `require_memory_backup` | Cleanup without backup |
| `memory_hygiene` | `reject_direct_db_mutation` | Bypassing framework APIs with raw SQL |

---

## Action Lifecycle

Every tool call in Loom is wrapped in an `ActionRecord` that tracks its complete lifecycle through a deterministic state machine. Unlike logging frameworks that annotate events after the fact, Loom's lifecycle states are **real-time control gates** — each transition must complete before execution proceeds to the next stage.

```
DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED → VALIDATED → COMMITTED → MEMORIALIZED
                                       ↓                       ↓
                                    ABORTED               REVERTING → REVERTED → MEMORIALIZED
```

Terminal failure paths: `DENIED`, `ABORTED`, `TIMED_OUT` → `MEMORIALIZED`

### Two-layer middleware architecture

The lifecycle is implemented as two cooperating middleware layers that share state through `LifecycleContext`:

| Layer | Position | Responsibilities |
|-------|----------|-----------------|
| `LifecycleMiddleware` | Outermost | `DECLARED` — creates `ActionRecord` + injects `LifecycleContext`; post-`OBSERVED` states (`VALIDATED`, `COMMITTED`, `REVERTING`, `REVERTED`, `MEMORIALIZED`); failure paths (`DENIED`, `ABORTED` from schema/auth) |
| `LifecycleGateMiddleware` | Innermost (just before handler) | `AUTHORIZED` ← reads from `BlastRadiusMiddleware`; `PREPARED` ← evaluates `precondition_checks[]`; `EXECUTING` ← fires at exact dispatch moment + races abort signal; `OBSERVED` ← fires when executor returns |

**Why two layers?** A single middleware cannot intercept both *before* and *at* the moment the tool executor runs. `next(call)` fires the entire remaining chain as one opaque call — the outermost layer has no way to inject logic immediately before the handler. Splitting into two layers solves this cleanly.

### Precondition gates

`ToolDefinition.precondition_checks` is a list of async callables evaluated before dispatch. All must pass for the tool to proceed to `EXECUTING`. Failure aborts with no side effects.

```python
async def require_write_lock(call: ToolCall) -> bool:
    return await lock_manager.is_held(call.session_id)

tool = ToolDefinition(
    name="write_critical_file",
    preconditions=["Requires active write lock"],        # human-readable audit trail
    precondition_checks=[require_write_lock],            # callable gate
    trust_level=TrustLevel.GUARDED,
)
```

Tools with no `precondition_checks` follow the same happy path as before — zero migration cost.

### Abort signal racing

When a tool call carries an `abort_signal` (`asyncio.Event`), `LifecycleGateMiddleware` races the executor against it using `asyncio.wait()`. If the signal fires during execution, the tool is cancelled, the record transitions to `ABORTED`, and `MEMORIALIZED` fires — the lifecycle always completes cleanly regardless of how execution ends.

### Post-validation and rollback

Attach `post_validator` and `rollback_fn` to any `ToolDefinition` to enable transactional semantics:

```python
tool = ToolDefinition(
    name="deploy",
    post_validator=verify_deployment_health,   # async (call, result) -> bool
    rollback_fn=rollback_deployment,           # async (call, result) -> ToolResult
)
```

If `post_validator` returns `False`, the lifecycle transitions `OBSERVED → VALIDATED → REVERTING → REVERTED → MEMORIALIZED`. The result returned to the LLM reflects the rollback, with `metadata["rolled_back"] = True`.

### Audit trail

Every tool call — success, failure, abort, timeout, or rollback — ends in `MEMORIALIZED`, firing the `on_lifecycle` callback. `TraceMiddleware` wires this to episodic memory: the complete lifecycle of every action is recorded, queryable, and available to reflection.

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

## Scope-Aware Authorization

Since v0.2.9.3, Loom upgrades tool authorization from tool-name-based to **resource-scope-based**. Instead of "do you approve `write_file`?", the system asks "do you approve writing under `/workspace/doc/`?" — and remembers the answer.

### How it works

```
Tool call: write_file(path="doc/design.md")
  │
  ├─ scope_resolver → ScopeRequest(resource=path, action=write, selector=/workspace/doc)
  ├─ PermissionContext.evaluate() → check existing grants
  │     ├─ Grant exists for /workspace/doc → ALLOW (no prompt)
  │     ├─ No grant → CONFIRM (first-time prompt)
  │     └─ Grant exists for /workspace/doc but request is /workspace/loom → EXPAND_SCOPE (red panel)
  └─ On user confirm → grant stored → future calls in same scope auto-approve
```

### Scope-aware tools

| Tool | Resource type | Scope logic |
|------|--------------|-------------|
| `write_file` | `path` | Directory prefix — grant for `/workspace/doc/` covers all files underneath |
| `run_bash` | `exec` | Workspace containment — pipes/subshells/`&&`/`||` marked as scope-unknown, always prompt |
| `fetch_url` | `network` | Exact domain match — grant for `api.openai.com` only covers that domain |
| `web_search` | `network` | Fixed domain (`api.search.brave.com`) |
| `spawn_agent` | `agent` | Budget tracking — grant with `remaining_budget=3` allows 3 spawns |

### Confirm panel

The confirmation panel shows structured scope information:

- **Yellow border** — first-time authorization or new resource type
- **Red border** — scope expansion beyond what was previously approved
- Resource type, action, and target selector displayed clearly
- Diff shows what's already covered vs. what's new

### `/auto` and scope grants

`/auto` now injects a scope grant (`exec:execute → workspace, absolute_paths=deny`) instead of a blanket flag. Workspace-confined commands auto-approve; commands with absolute paths outside the workspace still trigger a confirmation prompt.

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

[memory.governance]
admission_threshold      = 0.5    # filter low-quality facts before promotion
episodic_ttl_days        = 30     # delete episodic entries older than N days
semantic_decay_threshold = 0.1    # prune semantic entries below this confidence
relational_decay_factor  = 1.5    # accelerate dreaming triple decay (half-life = 90/factor)

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

# ── Autonomy (Unified Pipeline v0.2.9.4) ─────────────────────────────────────
# All execution paths share the same MiddlewarePipeline.
# Autonomy schedules run with origin="autonomy" — they can only "spend"
# pre-authorized grants, never request new permissions interactively.
# Declare needed GUARDED tools in allowed_tools; scope-aware tools also
# need scope_grants for resource-level authorization.
[autonomy]
enabled  = true
timezone = "Asia/Taipei"

[[autonomy.schedules]]
name          = "morning_briefing"
cron          = "0 1 * * *"          # 01:00 UTC = 09:00 Asia/Taipei
intent        = "Generate daily news briefing and write to news/YYYY-MM-DD/briefing.md"
trust_level   = "safe"
notify        = false
allowed_tools = ["write_file", "memorize"]   # GUARDED tools this schedule needs
scope_grants  = [
  { resource = "path", action = "write", selector = "news" },
]

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

Skills in the `skills/` directory are auto-imported at session start. For external formats:

```bash
loom import skills.json                          # Hermes format
loom import tools.json --lens openai_tools
loom import skills.json --dry-run --min-confidence 0.7
```

---

## Running Tests

```bash
python -m pytest tests/          # 654+ tests
python -m pytest tests/test_harness.py -v
python -m pytest tests/test_memory.py -v
python -m pytest tests/test_cognition.py -v
python -m pytest tests/test_autonomy.py -v
python -m pytest tests/test_integration.py -v
```

---

## License

MIT
