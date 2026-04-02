"""
Loom REST API Server.

Exposes memory stores and webhook-reply endpoint over HTTP.
Requires the ``api`` extra:  pip install loom[api]

Endpoints
---------
GET  /health                        — liveness probe
GET  /memory/semantic?limit=N       — list recent semantic facts
POST /memory/semantic               — upsert a semantic fact
GET  /memory/relational?subject=&predicate=  — query relational triples
POST /memory/relational             — upsert a relational triple
POST /webhook/reply                 — push a confirm result to WebhookNotifier
POST /events/emit                   — emit an autonomy event (requires daemon)

Usage
-----
    loom api start --host 0.0.0.0 --port 8000 --db ~/.loom/memory.db
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Query
    from pydantic import BaseModel
    import uvicorn
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Loom REST API requires FastAPI and uvicorn. "
        "Install with:  pip install loom[api]"
    ) from exc

import aiosqlite

from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.notify.types import ConfirmResult


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SemanticBody(BaseModel):
    key: str
    value: str
    confidence: float = 0.8
    source: str = "api"


class RelationalBody(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    source: str = "api"


class WebhookReplyBody(BaseModel):
    notification_id: str
    result: str     # "approved" | "denied"


class EmitEventBody(BaseModel):
    event_name: str
    context: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    db_path: str = "~/.loom/memory.db",
    webhook_notifier=None,    # WebhookNotifier | None
    trigger_evaluator=None,   # TriggerEvaluator | None
) -> FastAPI:
    """
    Build and return the FastAPI application.

    Parameters
    ----------
    db_path:           Path to the shared SQLite database.
    webhook_notifier:  Optional WebhookNotifier instance for /webhook/reply.
    trigger_evaluator: Optional TriggerEvaluator for /events/emit.
    """
    resolved_path = str(Path(db_path).expanduser().resolve())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = await aiosqlite.connect(resolved_path)
        app.state.db.row_factory = aiosqlite.Row
        app.state.webhook_notifier = webhook_notifier
        app.state.trigger_evaluator = trigger_evaluator
        yield
        await app.state.db.close()

    app = FastAPI(
        title="Loom API",
        description="Memory and autonomy REST interface for the Loom agent framework.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Health ────────────────────────────────────────────────────────────

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "service": "loom", "db": resolved_path}

    # ── Semantic memory ───────────────────────────────────────────────────

    @app.get("/memory/semantic", tags=["memory"])
    async def list_semantic(limit: int = Query(default=20, ge=1, le=200)):
        mem = SemanticMemory(app.state.db)
        entries = await mem.list_recent(limit)
        return [
            {
                "key": e.key,
                "value": e.value,
                "confidence": e.confidence,
                "source": e.source,
                "updated_at": e.updated_at.isoformat(),
            }
            for e in entries
        ]

    @app.post("/memory/semantic", status_code=201, tags=["memory"])
    async def upsert_semantic(body: SemanticBody):
        mem = SemanticMemory(app.state.db)
        entry = SemanticEntry(
            key=body.key,
            value=body.value,
            confidence=body.confidence,
            source=body.source,
        )
        await mem.upsert(entry)
        return {"ok": True, "key": body.key}

    # ── Relational memory ─────────────────────────────────────────────────

    @app.get("/memory/relational", tags=["memory"])
    async def query_relational(
        subject: str | None = Query(default=None),
        predicate: str | None = Query(default=None),
    ):
        mem = RelationalMemory(app.state.db)
        entries = await mem.query(
            subject=subject or None,
            predicate=predicate or None,
        )
        return [
            {
                "subject": e.subject,
                "predicate": e.predicate,
                "object": e.object,
                "confidence": e.confidence,
                "source": e.source,
                "updated_at": e.updated_at.isoformat(),
            }
            for e in entries
        ]

    @app.post("/memory/relational", status_code=201, tags=["memory"])
    async def upsert_relational(body: RelationalBody):
        mem = RelationalMemory(app.state.db)
        entry = RelationalEntry(
            subject=body.subject,
            predicate=body.predicate,
            object=body.object,
            confidence=body.confidence,
            source=body.source,
        )
        await mem.upsert(entry)
        return {"ok": True, "subject": body.subject, "predicate": body.predicate}

    @app.delete("/memory/relational", tags=["memory"])
    async def delete_relational(subject: str, predicate: str):
        mem = RelationalMemory(app.state.db)
        deleted = await mem.delete(subject, predicate)
        if not deleted:
            raise HTTPException(status_code=404, detail="Entry not found")
        return {"ok": True, "deleted": True}

    # ── Webhook reply ─────────────────────────────────────────────────────

    @app.post("/webhook/reply", tags=["autonomy"])
    async def webhook_reply(body: WebhookReplyBody):
        notifier = app.state.webhook_notifier
        if notifier is None:
            raise HTTPException(
                status_code=503,
                detail="No WebhookNotifier configured on this server instance.",
            )
        result = (
            ConfirmResult.APPROVED
            if body.result.lower() == "approved"
            else ConfirmResult.DENIED
        )
        notifier.push_reply(body.notification_id, result)
        return {"ok": True, "notification_id": body.notification_id, "result": body.result}

    # ── Event emission ────────────────────────────────────────────────────

    @app.post("/events/emit", tags=["autonomy"])
    async def emit_event(body: EmitEventBody):
        evaluator = app.state.trigger_evaluator
        if evaluator is None:
            raise HTTPException(
                status_code=503,
                detail="No TriggerEvaluator configured on this server instance.",
            )
        fired = await evaluator.emit(body.event_name, body.context)
        return {"ok": True, "event_name": body.event_name, "fired_triggers": fired}

    return app


# ---------------------------------------------------------------------------
# Standalone runner (used by CLI command)
# ---------------------------------------------------------------------------

def run_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    db_path: str = "~/.loom/memory.db",
    reload: bool = False,
) -> None:
    """Start the Uvicorn server (blocking)."""
    app = create_app(db_path=db_path)
    uvicorn.run(app, host=host, port=port, reload=reload)
