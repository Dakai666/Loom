"""Pull API — fluent query builder.

doc/53 §6.2. Covers the common ledger query shapes (where + since/until +
order_by + limit + all/first/count/group_by) without becoming a full
ORM. Complex queries go through ``ledger.execute_sql(...)`` (§6.2 raw
SQL escape).

Design:
- Immutable builder. Every method returns a new EventQuery; callers
  can reuse intermediate refs without copy-on-write surprises.
- Whitelisted column names — no SQL injection through ``where()``.
- ``where_payload(...)`` uses ``json_extract(payload, '$.key')`` and is
  documented as slower (no index) so callers know to favour generated
  columns where possible.
- ``group_by(field).count_by()`` is the only aggregate exposed in v1
  per §6.2 ("Aggregate 限制在簡單情境").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loom.core.ledger.schema import DEFAULT_BRANCH, LedgerEvent

if TYPE_CHECKING:
    from loom.core.ledger.store import LedgerStore


# Top-level columns + generated columns (§5.2 / §5.3). Whitelisted to
# block SQL injection through ``where(...)`` keys.
_QUERY_COLUMNS: frozenset[str] = frozenset(
    {
        "event_id",
        "session_id",
        "turn_id",
        "parent_event_id",
        "correlation_id",
        "branch_id",
        "event_type",
        "tool_name",
        "verdict",
        "skill_id",
        "predecessor_memory_id",
    }
)

_PAYLOAD_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _normalize_ts(value: float | int | datetime) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


@dataclass
class EventQuery:
    """Fluent, immutable ledger query builder.

    Obtain via ``store.events`` (a fresh instance per access). All
    chaining methods return a new instance — the receiver is never
    mutated.
    """

    _store: "LedgerStore"
    _column_conditions: list[tuple[str, Any]] = field(default_factory=list)
    _payload_conditions: list[tuple[str, Any]] = field(default_factory=list)
    _since_ts: float | None = None
    _until_ts: float | None = None
    _order: tuple[str, bool] | None = None
    _limit_n: int | None = None
    _branch_filter: str | None = DEFAULT_BRANCH

    # -- chaining ---------------------------------------------------------

    def _clone(self) -> "EventQuery":
        return EventQuery(
            _store=self._store,
            _column_conditions=list(self._column_conditions),
            _payload_conditions=list(self._payload_conditions),
            _since_ts=self._since_ts,
            _until_ts=self._until_ts,
            _order=self._order,
            _limit_n=self._limit_n,
            _branch_filter=self._branch_filter,
        )

    def where(self, **kwargs: Any) -> "EventQuery":
        """Filter by top-level or generated column. Equality only.

        Allowed keys: ``event_id`` / ``session_id`` / ``turn_id`` /
        ``parent_event_id`` / ``correlation_id`` / ``branch_id`` /
        ``event_type`` / ``tool_name`` / ``verdict`` / ``skill_id`` /
        ``predecessor_memory_id``. Other payload keys go through
        :meth:`where_payload`.
        """
        new = self._clone()
        for k, v in kwargs.items():
            if k not in _QUERY_COLUMNS:
                raise ValueError(
                    f"unknown column {k!r}; use where_payload(...) for "
                    f"arbitrary payload keys, or pick from {sorted(_QUERY_COLUMNS)}"
                )
            if k == "branch_id":
                new._branch_filter = v
            else:
                new._column_conditions.append((k, v))
        return new

    def where_payload(self, **kwargs: Any) -> "EventQuery":
        """Filter by an arbitrary payload key via ``json_extract``.

        Slower than :meth:`where` (no index). Reserve for fields not
        promoted to generated columns. Keys must match
        ``[A-Za-z_][A-Za-z0-9_]*`` to keep the JSON path safe.
        """
        new = self._clone()
        for k, v in kwargs.items():
            if not _PAYLOAD_KEY_RE.match(k):
                raise ValueError(
                    f"invalid payload key {k!r}; only ASCII identifiers allowed"
                )
            new._payload_conditions.append((k, v))
        return new

    def since(self, when: float | int | datetime) -> "EventQuery":
        new = self._clone()
        new._since_ts = _normalize_ts(when)
        return new

    def until(self, when: float | int | datetime) -> "EventQuery":
        new = self._clone()
        new._until_ts = _normalize_ts(when)
        return new

    def order_by(self, field_name: str, *, desc: bool = False) -> "EventQuery":
        if field_name != "timestamp" and field_name not in _QUERY_COLUMNS:
            raise ValueError(
                f"order_by only supports columns; got {field_name!r}"
            )
        new = self._clone()
        new._order = (field_name, desc)
        return new

    def limit(self, n: int) -> "EventQuery":
        new = self._clone()
        new._limit_n = int(n)
        return new

    def on_branch(self, branch_id: str | None) -> "EventQuery":
        """Override the branch filter. ``None`` removes branch filtering
        entirely (cross-branch reads, v2 territory)."""
        new = self._clone()
        new._branch_filter = branch_id
        return new

    # -- terminal verbs ---------------------------------------------------

    def _build_where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self._branch_filter is not None:
            clauses.append("branch_id=?")
            params.append(self._branch_filter)
        for col, value in self._column_conditions:
            clauses.append(f"{col}=?")
            params.append(value)
        for key, value in self._payload_conditions:
            clauses.append(f"json_extract(payload, '$.{key}')=?")
            params.append(value)
        if self._since_ts is not None:
            clauses.append("timestamp>=?")
            params.append(self._since_ts)
        if self._until_ts is not None:
            clauses.append("timestamp<?")
            params.append(self._until_ts)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where_sql, params

    def _build_select(self) -> tuple[str, list[Any]]:
        cols = (
            "event_id, session_id, turn_id, parent_event_id, correlation_id, "
            "branch_id, event_type, timestamp, payload"
        )
        where_sql, params = self._build_where()
        sql = f"SELECT {cols} FROM events{where_sql}"
        if self._order:
            field_name, desc = self._order
            sql += f" ORDER BY {field_name} {'DESC' if desc else 'ASC'}"
        elif self._limit_n is not None:
            # Provide a stable order when limit is set even if caller
            # didn't request one — predictable .first() / .limit() results.
            sql += " ORDER BY timestamp ASC"
        if self._limit_n is not None:
            sql += f" LIMIT {int(self._limit_n)}"
        return sql, params

    @staticmethod
    def _row_to_event(r) -> LedgerEvent:
        return LedgerEvent(
            event_id=r[0],
            session_id=r[1],
            turn_id=r[2],
            parent_event_id=r[3],
            correlation_id=r[4],
            branch_id=r[5],
            event_type=r[6],
            timestamp=r[7],
            payload=json.loads(r[8]),
        )

    async def all(self) -> list[LedgerEvent]:
        sql, params = self._build_select()
        rows = await self._store._execute_query(sql, tuple(params))
        return [self._row_to_event(r) for r in rows]

    async def first(self) -> LedgerEvent | None:
        new = self._clone()
        new._limit_n = 1
        rows = await self._store._execute_query(*self._build_first_sql(new))
        if not rows:
            return None
        return self._row_to_event(rows[0])

    @staticmethod
    def _build_first_sql(q: "EventQuery") -> tuple[str, tuple]:
        sql, params = q._build_select()
        return sql, tuple(params)

    async def count(self) -> int:
        where_sql, params = self._build_where()
        sql = f"SELECT COUNT(*) FROM events{where_sql}"
        rows = await self._store._execute_query(sql, tuple(params))
        return int(rows[0][0]) if rows else 0

    def group_by(self, field_name: str) -> "_GroupBy":
        if field_name != "timestamp" and field_name not in _QUERY_COLUMNS:
            raise ValueError(f"group_by only supports columns; got {field_name!r}")
        return _GroupBy(self, field_name)


class _GroupBy:
    """Terminal group_by builder. Only ``count_by()`` exposed in v1
    per doc/53 §6.2 — complex aggregates use :meth:`LedgerStore.execute_sql`.
    """

    def __init__(self, query: EventQuery, field_name: str) -> None:
        self._query = query
        self._field = field_name

    async def count_by(self) -> dict[Any, int]:
        where_sql, params = self._query._build_where()
        sql = (
            f"SELECT {self._field}, COUNT(*) "
            f"FROM events{where_sql} "
            f"GROUP BY {self._field}"
        )
        rows = await self._query._store._execute_query(sql, tuple(params))
        return {r[0]: int(r[1]) for r in rows}
