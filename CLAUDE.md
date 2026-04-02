# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in editable mode (run once)
pip install -e ".[dev]"

# Run all tests
python -m pytest tests/

# Run a single test file
python -m pytest tests/test_harness.py -v

# Run a single test by name
python -m pytest tests/test_autonomy.py::TestCronTrigger::test_should_fire_weekday_range -v

# Start interactive agent session (requires MINIMAX_API_KEY in .env)
loom chat

# Autonomy daemon
loom autonomy start --config loom.toml
loom autonomy status
loom autonomy emit <event_name>

# Inspect memory
loom memory list
loom reflect --session <session_id>
```

## Architecture

Loom is a harness-first, memory-native, self-directing agent framework. The codebase is organized as vertical layers with strict dependency direction: upper layers depend on lower ones, never the reverse.

```
Platform (CLI)  →  Cognition  →  Harness  →  Memory
                            ↘  Autonomy  →  Notify
                            ↘  Tasks
                            ↘  Extensibility
```

### Harness Layer (`loom/core/harness/`)

The spine of the framework. Every tool call flows through a `MiddlewarePipeline` before and after execution. Adding behavior means adding a `Middleware` subclass — no tool code changes needed.

- **`middleware.py`** — `ToolCall` / `ToolResult` data types; `MiddlewarePipeline` (recursive chain builder); `LogMiddleware`, `TraceMiddleware`, `BlastRadiusMiddleware`
- **`permissions.py`** — `TrustLevel` (SAFE / GUARDED / CRITICAL) and `PermissionContext` (session-scoped authorization state)
- **`registry.py`** — `ToolDefinition` with dual schema export (`to_openai_schema()` / `to_anthropic_schema()`); `ToolRegistry`

`TraceMiddleware` is the bridge to memory: its `on_trace` callback fires after every tool call, and `LoomSession` wires it to write `EpisodicEntry` rows.

`BlastRadiusMiddleware` calls an injected `confirm_fn` for GUARDED/CRITICAL tools. GUARDED authorizes for the rest of the session; CRITICAL re-confirms every time.

### Memory Layer (`loom/core/memory/`)

Single SQLite file (WAL mode) via `SQLiteStore`. All four memory types share one connection.

- **`episodic`** — append-only log of tool calls per session; compressed to semantic facts on session exit
- **`semantic`** — long-lived key→value facts; upsert by key; stores embedding vectors (`embedding TEXT`) for vector search
- **`procedural`** — `SkillGenome`: versioned skills with EMA-based `confidence` score; `is_deprecated` when `confidence <= deprecation_threshold`
- **`index.py`** — `MemoryIndex` lightweight directory injected into system prompt; `MemoryIndexer` builds it at session start
- **`search.py`** — `MemorySearch` with three-tier recall: embedding (cosine) → BM25 → recency fallback
- **`embeddings.py`** — `MiniMaxEmbeddingProvider` (`embo-01`); `build_embedding_provider(env)`; `cosine_similarity()`
- Relational table exists in the schema but has no read/write API yet (Phase 5)

Session compression: `compress_session()` in `main.py` sends episodic entries to the LLM and writes `FACT:` lines back as semantic entries. The prompt instructs the LLM to write facts in the same language as the session content.

### Cognition Layer (`loom/core/cognition/`)

- **`providers.py`** — `MiniMaxProvider` (OpenAI-compatible, with XML tool-call fallback parser for `<minimax:tool_call>` blocks) and `AnthropicProvider`. Both normalize to `LLMResponse` + OpenAI-canonical message history. `_to_anthropic_messages()` handles the conversion when Anthropic is the active provider. Both providers create their async HTTP clients once in `__init__` and reuse them.
- **`router.py`** — `LLMRouter` routes by model-name prefix (`MiniMax-*` → minimax, `claude-*` → anthropic). First registered provider is the default fallback.
- **`context.py`** — `ContextBudget` tracks token usage; `record_response()` uses replace (not accumulate) semantics; `should_compress()` triggers at 80%.
- **`reflection.py`** — `ReflectionAPI` queries episodic + procedural memory to produce session summaries and skill health reports.
- **`prompt_stack.py`** — `PromptStack` three-layer composition: SOUL.md (global identity) → Agent.md (project context) → personalities/<name>.md (cognitive lens).

### Task Engine (`loom/core/tasks/`)

`TaskGraph` builds a DAG with `add(content, depends_on=[...])`. `compile()` runs Kahn's topological sort and groups independent nodes into parallel levels. `TaskScheduler` executes each level with `asyncio.gather`.

**Live integration:** `LoomSession._dispatch_parallel()` uses `TaskGraph` + `TaskScheduler` to run multiple tool calls concurrently when the LLM requests them simultaneously. Parallelism is gated on `_all_authorized()` — tools requiring interactive confirmation (GUARDED not yet approved, CRITICAL) always run sequentially to avoid interleaved prompts.

### Autonomy Engine (`loom/autonomy/`)

Three trigger types fire the same callback chain:
1. `CronTrigger.should_fire(dt)` — standard 5-field cron (0=Sunday weekday convention, converted from Python's 0=Monday via `(weekday+1) % 7`)
2. `EventTrigger` — fired by `TriggerEvaluator.emit(event_name)`
3. `ConditionTrigger` — polled each cycle via `evaluate()`

`TriggerEvaluator` deduplicates cron fires within the same minute. `ActionPlanner` maps trust level → `ActionDecision`: SAFE→EXECUTE, GUARDED+notify→NOTIFY, CRITICAL→HOLD, disabled→SKIP. `AutonomyDaemon` loads triggers from `loom.toml` and routes planned actions to execute or confirm.

### Notification Layer (`loom/notify/`)

`NotificationRouter` fan-outs to all registered `BaseNotifier` subclasses concurrently; errors in one channel don't block others. `ConfirmFlow` wraps send + `asyncio.wait_for` with timeout, returning `APPROVED / DENIED / TIMEOUT`. Implemented notifiers: `CLINotifier` (Rich + stdin), `WebhookNotifier` (HTTP POST + `push_reply()` queue), `TelegramNotifier` (Bot API).

### Extensibility Layer (`loom/extensibility/`)

- **`lens.py`** — `BaseLens` abstract (`extract_skills / extract_middleware / extract_platform_adapters`)
- **`hermes.py`** — `HermesLens` (NousResearch/hermes-agent procedural memory format conversion)
- **`claw.py`** — `ClawCodeLens` (instructkr/claw-code harness pattern import)
- **`pipeline.py`** — Skill Import Pipeline (sandbox evaluation + confidence gate)
- **`adapter.py`** — Adapter Registry (`@loom.tool` decorator for external tool registration)

### CLI Platform (`loom/platform/cli/`)

`LoomSession` wires all layers together: builds the pipeline, opens the SQLite store, constructs the router and embedding provider from `.env`, and runs the agent loop.

- **`ui.py`** — event model (`TextChunk / ToolBegin / ToolEnd / TurnDone`), `PromptSession` (prompt_toolkit, InMemoryHistory, SlashCompleter)
- **`tools.py`** — 6 built-in tools: `read_file` (SAFE), `list_dir` (SAFE), `write_file` (GUARDED), `run_bash` (GUARDED), `recall` (SAFE), `memorize` (GUARDED)
- **`main.py`** — `LoomSession`, `stream_turn()` (streaming agent loop), `_dispatch_parallel()` (TaskGraph-backed parallel tool dispatch), `_smart_compact()` (LLM-based context compaction), slash command handling

Streaming uses direct `console.print(chunk, end="")` — no Rich Live — for genuine token-by-token output and clean stdin interaction.

## Key Conventions

- **Message history is always OpenAI-canonical** internally. Provider-specific conversion happens inside each `LLMProvider._sync_chat()`, never at the call site.
- **`loom.toml`** is the config file. API keys live in `.env` with keys `minimax.io_key` / `MINIMAX_API_KEY` / `ANTHROPIC_API_KEY`.
- **`asyncio_mode = "auto"`** in `pyproject.toml` — all async test functions run without `@pytest.mark.asyncio`.
- **`tests/` is gitignored** — tests exist locally but are not pushed to the repository.
- New middleware should be added to the pipeline in `LoomSession.start()` in `main.py`, not inside individual tools.
- When adding a new notifier, subclass `BaseNotifier` from `loom/notify/router.py` and register it in `NotificationRouter`.
- `record_response(input_tokens, output_tokens)` uses **replace** semantics (not accumulate) — `input_tokens` is the total context the model processed that call.
- Parallel tool dispatch only activates when `_all_authorized()` returns True. Never add interactive confirmation inside parallel paths.
- Embedding failures in `SemanticMemory.upsert()` are silently swallowed — never let a network error block a memory write.
- Context compaction (`_smart_compact`) requires ≥3 user turns to run; falls back to turn-boundary drop otherwise.

## Current Test Count

371 tests, all passing, Python 3.14 / pytest 9.0.
