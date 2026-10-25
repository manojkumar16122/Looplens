"""Governance engine for LoopLens.

Components:
* LoopDetector   - keeps the last N calls per session and detects identical calls repeated back-to-back.
* CostTracker    - converts tokens to USD and enforces COST_THRESHOLD_USD.
* GroqDiagnoser  - asks a Groq-hosted model to explain a detected loop (with a safe fallback).
* CacheManager   - serves stored results for identical calls within a session.

GovernanceEngine composes them. The API layer only talks to GovernanceEngine.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import database
from config import settings

logger = logging.getLogger("looplens.engine")

DIAGNOSIS_PROMPT = (
    "An AI agent is stuck in a loop. Here are the last {count} failed tool calls: {calls}. "
    "Provide a 1-sentence diagnosis of WHY it is looping and a 1-sentence suggestion on how "
    "the agent can break the loop."
)

PAUSED_REASON = (
    "Session paused: its cost limit was reached earlier. "
    "Tool calls stay blocked until the session is reset."
)


class SessionNotFoundError(LookupError):
    """Raised when an operation references a session that does not exist."""


def _usd(value: float) -> str:
    return f"${value:.4f}"


def canonical_arguments(arguments: dict[str, Any]) -> str:
    """Serialize arguments deterministically so key order never affects matching."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def build_signature(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Return (matching key, human-readable display) for a tool call."""
    args_json = canonical_arguments(arguments)
    return f"{tool_name}::{args_json}", f"{tool_name}({args_json})"


class LoopDetector:
    """Tracks recent calls per session and reports the current consecutive-identical streak."""

    def __init__(self, threshold: int, history_size: int) -> None:
        if history_size < threshold:
            raise ValueError("history_size must be >= threshold")
        self.threshold = threshold
        self._history_size = history_size
        self._histories: dict[str, deque[tuple[str, str]]] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, signature: str, display: str) -> int:
        """Record a call and return the length of the identical streak ending with it."""
        with self._lock:
            history = self._histories.setdefault(session_id, deque(maxlen=self._history_size))
            history.append((signature, display))
            streak = 0
            for sig, _ in reversed(history):
                if sig != signature:
                    break
                streak += 1
            return streak

    def is_loop(self, streak: int) -> bool:
        return streak >= self.threshold

    def last_calls(self, session_id: str, count: int) -> list[str]:
        with self._lock:
            history = list(self._histories.get(session_id, ()))
        return [display for _, display in history[-count:]]

    def reset(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id is None:
                self._histories.clear()
            else:
                self._histories.pop(session_id, None)


class CostTracker:
    """Turns token usage into dollars and checks them against the session budget."""

    def __init__(self, threshold_usd: float, price_per_1k_tokens_usd: float) -> None:
        self.threshold_usd = threshold_usd
        self.price_per_1k_tokens_usd = price_per_1k_tokens_usd

    def cost_for_tokens(self, tokens: int) -> float:
        return tokens / 1000 * self.price_per_1k_tokens_usd

    def would_exceed(self, projected_total_usd: float) -> bool:
        return projected_total_usd > self.threshold_usd


class CacheManager:
    """Per-session store of tool results keyed by the call signature."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def lookup(self, session_id: str, signature: str) -> tuple[bool, Any]:
        with self._lock:
            bucket = self._store.get(session_id, {})
            if signature in bucket:
                return True, bucket[signature]
            return False, None

    def store(self, session_id: str, signature: str, result: Any) -> None:
        with self._lock:
            self._store.setdefault(session_id, {})[signature] = result

    def clear(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id is None:
                self._store.clear()
            else:
                self._store.pop(session_id, None)


class GroqDiagnoser:
    """Explains loops using Groq. Never raises: falls back to a deterministic message on any failure."""

    def __init__(self, api_key: Optional[str], model: str) -> None:
        self._model = model
        self._client = None
        if api_key:
            try:
                from groq import Groq

                self._client = Groq(api_key=api_key, timeout=10.0)
            except Exception:  # missing package or invalid configuration
                logger.exception("Could not initialise Groq client; diagnoses will use the fallback")

    @property
    def available(self) -> bool:
        return self._client is not None

    def diagnose(self, last_calls: list[str]) -> str:
        if self._client is None:
            return self.fallback_diagnosis(last_calls)

        prompt = DIAGNOSIS_PROMPT.format(
            count=len(last_calls),
            calls="\n".join(f"- {call}" for call in last_calls),
        )
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=200,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
            logger.warning("Groq returned an empty diagnosis")
        except Exception:
            logger.exception("Groq diagnosis failed; using fallback")
        return self.fallback_diagnosis(last_calls)

    @staticmethod
    def fallback_diagnosis(last_calls: list[str]) -> str:
        tool = last_calls[-1].split("(", 1)[0] if last_calls else "a tool"
        return (
            f"Automatic diagnosis unavailable. The agent issued the identical {tool} call "
            f"{len(last_calls)} times in a row, which usually means it is not using the previous "
            "result to change its next step. Vary the arguments, switch tools, or escalate to a human."
        )


@dataclass(frozen=True)
class Decision:
    status: str
    reason: Optional[str] = None
    cached_result: Any = None
    alert_type: Optional[str] = None
    diagnosis: Optional[str] = None


class GovernanceEngine:
    """Evaluates each tool call against cost, loop and cache rules, in that order."""

    def __init__(
        self,
        detector: LoopDetector,
        cost_tracker: CostTracker,
        diagnoser: GroqDiagnoser,
        cache: CacheManager,
    ) -> None:
        self.detector = detector
        self.cost_tracker = cost_tracker
        self.diagnoser = diagnoser
        self.cache = cache
        # Serialises decisions so concurrent requests cannot race on session state.
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls) -> "GovernanceEngine":
        return cls(
            detector=LoopDetector(settings.loop_threshold, settings.loop_history_size),
            cost_tracker=CostTracker(settings.cost_threshold_usd, settings.price_per_1k_tokens_usd),
            diagnoser=GroqDiagnoser(settings.groq_api_key, settings.groq_model),
            cache=CacheManager(),
        )

    def reset_state(self) -> None:
        with self._lock:
            self.detector.reset()
            self.cache.clear()

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = database.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session {session_id} not found")
        return session

    def evaluate(self, session_id: str, tool_name: str, arguments: dict[str, Any], tokens: int = 0) -> Decision:
        signature, display = build_signature(tool_name, arguments)
        args_json = canonical_arguments(arguments)
        cost_delta = self.cost_tracker.cost_for_tokens(tokens)

        with self._lock:
            session = self._require_session(session_id)
            projected_cost = session["total_cost"] + cost_delta

            if session["status"] == database.SESSION_PAUSED:
                decision = Decision(database.CALL_BLOCKED, reason=PAUSED_REASON)

            elif self.cost_tracker.would_exceed(projected_cost):
                message = (
                    f"Cost limit reached: projected session cost {_usd(projected_cost)} exceeds "
                    f"COST_THRESHOLD_USD of {_usd(self.cost_tracker.threshold_usd)}. Session paused."
                )
                database.set_session_status(session_id, database.SESSION_PAUSED)
                decision = Decision(database.CALL_BLOCKED, reason=message, alert_type="COST", diagnosis=message)

            else:
                streak = self.detector.record(session_id, signature, display)
                if self.detector.is_loop(streak):
                    decision = self._loop_decision(session_id, tool_name, streak)
                else:
                    hit, cached = self.cache.lookup(session_id, signature)
                    if hit:
                        decision = Decision(
                            database.CALL_CACHED,
                            reason="Identical call found in cache; reuse cached_result instead of executing the tool.",
                            cached_result=cached,
                        )
                    else:
                        decision = Decision(database.CALL_ALLOWED)

            database.record_call(session_id, tool_name, args_json, tokens, cost_delta, decision.status)
            if decision.alert_type:
                database.insert_alert(session_id, decision.alert_type, decision.diagnosis or decision.reason or "")
            return decision

    def _loop_decision(self, session_id: str, tool_name: str, streak: int) -> Decision:
        if streak == self.detector.threshold:
            # First detection of this streak: ask Groq once and raise an alert.
            diagnosis = self.diagnoser.diagnose(self.detector.last_calls(session_id, self.detector.threshold))
            alert_type: Optional[str] = "LOOP"
        else:
            # The loop is still running; reuse the existing diagnosis instead of paying for another call.
            diagnosis = database.latest_alert_text(session_id, "LOOP") or "Loop still in progress."
            alert_type = None

        reason = (
            f"Loop detected: {tool_name} was called {streak} times in a row with identical arguments. "
            f"{diagnosis}"
        )
        return Decision(database.CALL_BLOCKED, reason=reason, alert_type=alert_type, diagnosis=diagnosis)

    def store_result(self, session_id: str, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        signature, _ = build_signature(tool_name, arguments)
        with self._lock:
            self._require_session(session_id)
            self.cache.store(session_id, signature, result)
