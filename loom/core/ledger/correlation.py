"""correlation_id propagation via contextvars.

doc/53 §4.2: middleware auto-inherit is the main strategy. Three
event types open a fresh correlation_id (env_observation,
memory_op.compact, turn_end.outcome=error) — that discipline lives
at the emit call site, not here.

Usage:
    with correlation_scope("user_q_xyz"):
        # any code in this block sees current_correlation() == "user_q_xyz"
        await tool.run(...)

    async with async_correlation_scope("dream_001"):
        ...

    # Or imperatively, paired with reset():
    token = set_correlation("ad-hoc")
    try:
        ...
    finally:
        reset_correlation(token)
"""

from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar, Token

_current_correlation: ContextVar[str | None] = ContextVar(
    "loom_ledger_correlation_id", default=None
)


def new_correlation_id(prefix: str = "corr") -> str:
    """Mint a fresh correlation_id. ``prefix`` aids log grep but has no semantics."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def current_correlation() -> str | None:
    """Return the active correlation_id, or None if no scope is active."""
    return _current_correlation.get()


def set_correlation(corr_id: str) -> Token:
    """Imperatively set the active correlation_id. Returns a Token for reset."""
    return _current_correlation.set(corr_id)


def reset_correlation(token: Token) -> None:
    _current_correlation.reset(token)


@contextlib.contextmanager
def correlation_scope(corr_id: str):
    """Sync contextmanager — sets correlation_id for the block."""
    token = _current_correlation.set(corr_id)
    try:
        yield corr_id
    finally:
        _current_correlation.reset(token)


@contextlib.asynccontextmanager
async def async_correlation_scope(corr_id: str):
    """Async contextmanager — sets correlation_id for the block."""
    token = _current_correlation.set(corr_id)
    try:
        yield corr_id
    finally:
        _current_correlation.reset(token)
