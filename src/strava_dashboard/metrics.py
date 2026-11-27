"""Training metrics computed from the activities / hr_streams tables.

All functions are pure: they take DataFrames (as returned by `db.load_*`) and
return DataFrames/Series, so they are easy to test and reuse.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

METERS_PER_UNIT = {"mi": 1609.344, "km": 1000.0}

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)):
        return "–"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_pace(minutes_per_unit: float | None) -> str:
    if minutes_per_unit is None or math.isnan(minutes_per_unit):
        return "–"
    return format_duration(minutes_per_unit * 60)


# ---------------------------------------------------------------------------
# Volume and pace
# ---------------------------------------------------------------------------


def _valid_runs(activities: pd.DataFrame) -> pd.DataFrame:
    return activities[(activities["distance_m"] > 0) & (activities["moving_time_s"] > 0)]


def week_start(ts: pd.Series) -> pd.Series:
    """Monday 00:00 of each timestamp's week."""
    return ts.dt.to_period("W-SUN").dt.start_time


def weekly_distance(activities: pd.DataFrame, units: str = "mi") -> pd.DataFrame:
    """Distance, run count, hours and long run per Monday-start week, gaps filled with 0."""
    columns = ["week_start", "distance", "runs", "hours", "long_run"]
    runs = _valid_runs(activities)
    if runs.empty:
        return pd.DataFrame(columns=columns)
    per_unit = METERS_PER_UNIT[units]
    weekly = (
        runs.assign(week_start=week_start(runs["start_date_local"]))
        .groupby("week_start")
        .agg(
            distance=("distance_m", lambda s: s.sum() / per_unit),
            runs=("id", "count"),
            hours=("moving_time_s", lambda s: s.sum() / 3600),
            long_run=("distance_m", lambda s: s.max() / per_unit),
        )
    )
    full_range = pd.date_range(weekly.index.min(), weekly.index.max(), freq="W-MON")
    weekly = weekly.reindex(full_range, fill_value=0)
    weekly.index.name = "week_start"
    return weekly.reset_index()[columns]


def pace_trend(
    activities: pd.DataFrame,
    units: str = "mi",
    window: int = 10,
    min_distance_m: float = 1000,
) -> pd.DataFrame:
    """Per-run pace (minutes per unit) plus a rolling median over `window` runs."""
    runs = _valid_runs(activities)
    runs = runs[runs["distance_m"] >= min_distance_m].sort_values("start_date_local")
    per_unit = METERS_PER_UNIT[units]
    out = pd.DataFrame(
        {
            "id": runs["id"],
            "start_date_local": runs["start_date_local"],
            "name": runs["name"],
            "distance": runs["distance_m"] / per_unit,
            "pace": runs["moving_time_s"] / 60 / (runs["distance_m"] / per_unit),
            "average_heartrate": runs["average_heartrate"],
        }
    )
    out["rolling_pace"] = out["pace"].rolling(window, min_periods=1).median()
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Heart-rate zones
# ---------------------------------------------------------------------------

Zones = list[tuple[int, int]]  # [(min_bpm, max_bpm), ...]; max_bpm == -1 is open-ended


def default_hr_zones(max_hr: int) -> Zones:
    """Classic 5-zone model at 60/70/80/90% of max HR (used when Strava has no zones)."""
    bounds = [0] + [round(max_hr * p) for p in (0.6, 0.7, 0.8, 0.9)]
    return [(lo, hi) for lo, hi in zip(bounds, bounds[1:] + [-1], strict=True)]


def zone_indices(heartrate: np.ndarray, zones: Zones) -> np.ndarray:
    """0-based zone index for each HR sample (zones assumed contiguous, ascending)."""
    lower_bounds = np.array([lo for lo, _ in zones[1:]])
    return np.searchsorted(lower_bounds, heartrate, side="right")


def time_in_zones(
    time_s: list[int] | np.ndarray,
    heartrate: list[int] | np.ndarray,
    zones: Zones,
    max_gap_s: float = 30,
) -> np.ndarray:
    """Seconds spent in each zone. Gaps longer than `max_gap_s` (pauses) are ignored."""
    t = np.asarray(time_s, dtype=float)
    hr = np.asarray(heartrate, dtype=float)
    if len(t) < 2:
        return np.zeros(len(zones))
    dt = np.diff(t)
    dt[(dt < 0) | (dt > max_gap_s)] = 0
    idx = zone_indices(hr[:-1], zones)
    return np.bincount(idx, weights=dt, minlength=len(zones))[: len(zones)]


def zone_time_by_activity(
    streams: pd.DataFrame, activities: pd.DataFrame, zones: Zones
) -> pd.DataFrame:
    """Long-form table: one row per (activity, zone) with seconds and week_start."""
    columns = ["activity_id", "start_date_local", "week_start", "zone", "seconds"]
    if streams.empty or not zones:
        return pd.DataFrame(columns=columns)
    rows = []
    for activity_id, t, hr in streams[["activity_id", "time_s", "heartrate"]].itertuples(
        index=False
    ):
        for zone, secs in enumerate(time_in_zones(t, hr, zones), start=1):
            rows.append((activity_id, zone, secs))
    long = pd.DataFrame(rows, columns=["activity_id", "zone", "seconds"])
    meta = activities[["id", "start_date_local"]].rename(columns={"id": "activity_id"})
    long = long.merge(meta, on="activity_id", how="inner")
    long["week_start"] = week_start(long["start_date_local"])
    return long[columns]


INTENSITY_LEVELS = ("easy", "moderate", "hard")  # Z1-2, Z3, Z4-5


def intensity_distribution(
    zone_time: pd.DataFrame, freq: str = "W", min_hours: float = 0.5
) -> pd.DataFrame:
    """Share of heart-rate time that was easy (Z1-2), moderate (Z3) or hard (Z4+) per period.

    `zone_time` is the output of `zone_time_by_activity`. `freq` is "W" (Monday-start
    weeks) or "M" (calendar months). Periods with less than `min_hours` of HR data are
    dropped, because a single run would swing the shares. Returns one row per period with
    the three shares (summing to 1) and the hours of HR data behind them.
    """
    columns = ["period_start", *INTENSITY_LEVELS, "hours"]
    if freq not in ("W", "M"):
        raise ValueError("freq must be 'W' or 'M'")
    if zone_time.empty:
        return pd.DataFrame(columns=columns)
    dates = zone_time["start_date_local"]
    period = week_start(dates) if freq == "W" else dates.dt.to_period("M").dt.start_time
    level = np.select(
        [zone_time["zone"] <= 2, zone_time["zone"] == 3], ["easy", "moderate"], "hard"
    )
    seconds = (
        zone_time.assign(period_start=period.values, level=level)
        .pivot_table(
            index="period_start", columns="level", values="seconds", aggfunc="sum", fill_value=0
        )
        .reindex(columns=list(INTENSITY_LEVELS), fill_value=0)
    )
    total = seconds.sum(axis=1)
    out = seconds.div(total.where(total > 0), axis=0)
    out["hours"] = total / 3600
    out = out[out["hours"] >= min_hours]
    out.columns.name = None
    return out.reset_index()[columns]


# ---------------------------------------------------------------------------
# Aerobic efficiency
# ---------------------------------------------------------------------------


def aerobic_efficiency(
    activities: pd.DataFrame,
    zones: Zones,
    units: str = "km",
    *,
    road_only: bool = True,
    min_minutes: float = 20,
    window: int = 10,
) -> pd.DataFrame:
    """Pace and efficiency of steady aerobic runs, to see whether the base is improving.

    Keeps runs of at least `min_minutes` whose average HR falls inside zone 2
    (`zones[1]`), dropping trail runs when `road_only` (hills distort pace). For each run:
    pace (min per unit) and efficiency factor, EF = speed (m/min) / average HR, i.e. meters
    covered per heartbeat. Higher EF or faster pace at the same HR means better aerobic
    fitness. Rolling medians over `window` runs smooth out weather, terrain and fatigue.
    """
    columns = [
        "id",
        "start_date_local",
        "name",
        "distance",
        "average_heartrate",
        "pace",
        "ef",
        "rolling_pace",
        "rolling_ef",
    ]
    if len(zones) < 2:
        return pd.DataFrame(columns=columns)
    lo, hi = zones[1]
    hi = math.inf if hi == -1 else hi
    runs = _valid_runs(activities)
    hr = runs["average_heartrate"]
    keep = (runs["moving_time_s"] >= min_minutes * 60) & (hr >= lo) & (hr < hi)
    if road_only and "sport_type" in runs:
        keep &= runs["sport_type"] != "TrailRun"
    runs = runs[keep].sort_values("start_date_local")
    if runs.empty:
        return pd.DataFrame(columns=columns)
    per_unit = METERS_PER_UNIT[units]
    speed = runs["distance_m"] / (runs["moving_time_s"] / 60)  # m/min
    out = pd.DataFrame(
        {
            "id": runs["id"],
            "start_date_local": runs["start_date_local"],
            "name": runs["name"],
            "distance": runs["distance_m"] / per_unit,
            "average_heartrate": runs["average_heartrate"],
            "pace": per_unit / speed,
            "ef": speed / runs["average_heartrate"],
        }
    )
    out["rolling_pace"] = out["pace"].rolling(window, min_periods=1).median()
    out["rolling_ef"] = out["ef"].rolling(window, min_periods=1).median()
    return out.reset_index(drop=True)[columns]


# ---------------------------------------------------------------------------
# Training load and acute:chronic workload ratio
# ---------------------------------------------------------------------------

LOAD_METHODS = ("trimp", "distance", "duration")


def banister_trimp(
    duration_min: np.ndarray | float,
    avg_hr: np.ndarray | float,
    resting_hr: float,
    max_hr: float,
    k: float = 1.92,
) -> np.ndarray | float:
    """Banister TRIMP: minutes x HRr x 0.64 x e^(k x HRr). k=1.92 (men), 1.67 (women)."""
    hrr = np.clip((np.asarray(avg_hr) - resting_hr) / (max_hr - resting_hr), 0, 1)
    return duration_min * hrr * 0.64 * np.exp(k * hrr)


def activity_load(
    activities: pd.DataFrame,
    method: str = "trimp",
    *,
    units: str = "mi",
    max_hr: float | None = None,
    resting_hr: float = 60,
) -> pd.DataFrame:
    """Per-activity load with an `estimated` flag.

    trimp: Banister TRIMP from average HR. Runs without HR are estimated from the
    median TRIMP-per-minute of runs with HR; with no HR at all, falls back to minutes.
    distance: distance in `units`. duration: moving minutes.
    """
    if method not in LOAD_METHODS:
        raise ValueError(f"method must be one of {LOAD_METHODS}")
    runs = _valid_runs(activities)
    minutes = runs["moving_time_s"] / 60
    estimated = pd.Series(False, index=runs.index)

    if method == "distance":
        load = runs["distance_m"] / METERS_PER_UNIT[units]
    elif method == "duration":
        load = minutes
    else:
        max_hr = max_hr or runs["max_heartrate"].max()
        has_hr = runs["average_heartrate"].notna()
        if not has_hr.any() or not max_hr or np.isnan(max_hr) or max_hr <= resting_hr:
            load, estimated = minutes, pd.Series(True, index=runs.index)
        else:
            load = pd.Series(np.nan, index=runs.index)
            load[has_hr] = banister_trimp(
                minutes[has_hr], runs.loc[has_hr, "average_heartrate"], resting_hr, max_hr
            )
            per_minute = (load[has_hr] / minutes[has_hr]).median()
            load[~has_hr] = minutes[~has_hr] * per_minute
            estimated = ~has_hr

    return pd.DataFrame(
        {
            "id": runs["id"],
            "date": runs["start_date_local"].dt.normalize(),
            "load": load.astype(float),
            "estimated": estimated,
        }
    ).reset_index(drop=True)


def daily_load(loads: pd.DataFrame, end: date | datetime | None = None) -> pd.Series:
    """Sum per-activity load by day, with rest days filled as 0 through `end`."""
    if loads.empty:
        return pd.Series(dtype=float, name="load")
    daily = loads.groupby("date")["load"].sum()
    last = max(daily.index.max(), pd.Timestamp(end).normalize()) if end else daily.index.max()
    daily = daily.reindex(pd.date_range(daily.index.min(), last, freq="D"), fill_value=0.0)
    daily.index.name = "date"
    return daily.rename("load")


ACWR_BANDS = [
    (0.0, 0.8, "Low"),
    (0.8, 1.3, "Optimal"),
    (1.3, 1.5, "Elevated"),
    (1.5, math.inf, "High"),
]


def acwr_band(ratio: float) -> str:
    if ratio is None or math.isnan(ratio):
        return "Insufficient data"
    for lo, hi, label in ACWR_BANDS:
        if lo <= ratio < hi:
            return label
    return "Low"


def acwr(
    daily: pd.Series,
    method: str = "ewma",
    acute_days: int = 7,
    chronic_days: int = 28,
) -> pd.DataFrame:
    """Acute and chronic load and their ratio.

    rolling: simple means over the last 7 / 28 days.
    ewma: exponentially weighted means with alpha = 2 / (N + 1) (Williams et al., 2017).
    The ratio is left blank until `chronic_days` of history exist.
    """
    if method not in ("rolling", "ewma"):
        raise ValueError("method must be 'rolling' or 'ewma'")
    if method == "rolling":
        acute = daily.rolling(acute_days, min_periods=acute_days).mean()
        chronic = daily.rolling(chronic_days, min_periods=chronic_days).mean()
    else:
        acute = daily.ewm(alpha=2 / (acute_days + 1), adjust=False).mean()
        chronic = daily.ewm(alpha=2 / (chronic_days + 1), adjust=False).mean()
    ratio = (acute / chronic.where(chronic > 0)).astype(float)
    ratio.iloc[: chronic_days - 1] = np.nan
    return pd.DataFrame({"load": daily, "acute": acute, "chronic": chronic, "ratio": ratio})


# ---------------------------------------------------------------------------
# Race predictions
# ---------------------------------------------------------------------------

RACE_DISTANCES = {
    "5K": 5000.0,
    "10K": 10000.0,
    "Half marathon": 21097.5,
    "Marathon": 42195.0,
}


def vdot(distance_m: float, time_s: float) -> float:
    """Daniels-Gilbert VDOT for a performance."""
    t = time_s / 60
    v = distance_m / t  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v**2
    pct_max = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct_max


def time_for_vdot(target_vdot: float, distance_m: float) -> float:
    """Inverse of `vdot`: the race time (s) at `distance_m` for a given VDOT (bisection)."""
    lo, hi = 60.0, 24 * 3600.0  # VDOT strictly decreases as time increases
    for _ in range(100):
        mid = (lo + hi) / 2
        if vdot(distance_m, mid) > target_vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def riegel(time_s: float, distance_m: float, target_m: float, exponent: float = 1.06) -> float:
    """Riegel endurance model: T2 = T1 x (D2 / D1) ^ exponent."""
    return time_s * (target_m / distance_m) ** exponent


def best_effort(
    activities: pd.DataFrame,
    as_of: datetime | None = None,
    lookback_days: int = 90,
    min_distance_m: float = 3000,
    max_speed_mps: float = 6.5,
) -> pd.Series | None:
    """The run with the highest VDOT in the lookback window (implausible GPS speeds excluded)."""
    as_of = pd.Timestamp(as_of or datetime.now())
    runs = _valid_runs(activities)
    window = runs[
        (runs["start_date_local"] > as_of - timedelta(days=lookback_days))
        & (runs["start_date_local"] <= as_of)
        & (runs["distance_m"] >= min_distance_m)
        & (runs["distance_m"] / runs["moving_time_s"] <= max_speed_mps)
    ]
    if window.empty:
        return None
    scores = [
        vdot(d, t) for d, t in zip(window["distance_m"], window["moving_time_s"], strict=True)
    ]
    best = window.iloc[int(np.argmax(scores))].copy()
    best["vdot"] = max(scores)
    return best


def race_predictions(effort: pd.Series) -> pd.DataFrame:
    """Predicted times for standard distances from one effort, by VDOT and Riegel."""
    rows = []
    for race, meters in RACE_DISTANCES.items():
        rows.append(
            {
                "race": race,
                "distance_m": meters,
                "vdot_time_s": time_for_vdot(effort["vdot"], meters),
                "riegel_time_s": riegel(effort["moving_time_s"], effort["distance_m"], meters),
            }
        )
    return pd.DataFrame(rows)
