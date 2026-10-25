"""FastAPI application: the LoopLens governance API plus the static dashboard."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import database
from config import settings
from engine import GovernanceEngine, SessionNotFoundError
from models import (
    AlertRecord,
    CallRecord,
    DashboardResponse,
    ResetResponse,
    SessionCreated,
    SessionStats,
    TokenPoint,
    ToolCallRequest,
    ToolCallResponse,
    ToolResultRequest,
    ToolResultResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("looplens.api")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

database.init_db()
governance = GovernanceEngine.from_settings()

app = FastAPI(
    title="LoopLens",
    version="1.0.0",
    summary="Observability and governance middleware for AI agents.",
)


@app.exception_handler(SessionNotFoundError)
async def _session_not_found(_request, exc: SessionNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/session", response_model=SessionCreated, status_code=201)
def create_session() -> SessionCreated:
    session = database.create_session(uuid.uuid4().hex)
    return SessionCreated(session_id=session["id"], status=session["status"], start_time=session["start_time"])


@app.post("/tool_call", response_model=ToolCallResponse)
def tool_call(payload: ToolCallRequest) -> ToolCallResponse:
    """Called by the agent before it executes a tool. Returns allowed, blocked or cached."""
    decision = governance.evaluate(payload.session_id, payload.tool_name, payload.arguments, payload.tokens)
    session = database.get_session(payload.session_id)
    if session is None:
        raise SessionNotFoundError(f"Session {payload.session_id} not found")
    return ToolCallResponse(
        status=decision.status,
        reason=decision.reason,
        cached_result=decision.cached_result,
        session_status=session["status"],
        total_tokens=session["total_tokens"],
        total_cost_usd=round(session["total_cost"], 6),
    )


@app.post("/tool_result", response_model=ToolResultResponse)
def tool_result(payload: ToolResultRequest) -> ToolResultResponse:
    """Called by the agent after a tool executes, so identical future calls can be served from cache."""
    governance.store_result(payload.session_id, payload.tool_name, payload.arguments, payload.result)
    return ToolResultResponse(status="stored")


@app.get("/dashboard", response_model=DashboardResponse)
def dashboard(
    session_id: Optional[str] = Query(default=None, min_length=1, max_length=64),
) -> DashboardResponse:
    """Session stats, recent calls, alerts and token series. Defaults to the most recent session."""
    target_id = session_id or database.get_latest_session_id()
    if target_id is None:
        return DashboardResponse(
            session=None,
            cost_threshold_usd=settings.cost_threshold_usd,
            recent_calls=[],
            alerts=[],
            token_series=[],
        )

    session = database.get_session(target_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {target_id} not found")

    total_calls, blocked_calls = database.count_calls(target_id)

    series: list[TokenPoint] = []
    cumulative = 0
    for index, tokens in enumerate(database.get_token_series(target_id), start=1):
        cumulative += tokens
        series.append(TokenPoint(call_index=index, cumulative_tokens=cumulative))

    return DashboardResponse(
        session=SessionStats(
            session_id=session["id"],
            status=session["status"],
            start_time=session["start_time"],
            total_tokens=session["total_tokens"],
            total_cost_usd=round(session["total_cost"], 6),
            total_calls=total_calls,
            blocked_calls=blocked_calls,
        ),
        cost_threshold_usd=settings.cost_threshold_usd,
        recent_calls=[
            CallRecord(
                tool_name=row["tool_name"],
                arguments=_parse_arguments(row["arguments"]),
                tokens=row["tokens"],
                status=row["status"],
                timestamp=row["timestamp"],
            )
            for row in database.get_recent_calls(target_id, limit=50)
        ],
        alerts=[
            AlertRecord(alert_type=row["alert_type"], diagnosis=row["diagnosis"], timestamp=row["timestamp"])
            for row in database.get_alerts(target_id)
        ],
        token_series=series,
    )


@app.post("/reset", response_model=ResetResponse)
def reset() -> ResetResponse:
    """Wipe all sessions, calls and alerts, and clear in-memory detector and cache state."""
    database.reset_db()
    governance.reset_state()
    return ResetResponse(status="reset")


# Mounted last so API routes take precedence over static files.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
