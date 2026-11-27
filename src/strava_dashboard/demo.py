"""Synthetic training data so the dashboard can be explored without a Strava account.

Writes Strava-shaped activity dicts through the same `db` functions a real sync
uses: a weekly plan of easy runs, a workout, a long run, and an occasional 5K
race, with fitness slowly improving over time.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
import numpy as np

from . import db
from .metrics import default_hr_zones

MAX_HR = 190

# weekday -> (kind, base distance km, base pace min/km, avg HR)
WEEKLY_PLAN = {
    1: ("Tempo", 8.0, 4.9, 157),
    2: ("Easy", 6.0, 5.9, 122),
    3: ("Intervals", 9.0, 5.1, 147),
    5: ("Easy", 7.0, 5.8, 123),
    6: ("Long run", 16.0, 5.7, 125),
}


def _activity(
    rng: np.random.Generator,
    activity_id: int,
    day: date,
    kind: str,
    km: float,
    pace: float,
    hr: float,
):
    moving = int(km * pace * 60)
    start_local = datetime.combine(day, time(7, int(rng.integers(0, 50))))
    start_utc = start_local.replace(tzinfo=UTC) + timedelta(hours=5)
    activity = {
        "id": activity_id,
        "name": f"{kind}",
        "sport_type": "Run",
        "type": "Run",
        "start_date": start_utc.isoformat().replace("+00:00", "Z"),
        "start_date_local": start_local.isoformat() + "Z",
        "distance": round(km * 1000, 1),
        "moving_time": moving,
        "elapsed_time": moving + int(rng.integers(0, 180)),
        "total_elevation_gain": round(float(rng.uniform(10, 120)), 1),
        "average_speed": round(km * 1000 / moving, 3),
        "average_heartrate": round(hr, 1),
        "max_heartrate": round(min(MAX_HR, hr + rng.uniform(8, 18)), 1),
        "has_heartrate": True,
        "workout_type": 1 if kind == "5K race" else 0,
    }
    t = np.arange(0, moving, 5)
    drift = np.linspace(-6, 6, len(t))  # cardiac drift
    wobble = 4 * np.sin(t / 90) + rng.normal(0, 3, len(t))
    stream_hr = np.clip(hr + drift + wobble, 80, MAX_HR).round().astype(int)
    return activity, t.tolist(), stream_hr.tolist()


def generate_demo(
    con: duckdb.DuckDBPyConnection,
    days: int = 210,
    end: date | None = None,
    seed: int = 7,
) -> int:
    rng = np.random.default_rng(seed)
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    activities, streams = [], []
    for offset in range(days):
        day = start + timedelta(days=offset)
        progress = offset / days  # 0 -> 1 over the period
        fitness = 1 - 0.07 * progress  # paces get ~7% faster
        volume = 0.75 + 0.45 * progress
        if offset % 7 == 0 and (offset // 7) % 4 == 3:
            volume *= 0.7  # cutback week
        if day.weekday() == 5 and (offset // 7) % 6 == 5:
            kind, km, pace, hr = "5K race", 5.02, 4.45, 178
        elif day.weekday() in WEEKLY_PLAN and rng.random() > 0.08:
            kind, km, pace, hr = WEEKLY_PLAN[day.weekday()]
            km = km * volume * rng.uniform(0.92, 1.08)
        else:
            continue
        pace = pace * fitness * rng.uniform(0.97, 1.03)
        activity, t, h = _activity(rng, len(activities) + 1, day, kind, km, pace, hr)
        activities.append(activity)
        streams.append((activity["id"], t, h))

    db.upsert_activities(con, activities)
    zones = default_hr_zones(MAX_HR)
    db.save_hr_zones(con, [{"min": lo, "max": hi} for lo, hi in zones])
    for activity_id, t, h in streams:
        db.save_hr_stream(con, activity_id, t, h)
    return len(activities)


def ensure_fresh_demo(path: Path, today: date | None = None) -> bool:
    """(Re)build the demo database at `path` unless it was already built today.

    The synthetic runs end on the day they're generated, so a long-running hosted
    demo regenerates daily to keep "Last 12 weeks" populated. The new file is
    written next to the old one and swapped in, so readers never see half a file.
    Returns True if the database was (re)generated.
    """
    today = today or date.today()
    if path.exists() and date.fromtimestamp(path.stat().st_mtime) >= today:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    con = db.connect(tmp)
    try:
        generate_demo(con, end=today)
    finally:
        con.close()
    os.replace(tmp, path)
    return True
