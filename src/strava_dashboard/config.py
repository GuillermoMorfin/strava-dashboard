"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_UNITS = ("mi", "km")
# Same bounds as the dashboard's heart-rate inputs.
MAX_HR_RANGE = (120, 230)
RESTING_HR_RANGE = (30, 100)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _optional_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _check_heart_rates(max_hr: int | None, resting_hr: int) -> None:
    lo, hi = RESTING_HR_RANGE
    if not lo <= resting_hr <= hi:
        raise ConfigError(f"RESTING_HR must be between {lo} and {hi} bpm, got {resting_hr}")
    if max_hr is None:
        return
    lo, hi = MAX_HR_RANGE
    if not lo <= max_hr <= hi:
        hint = " (are MAX_HR and RESTING_HR swapped?)" if max_hr < resting_hr else ""
        raise ConfigError(f"MAX_HR must be between {lo} and {hi} bpm, got {max_hr}{hint}")


@dataclass(frozen=True)
class Settings:
    client_id: str = ""
    client_secret: str = ""
    redirect_port: int = 8765
    db_path: Path = Path("data/strava.duckdb")
    token_path: Path = Path(".strava_tokens.json")
    units: str = "mi"
    max_hr: int | None = None
    resting_hr: int = 60

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"

    def require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set. "
                "Copy .env.example to .env and fill them in."
            )

    @classmethod
    def from_env(cls, dotenv: bool = True) -> Settings:
        if dotenv:
            load_dotenv()
        units = os.environ.get("STRAVA_UNITS", "mi").strip().lower() or "mi"
        if units not in VALID_UNITS:
            raise ConfigError(f"STRAVA_UNITS must be one of {VALID_UNITS}, got {units!r}")
        max_hr = _optional_int("MAX_HR")
        resting_hr = _optional_int("RESTING_HR") or 60
        _check_heart_rates(max_hr, resting_hr)
        return cls(
            client_id=os.environ.get("STRAVA_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("STRAVA_CLIENT_SECRET", "").strip(),
            redirect_port=_optional_int("STRAVA_REDIRECT_PORT") or 8765,
            db_path=Path(os.environ.get("STRAVA_DB_PATH") or "data/strava.duckdb"),
            token_path=Path(os.environ.get("STRAVA_TOKEN_PATH") or ".strava_tokens.json"),
            units=units,
            max_hr=max_hr,
            resting_hr=resting_hr,
        )
