"""DuckDB storage: schema, upserts, and read helpers.

Timestamps: `start_date` is stored as naive UTC; `start_date_local` is the
athlete's wall-clock time (Strava suffixes it with "Z", but it is local).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id                      BIGINT PRIMARY KEY,
    name                    VARCHAR,
    sport_type              VARCHAR,
    start_date              TIMESTAMP,
    start_date_local        TIMESTAMP,
    distance_m              DOUBLE,
    moving_time_s           INTEGER,
    elapsed_time_s          INTEGER,
    total_elevation_gain_m  DOUBLE,
    average_speed_mps       DOUBLE,
    average_heartrate       DOUBLE,
    max_heartrate           DOUBLE,
    has_heartrate           BOOLEAN,
    workout_type            INTEGER,
    raw                     JSON,
    synced_at               TIMESTAMP
);

-- Raw per-second HR streams. Empty lists mark "fetched, no HR data".
CREATE TABLE IF NOT EXISTS hr_streams (
    activity_id  BIGINT PRIMARY KEY,
    time_s       INTEGER[],
    heartrate    INTEGER[],
    fetched_at   TIMESTAMP
);

-- Athlete HR zones from Strava; max_bpm = -1 means open-ended.
CREATE TABLE IF NOT EXISTS hr_zones (
    zone     INTEGER PRIMARY KEY,
    min_bpm  INTEGER,
    max_bpm  INTEGER
);
"""

ACTIVITY_COLUMNS = [
    "id",
    "name",
    "sport_type",
    "start_date",
    "start_date_local",
    "distance_m",
    "moving_time_s",
    "elapsed_time_s",
    "total_elevation_gain_m",
    "average_speed_mps",
    "average_heartrate",
    "max_heartrate",
    "has_heartrate",
    "workout_type",
    "raw",
    "synced_at",
]


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:" and not read_only:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def _utc_naive(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)


def _wall_clock(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def activity_row(a: dict, synced_at: datetime) -> tuple:
    return (
        a["id"],
        a.get("name"),
        a.get("sport_type") or a.get("type"),
        _utc_naive(a["start_date"]),
        _wall_clock(a.get("start_date_local") or a["start_date"]),
        a.get("distance"),
        a.get("moving_time"),
        a.get("elapsed_time"),
        a.get("total_elevation_gain"),
        a.get("average_speed"),
        a.get("average_heartrate"),
        a.get("max_heartrate"),
        bool(a.get("has_heartrate")),
        a.get("workout_type"),
        json.dumps(a),
        synced_at,
    )


def upsert_activities(con: duckdb.DuckDBPyConnection, activities: list[dict]) -> int:
    if not activities:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    placeholders = ", ".join("?" * len(ACTIVITY_COLUMNS))
    con.executemany(
        f"INSERT OR REPLACE INTO activities ({', '.join(ACTIVITY_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [activity_row(a, now) for a in activities],
    )
    return len(activities)


def latest_start_epoch(con: duckdb.DuckDBPyConnection) -> int | None:
    (latest,) = con.execute("SELECT max(start_date) FROM activities").fetchone()
    if latest is None:
        return None
    return int(latest.replace(tzinfo=UTC).timestamp())


def save_hr_zones(con: duckdb.DuckDBPyConnection, zones: list[dict]) -> None:
    con.execute("DELETE FROM hr_zones")
    con.executemany(
        "INSERT INTO hr_zones VALUES (?, ?, ?)",
        [(i + 1, z["min"], z["max"]) for i, z in enumerate(zones)],
    )


def load_hr_zones(con: duckdb.DuckDBPyConnection) -> list[tuple[int, int]]:
    return [
        (lo, hi)
        for lo, hi in con.execute("SELECT min_bpm, max_bpm FROM hr_zones ORDER BY zone").fetchall()
    ]


def activities_missing_streams(con: duckdb.DuckDBPyConnection, limit: int) -> list[int]:
    rows = con.execute(
        """
        SELECT a.id FROM activities a
        LEFT JOIN hr_streams s ON s.activity_id = a.id
        WHERE a.has_heartrate AND s.activity_id IS NULL
        ORDER BY a.start_date DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [r[0] for r in rows]


def save_hr_stream(
    con: duckdb.DuckDBPyConnection, activity_id: int, time_s: list[int], heartrate: list[int]
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO hr_streams VALUES (?, ?, ?, ?)",
        [activity_id, time_s, heartrate, datetime.now(UTC).replace(tzinfo=None)],
    )


def load_activities(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        f"SELECT {', '.join(c for c in ACTIVITY_COLUMNS if c != 'raw')} "
        "FROM activities ORDER BY start_date"
    ).df()


def load_hr_streams(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        "SELECT activity_id, time_s, heartrate FROM hr_streams WHERE len(heartrate) > 0"
    ).df()
