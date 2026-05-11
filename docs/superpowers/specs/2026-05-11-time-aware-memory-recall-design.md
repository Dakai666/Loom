# Time-Aware Memory Recall Design

## Problem

Loom stores time information in all memory strata, but agent-facing retrieval cannot ask time-shaped questions. A user can ask "what did we talk about yesterday?" or "what happened last week?", yet `recall` only performs semantic relevance search with ontology filters. The data exists, but the access path does not.

## Goal

Add time-aware memory access that follows the existing memory stack:

1. Semantic memory first for distilled facts.
2. Episodic memory next for time-stamped events and tool traces.
3. Session log last for raw conversation history when deeper recall is needed.

## Non-Goals

- Do not replace semantic recall ranking.
- Do not build an LLM summarizer in this pass.
- Do not expose unrestricted SQL or arbitrary DB queries.
- Do not migrate existing memory rows beyond indexes or additive columns if required.

## Architecture

### Semantic Layer

Add a bounded time query API to `SemanticMemory` and thread it through `MemorySearch.recall`, `MemoryFacade.search`, and the `recall` tool. The tool accepts ISO-like `since` and `until` arguments and applies them to `semantic_entries.updated_at`.

When a query has both semantic terms and time bounds, results should still rank by embedding/BM25 inside the time window. When a query is generic but time-bound, the caller can request recent facts in the range by using the new period tool rather than abusing keyword recall.

### Episodic Layer

Add `EpisodicMemory.list_between(since, until, session_id=None, limit=...)`. It returns chronological episodic entries for a date range, optionally scoped to one session. This lets tools inspect the compressed source layer without needing a known session id first.

### Session Log Layer

Add `SessionLog.list_sessions_between(since, until, limit=...)` and `SessionLog.messages_between(since, until, session_id=None, limit=...)`. The first answers "which sessions were active in this window"; the second supports deep recall for diary-style use.

### Agent Tool

Add a SAFE tool named `recall_period`.

Inputs:

- `since`: required ISO timestamp or date string.
- `until`: optional ISO timestamp or date string.
- `limit`: capped result count.
- `include_episodic`: optional boolean, default `true`.
- `include_sessions`: optional boolean, default `false`.
- `session_id`: optional scope.

Output groups entries by source layer:

- `Semantic facts`
- `Episodic events`
- `Sessions`
- `Session messages`

Each line includes timestamp, key or session id, and a short value. The tool does not summarize with an LLM; it returns traceable evidence that the agent can use to answer naturally.

`Session messages` are forensic rows from `session_log`, not reconstructed chat replay messages. They preserve the stored `session_id`, `turn_index`, `role`, redacted `content`, and `created_at` for diary-style investigation.

## Data Flow

For "what did we talk about yesterday?":

1. Agent resolves "yesterday" using current time context.
2. Agent calls `recall_period(since="2026-05-10", until="2026-05-11")`.
3. Tool returns semantic facts updated in that range.
4. If facts are too sparse, the same call includes episodic events.
5. If deeper inspection is needed, `include_sessions=true` surfaces matching sessions and optional raw messages.

## Error Handling

- Invalid dates return a tool error with a concise message.
- Missing `since` returns a tool error.
- Providing `until` without `since` returns a tool error at the tool boundary.
- `until <= since` returns a tool error.
- Empty ranges return success with "No memories found for this period."
- Limits are clamped to a small maximum to protect context.

## Testing

Add tests before implementation:

- `SemanticMemory.list_between` filters by `updated_at`.
- `MemorySearch.recall` respects `since` / `until`.
- `make_recall_tool` forwards time filters.
- `EpisodicMemory.list_between` works globally and per session.
- `SessionLog.list_sessions_between` finds active sessions in a range.
- `recall_period` returns grouped semantic/episodic/session output.

## Rollout

This is additive and backward compatible. Existing callers of `recall` continue working. New arguments are optional. The period tool is SAFE because it only reads local memory and caps output.
