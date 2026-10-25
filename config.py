"""Application configuration, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(ValueError):
    """Raised when an environment variable holds an invalid value."""


def _get_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    groq_api_key: Optional[str]
    groq_model: str
    cost_threshold_usd: float
    price_per_1k_tokens_usd: float
    loop_threshold: int
    loop_history_size: int
    db_path: Path


def load_settings() -> Settings:
    loop_threshold = _get_int("LOOP_THRESHOLD", 3, minimum=2)
    db_path = Path(os.getenv("LOOPLENS_DB_PATH", "looplens.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    cost_threshold = _get_float("COST_THRESHOLD_USD", 0.05)
    if cost_threshold <= 0:
        raise ConfigError("COST_THRESHOLD_USD must be greater than zero")

    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
        groq_model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip(),
        cost_threshold_usd=cost_threshold,
        price_per_1k_tokens_usd=_get_float("PRICE_PER_1K_TOKENS_USD", 0.002),
        loop_threshold=loop_threshold,
        loop_history_size=_get_int("LOOP_HISTORY_SIZE", 10, minimum=loop_threshold),
        db_path=db_path,
    )


settings = load_settings()
