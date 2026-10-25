"""Pydantic request and response models for the LoopLens API."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ARGUMENT_BYTES = 32 * 1024
MAX_RESULT_BYTES = 256 * 1024
TOOL_NAME_PATTERN = r"^[A-Za-z0-9_.\-]{1,100}$"


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


# ---------- Requests ----------

class ToolCallRequest(BaseModel):
    """Sent by an agent before it executes a tool."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=64)
    tool_name: str = Field(..., pattern=TOOL_NAME_PATTERN)
    arguments: dict[str, Any] = Field(default_factory=dict)
    tokens: int = Field(default=0, ge=0, le=10_000_000,
                        description="Tokens consumed by the agent step that produced this call.")

    @field_validator("arguments")
    @classmethod
    def _limit_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _json_size(value) > MAX_ARGUMENT_BYTES:
            raise ValueError(f"arguments must be under {MAX_ARGUMENT_BYTES} bytes when serialized")
        return value


class ToolResultRequest(BaseModel):
    """Sent by an agent after a tool executed, so identical future calls can be served from cache."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=64)
    tool_name: str = Field(..., pattern=TOOL_NAME_PATTERN)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = Field(...)

    @field_validator("result")
    @classmethod
    def _limit_result(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("result must not be null")
        if _json_size(value) > MAX_RESULT_BYTES:
            raise ValueError(f"result must be under {MAX_RESULT_BYTES} bytes when serialized")
        return value


# ---------- Responses ----------

class SessionCreated(BaseModel):
    session_id: str
    status: str
    start_time: str


class ToolCallResponse(BaseModel):
    status: Literal["allowed", "blocked", "cached"]
    reason: Optional[str] = None
    cached_result: Any = None
    session_status: str
    total_tokens: int
    total_cost_usd: float


class ToolResultResponse(BaseModel):
    status: Literal["stored"]


class SessionStats(BaseModel):
    session_id: str
    status: str
    start_time: str
    total_tokens: int
    total_cost_usd: float
    total_calls: int
    blocked_calls: int


class CallRecord(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    tokens: int
    status: str
    timestamp: str


class AlertRecord(BaseModel):
    alert_type: str
    diagnosis: str
    timestamp: str


class TokenPoint(BaseModel):
    call_index: int
    cumulative_tokens: int


class DashboardResponse(BaseModel):
    session: Optional[SessionStats]
    cost_threshold_usd: float
    recent_calls: list[CallRecord]
    alerts: list[AlertRecord]
    token_series: list[TokenPoint]


class ResetResponse(BaseModel):
    status: Literal["reset"]
