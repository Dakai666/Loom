# Time-Aware Memory Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, time-aware memory retrieval across semantic, episodic, and session-log layers so Loom can answer period questions like "what did we discuss yesterday?"

**Architecture:** Add additive range-query methods to each memory layer, thread `since` / `until` through semantic recall, then expose a new SAFE `recall_period` tool that returns grouped evidence from semantic facts, episodic events, and optionally sessions/messages. Keep all existing recall behavior backward compatible.

**Tech Stack:** Python 3.11+, SQLite via `aiosqlite`, pytest + pytest-asyncio, existing Loom memory/tool abstractions.

---

## File Structure

- Modify `loom/core/memory/semantic.py`: add `list_between`.
- Modify `loom/core/memory/episodic.py`: add `list_between`.
- Modify `loom/core/memory/session_log.py`: add `list_sessions_between` and `messages_between`.
- Modify `loom/core/memory/search.py`: add time bounds to semantic embedding, BM25, and fallback paths.
- Modify `loom/core/memory/facade.py`: forward time bounds and expose period recall helper.
- Modify `loom/platform/cli/tools.py`: add `since` / `until` to `recall`; add `make_recall_period_tool`.
- Modify `loom/core/session.py`: register `recall_period`.
- Modify `tests/test_memory.py`: semantic and episodic range tests.
- Modify `tests/test_session.py`: session-log range tests.
- Modify `tests/test_memory_search.py`: recall time filter and tool tests.
- Modify `tests/test_memory_facade.py`: facade period helper tests.

---

### Task 1: Memory Layer Time Range APIs

**Files:**
- Modify: `loom/core/memory/semantic.py`
- Modify: `loom/core/memory/episodic.py`
- Modify: `loom/core/memory/session_log.py`
- Test: `tests/test_memory.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Run GitNexus impact analysis**

Run:

```bash
npx gitnexus impact list_recent --direction upstream
npx gitnexus impact read_session --direction upstream
npx gitnexus impact list_sessions --direction upstream
```

Expected: LOW or UNKNOWN for newly-added sibling methods. If HIGH or CRITICAL appears, stop and report before editing.

- [ ] **Step 2: Write failing semantic and episodic tests**

Add tests that insert rows with explicit timestamps, then assert:

```python
semantic_results = await sm.list_between(
    since=datetime(2026, 5, 10, tzinfo=UTC),
    until=datetime(2026, 5, 11, tzinfo=UTC),
)
assert [e.key for e in semantic_results] == ["inside:new", "inside:old"]
```

and:

```python
episodic_results = await em.list_between(
    since=datetime(2026, 5, 10, tzinfo=UTC),
    until=datetime(2026, 5, 11, tzinfo=UTC),
    session_id="s1",
)
assert [e.content for e in episodic_results] == ["inside s1"]
```

- [ ] **Step 3: Verify RED**

Run:

```bash
pytest -q tests/test_memory.py::TestSemanticMemory::test_list_between_filters_updated_at tests/test_memory.py::TestEpisodicMemory::test_list_between_filters_created_at_and_session
```

Expected: FAIL because `list_between` does not exist.

- [ ] **Step 4: Implement minimal APIs**

Implement signatures:

```python
async def list_between(
    self,
    since: datetime,
    until: datetime | None = None,
    *,
    limit: int = 20,
) -> list[SemanticEntry]:
```

and:

```python
async def list_between(
    self,
    since: datetime,
    until: datetime | None = None,
    *,
    session_id: str | None = None,
    limit: int = 50,
) -> list[EpisodicEntry]:
```

Use `updated_at >= ? AND updated_at < ?` for semantic and `created_at >= ? AND created_at < ?` for episodic. Order semantic newest first and episodic chronological.

- [ ] **Step 5: Add session-log tests and APIs**

Test:

```python
sessions = await log.list_sessions_between(
    since=datetime(2026, 5, 10, tzinfo=UTC),
    until=datetime(2026, 5, 11, tzinfo=UTC),
)
assert [s["session_id"] for s in sessions] == ["active-inside"]
```

Implement:

```python
async def list_sessions_between(self, since: datetime, until: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]
async def messages_between(self, since: datetime, until: datetime | None = None, *, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
pytest -q tests/test_memory.py tests/test_session.py
```

Expected: new tests pass; existing tests remain green.

---

### Task 2: Thread Time Bounds Through Semantic Recall

**Files:**
- Modify: `loom/core/memory/search.py`
- Modify: `loom/core/memory/facade.py`
- Modify: `loom/platform/cli/tools.py`
- Test: `tests/test_memory_search.py`
- Test: `tests/test_memory_facade.py`

- [ ] **Step 1: Run GitNexus impact analysis**

Run:

```bash
npx gitnexus impact recall --direction upstream
npx gitnexus impact Function:loom/core/memory/facade.py:MemoryFacade.search --direction upstream
npx gitnexus impact make_recall_tool --direction upstream
```

Expected: LOW. If HIGH or CRITICAL appears, stop and report.

- [ ] **Step 2: Write failing search test**

Add a test that writes one semantic fact inside and one outside a date range, then:

```python
results = await search.recall(
    "loom",
    type="semantic",
    since=datetime(2026, 5, 10, tzinfo=UTC),
    until=datetime(2026, 5, 11, tzinfo=UTC),
)
assert [r.key for r in results] == ["inside"]
```

- [ ] **Step 3: Verify RED**

Run:

```bash
pytest -q tests/test_memory_search.py::TestMemorySearch::test_recall_filters_by_updated_at_range
```

Expected: FAIL because `recall()` does not accept `since`.

- [ ] **Step 4: Implement semantic time filters**

Add optional `since: datetime | None = None` and `until: datetime | None = None` through:

- `MemorySearch.recall`
- `_search_semantic_embedding`
- `_search_semantic`
- `_recent_fallback`
- `MemoryFacade.search`

Build SQL fragments using `e.updated_at >= ?` and `e.updated_at < ?` on joined queries, and pass bounds to `SemanticMemory.list_between` for fallback when any time bound is present.

- [ ] **Step 5: Write failing recall tool forwarding test**

Create a mock facade and assert `make_recall_tool` parses and forwards `since` / `until`:

```python
call = _make_call("recall", {
    "query": "loom",
    "type": "semantic",
    "since": "2026-05-10",
    "until": "2026-05-11",
})
await tool.executor(call)
facade.search.assert_awaited_once()
assert facade.search.await_args.kwargs["since"].isoformat().startswith("2026-05-10")
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
pytest -q tests/test_memory_search.py tests/test_memory_facade.py
```

Expected: all pass.

---

### Task 3: Add `recall_period` Tool

**Files:**
- Modify: `loom/core/memory/facade.py`
- Modify: `loom/platform/cli/tools.py`
- Modify: `loom/core/session.py`
- Test: `tests/test_memory_facade.py`
- Test: `tests/test_memory_search.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Run GitNexus impact analysis**

Run:

```bash
npx gitnexus impact memorize --direction upstream
npx gitnexus impact make_recall_tool --direction upstream
npx gitnexus impact start --direction upstream
```

Expected: LOW or known broad session impact for registration. If HIGH or CRITICAL appears, stop and report.

- [ ] **Step 2: Write failing facade period test**

Add:

```python
result = await facade.recall_period(
    since=datetime(2026, 5, 10, tzinfo=UTC),
    until=datetime(2026, 5, 11, tzinfo=UTC),
    include_episodic=True,
    include_sessions=True,
    limit=5,
)
assert result["semantic"]
assert result["episodic"]
assert result["sessions"]
```

- [ ] **Step 3: Implement facade helper**

Add:

```python
async def recall_period(
    self,
    *,
    since: datetime,
    until: datetime | None = None,
    session_id: str | None = None,
    limit: int = 10,
    include_episodic: bool = True,
    include_sessions: bool = False,
) -> dict[str, list[Any]]:
```

Return semantic facts from `semantic.list_between`, episodic entries from `episodic.list_between`, and sessions plus message rows from `session_log` only when a session-log handle is available.

- [ ] **Step 4: Write failing tool test**

Add a tool-level test:

```python
tool = make_recall_period_tool(facade)
call = _make_call("recall_period", {
    "since": "2026-05-10",
    "until": "2026-05-11",
    "include_episodic": True,
    "include_sessions": True,
})
result = await tool.executor(call)
assert result.success is True
assert "Semantic facts" in result.output
assert "Episodic events" in result.output
assert "Sessions" in result.output
assert "Session messages" in result.output
```

- [ ] **Step 5: Implement tool and registration**

Add `make_recall_period_tool(memory)` next to `make_recall_tool`, parse dates via a small helper, clamp `limit` to 20, and register it in `LoomSession.start()` after `recall`.

- [ ] **Step 6: Verify GREEN**

Run:

```bash
pytest -q tests/test_memory_search.py tests/test_memory_facade.py tests/test_session.py tests/test_memory.py
```

Expected: all pass.

---

### Task 4: Final Verification and PR

**Files:**
- All changed implementation/test/spec/plan files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest -q tests/test_memory.py tests/test_memory_search.py tests/test_memory_facade.py tests/test_session.py
```

Expected: all pass.

- [ ] **Step 2: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes
```

Expected: changed symbols match memory-time retrieval work.

- [ ] **Step 3: Stage and commit**

Run:

```bash
git add docs/superpowers/specs/2026-05-11-time-aware-memory-recall-design.md docs/superpowers/plans/2026-05-11-time-aware-memory-recall.md loom/core/memory/semantic.py loom/core/memory/episodic.py loom/core/memory/session_log.py loom/core/memory/search.py loom/core/memory/facade.py loom/platform/cli/tools.py loom/core/session.py tests/test_memory.py tests/test_session.py tests/test_memory_search.py tests/test_memory_facade.py
git commit -m "feat(memory): add time-aware recall"
```

- [ ] **Step 4: Push and open PR**

Run:

```bash
git push -u origin codex/time-aware-memory-recall
gh pr create --repo Dakai666/Loom --title "feat(memory): add time-aware recall" --body "Closes #354"
```

Expected: PR created against the default branch.

---

## Self-Review

- Spec coverage: semantic time filter, episodic range query, session-log range query, and SAFE period tool are covered.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: date parameters are `datetime` internally and strings at the tool boundary.
