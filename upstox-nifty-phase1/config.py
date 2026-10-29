from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IST = ZoneInfo("Asia/Kolkata")

def _as_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a valid integer.") from exc

@dataclass(frozen=True)
class Settings:
    access_token: str
    instrument_keys: tuple[str, ...]
    feed_mode: str
    db_path: Path
    host: str
    port: int
    debug: bool
    reconnect_min_seconds: int
    reconnect_max_seconds: int
    ui_tick_limit: int
    ui_candle_limit: int

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.instrument_keys)

def load_settings() -> Settings:
    access_token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

    instrument_keys = tuple(
        value.strip()
        for value in os.getenv("UPSTOX_INSTRUMENT_KEYS", "").split(",")
        if value.strip()
    )

    feed_mode = os.getenv("UPSTOX_FEED_MODE", "full").strip().lower()
    supported_modes = {"ltpc", "full", "full_d30"}
    if feed_mode not in supported_modes:
        raise RuntimeError(
            "UPSTOX_FEED_MODE must be one of: ltpc, full, full_d30."
        )

    database_name = os.getenv("DATABASE_NAME", "market_data.db").strip()
    database_path = Path(database_name)
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path

    debug_value = os.getenv("FLASK_DEBUG", "false").strip().lower()

    return Settings(
        access_token=access_token,
        instrument_keys=instrument_keys,
        feed_mode=feed_mode,
        db_path=database_path,
        host=os.getenv("HOST", "127.0.0.1").strip(),
        port=_as_int("PORT", 5000),
        debug=debug_value in {"1", "true", "yes", "on"},
        reconnect_min_seconds=max(1, _as_int("RECONNECT_MIN_SECONDS", 2)),
        reconnect_max_seconds=max(5, _as_int("RECONNECT_MAX_SECONDS", 30)),
        ui_tick_limit=max(10, _as_int("UI_TICK_LIMIT", 100)),
        ui_candle_limit=max(10, _as_int("UI_CANDLE_LIMIT", 120)),
    )