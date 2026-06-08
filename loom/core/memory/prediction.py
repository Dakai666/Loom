"""
Prediction Spine — episodic prediction log (epic #528, spec docs/designs/58 §3.1).

A ``PredictionRecord`` is a bet the agent makes about the world: *I expect this
command to succeed / this PR to merge / this output to contain X*. Bets are
reconciled against **runtime observation** (never LLM self-narrative — I2) in
the convergent dream, producing a per-domain calibration residue that lives in
semantic memory. Individual records decay like any episodic entry; the rolled-up
``calibration:<domain-or-resolver>`` summary is what persists (§3.2).

This module is the data layer only — slice 1. The reconciliation pipeline
(``run_prediction_reconciliation``) and the resolver whitelist land in later
slices. Everything here enforces the structural seeds of the lifeline
invariants:

* **I1** — the ``(claim, due_condition, resolver)`` triple is mandatory; a
  record missing any leg cannot be written.
* **I4** — ``status`` is an explicit state machine. A bet is born ``pending``;
  only a *reconciled* record carries a ``score`` and an ``observation_ref``.
  Reconcile and stale are terminal-aware transitions, so a verified bet cannot
  be re-scored (idempotency seed) and an unverified bet cannot masquerade as
  verified.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import aiosqlite

# I4 — the only legal status values, and which ones are terminal.
VALID_STATUSES = ("pending", "due", "reconciled", "stale")
_TERMINAL = ("reconciled", "stale")


@dataclass
class PredictionRecord:
    session_id: str
    claim: str                                  # what is predicted
    due_condition: dict[str, Any]               # when it can be reconciled
    resolver: dict[str, Any]                     # how to mechanically judge
    domain: str = "knowledge"   # "knowledge"|"cli"|"git"|... — enum/normalize in slice 4
    context: str | None = None
    status: str = "pending"
    observation_ref: str | None = None          # set at reconcile
    score: float | None = None                  # error_score, None until reconciled
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reconciled_at: datetime | None = None


def _row_to_record(r: Any) -> PredictionRecord:
    return PredictionRecord(
        id=r[0],
        session_id=r[1],
        claim=r[2],
        due_condition=json.loads(r[3]),
        resolver=json.loads(r[4]),
        status=r[5],
        domain=r[6],
        context=r[7],
        observation_ref=r[8],
        score=r[9],
        metadata=json.loads(r[10]),
        created_at=datetime.fromisoformat(r[11]),
        reconciled_at=datetime.fromisoformat(r[12]) if r[12] else None,
    )


_COLUMNS = (
    "id, session_id, claim, due_condition, resolver, status, domain, "
    "context, observation_ref, score, metadata, created_at, reconciled_at"
)


class PredictionStore:
    """Read/write access to the prediction_records table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # -- write -------------------------------------------------------------

    async def write(self, rec: PredictionRecord) -> None:
        """Persist a fresh bet. Enforces I1 (full triple) and I4 (born pending).

        Raises ``ValueError`` when any leg of the ``(claim, due_condition,
        resolver)`` triple is empty, or when the record claims a non-``pending``
        status at birth (a bet cannot be verified before it has been observed).
        """
        if not rec.claim or not rec.claim.strip():
            raise ValueError("prediction.claim is required (I1)")
        if not rec.due_condition:
            raise ValueError("prediction.due_condition is required (I1)")
        if not rec.resolver:
            raise ValueError("prediction.resolver is required (I1)")
        if rec.status != "pending":
            raise ValueError(
                f"a prediction is born 'pending', not {rec.status!r} (I4)"
            )
        await self._db.execute(
            f"INSERT INTO prediction_records ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.id,
                rec.session_id,
                rec.claim,
                json.dumps(rec.due_condition, ensure_ascii=False),
                json.dumps(rec.resolver, ensure_ascii=False),
                rec.status,
                rec.domain,
                rec.context,
                rec.observation_ref,
                rec.score,
                json.dumps(rec.metadata, ensure_ascii=False),
                rec.created_at.isoformat(),
                rec.reconciled_at.isoformat() if rec.reconciled_at else None,
            ),
        )
        await self._db.commit()

    # -- read --------------------------------------------------------------

    async def get(self, prediction_id: str) -> PredictionRecord | None:
        cur = await self._db.execute(
            f"SELECT {_COLUMNS} FROM prediction_records WHERE id = ?",
            (prediction_id,),
        )
        row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list_by_status(
        self, status: str, *, limit: int = 200
    ) -> list[PredictionRecord]:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected {VALID_STATUSES}")
        cur = await self._db.execute(
            f"SELECT {_COLUMNS} FROM prediction_records "
            "WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        )
        return [_row_to_record(r) for r in await cur.fetchall()]

    # -- transitions (I4 state machine) -----------------------------------

    async def mark_due(self, prediction_id: str) -> None:
        """pending → due. The bet's due_condition has been met; it awaits reconcile."""
        await self._transition(prediction_id, frm=("pending",), to="due")

    async def mark_reconciled(
        self, prediction_id: str, *, score: float, observation_ref: str
    ) -> None:
        """pending/due → reconciled. Requires a ground-truth ``observation_ref``.

        The ``observation_ref`` requirement is the structural seed of I2: a
        score can only exist alongside a pointer to the observation that
        produced it. Reconcile is terminal — re-reconciling raises (idempotency).
        """
        if not observation_ref or not observation_ref.strip():
            raise ValueError(
                "mark_reconciled requires an observation_ref (I2 seed): a score "
                "must point at the observation that produced it"
            )
        await self._transition(
            prediction_id,
            frm=("pending", "due"),
            to="reconciled",
            score=score,
            observation_ref=observation_ref,
            stamp_reconciled=True,
        )

    async def mark_stale(self, prediction_id: str) -> None:
        """pending/due → stale. The bet expired unverified; it never carries a score."""
        await self._transition(prediction_id, frm=("pending", "due"), to="stale")

    async def _transition(
        self,
        prediction_id: str,
        *,
        frm: tuple[str, ...],
        to: str,
        score: float | None = None,
        observation_ref: str | None = None,
        stamp_reconciled: bool = False,
    ) -> None:
        rec = await self.get(prediction_id)
        if rec is None:
            raise ValueError(f"no prediction {prediction_id!r}")
        if rec.status not in frm:
            raise ValueError(
                f"cannot move prediction from {rec.status!r} to {to!r} "
                f"(allowed from {frm})"
            )
        stamp = datetime.now(UTC).isoformat() if stamp_reconciled else None
        await self._db.execute(
            "UPDATE prediction_records SET status = ?, score = ?, "
            "observation_ref = COALESCE(?, observation_ref), reconciled_at = ? "
            "WHERE id = ?",
            (to, score, observation_ref, stamp, prediction_id),
        )
        await self._db.commit()
