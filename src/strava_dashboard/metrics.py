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
# Year-over-year comparison
# ---------------------------------------------------------------------------

PERIOD_COLUMNS = [
    "label",
    "start",
    "end",
    "runs",
    "distance_per_week",
    "avg_pace",
    "z2_pace",
    "z2_runs",
    "long_run",
]


def period_comparison(
    activities: pd.DataFrame,
    as_of: date | datetime | pd.Timestamp,
    *,
    months: int = 6,
    years: int = 3,
    units: str = "km",
    zones: Zones | None = None,
    road_only: bool = True,
) -> pd.DataFrame:
    """Compare the same window of `months` ending on `as_of`, this year and in earlier years.

    Row k covers the `months` up to `as_of` minus k years, so seasons line up. Volume
    (runs, distance per week, longest run) counts every run. Pace leaves out trail runs
    when `road_only`, because hills and terrain slow them down:
      - avg_pace: total moving time / total distance, so a long run counts more than a short one;
      - z2_pace: median pace of steady zone 2 runs (see `aerobic_efficiency`), the fairest
        comparison, since it compares runs at the same effort. Needs `zones`.
    """
    as_of = pd.Timestamp(as_of).normalize()
    per_unit = METERS_PER_UNIT[units]
    runs = _valid_runs(activities)
    rows = []
    for k in range(years):
        end = as_of - pd.DateOffset(years=k)
        start = end - pd.DateOffset(months=months)
        day = runs["start_date_local"].dt.normalize()
        window = runs[(day > start) & (day <= end)]
        paced = window
        if road_only and "sport_type" in window:
            paced = window[window["sport_type"] != "TrailRun"]
        weeks = (end - start).days / 7
        distance = paced["distance_m"].sum()
        z2 = (
            aerobic_efficiency(window, zones, units, road_only=road_only)
            if zones
            else pd.DataFrame()
        )
        rows.append(
            {
                "label": f"Last {months} months"
                if k == 0
                else f"{k} year{'s' if k > 1 else ''} ago",
                "start": start + pd.Timedelta(days=1),
                "end": end,
                "runs": len(window),
                "distance_per_week": window["distance_m"].sum() / per_unit / weeks,
                "avg_pace": (paced["moving_time_s"].sum() / 60) / (distance / per_unit)
                if distance
                else np.nan,
                "z2_pace": z2["pace"].median() if not z2.empty else np.nan,
                "z2_runs": len(z2),
                "long_run": window["distance_m"].max() / per_unit if len(window) else np.nan,
            }
        )
    return pd.DataFrame(rows, columns=PERIOD_COLUMNS)


def _pace_gap(now: float, then: float) -> str | None:
    """'12 s faster' / '5 s slower' per unit between two paces in minutes, or None."""
    if pd.isna(now) or pd.isna(then):
        return None
    seconds = round((then - now) * 60)
    if seconds == 0:
        return "the same as"
    return f"{abs(seconds)} s {'faster' if seconds > 0 else 'slower'} than"


def comparison_summary(cmp: pd.DataFrame, units: str = "km") -> str:
    """Average pace this period against the same period in earlier years, then zone 2 pace."""
    if cmp.empty or pd.isna(cmp["avg_pace"].iloc[0]):
        return "No runs in this period yet."
    now, earlier = cmp.iloc[0], cmp.iloc[1:]

    def gaps(column: str) -> list[str]:
        out = []
        for _, then in earlier.iterrows():
            gap = _pace_gap(now[column], then[column])
            if gap:
                out.append(f"{gap} {then['label']}")
        return out

    text = (
        f"Average pace over the {now['label'].lower()} was {format_pace(now['avg_pace'])} /{units}"
    )
    avg_gaps = gaps("avg_pace")
    text += (", " + " and ".join(avg_gaps) + ".") if avg_gaps else "."
    z2_gaps = gaps("z2_pace")
    if pd.notna(now["z2_pace"]) and z2_gaps:
        text += (
            f" At the same effort (zone 2 runs) it was {format_pace(now['z2_pace'])} /{units}, "
            + " and ".join(z2_gaps)
            + "."
        )
    return text


# ---------------------------------------------------------------------------
# Training load diagnosis
# ---------------------------------------------------------------------------

LOAD_ADVICE = {
    "Low": (
        "Fine for a taper or a recovery week. Over several weeks, load this low means "
        "losing fitness."
    ),
    "Optimal": "Load is in line with what your body is used to. Keep building gradually.",
    "Elevated": (
        "Fine if it was planned, like a workout block or race week. If not, make the next "
        "day or two easy or rest: the ratio drops quickly after a lighter day."
    ),
    "High": (
        "A big jump compared with your usual load. Take an easy or rest day and avoid "
        "stacking hard sessions until the ratio is back under 1.3."
    ),
    "Insufficient data": "The ratio needs at least 28 days of history.",
}


def _on_day(ts: pd.Timestamp, as_of: pd.Timestamp) -> str:
    """'today', 'yesterday' or 'on Thursday (Nov 26)'."""
    days = (as_of - ts.normalize()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"on {ts:%A} ({ts:%b} {ts.day})"


def load_diagnosis(
    activities: pd.DataFrame,
    acwr_df: pd.DataFrame,
    *,
    units: str = "km",
    method: str = "ewma",
) -> dict:
    """Explain the latest acute:chronic ratio in plain words.

    Looks at the 7 days up to the last day of `acwr_df` and the 4 weeks before them for:
    runs on consecutive days, an unusually hard session (average HR well above your usual),
    load bunched into the last 3 days, more distance than a usual week, a longer long run
    than usual, and a light week that lowered the 28-day base.
    Returns {"ratio", "band", "summary", "reasons", "advice"}.
    """
    if acwr_df.empty or acwr_df["ratio"].dropna().empty:
        band = "Insufficient data"
        return {
            "ratio": np.nan,
            "band": band,
            "summary": "",
            "reasons": [],
            "advice": LOAD_ADVICE[band],
        }

    as_of = acwr_df.index[-1].normalize()
    ratio = float(acwr_df["ratio"].iloc[-1])
    band = acwr_band(ratio)
    per_unit = METERS_PER_UNIT[units]

    runs = _valid_runs(activities).copy()
    runs["day"] = runs["start_date_local"].dt.normalize()
    runs["dist"] = runs["distance_m"] / per_unit
    week = runs[(runs["day"] > as_of - pd.Timedelta(days=7)) & (runs["day"] <= as_of)]
    before = runs[
        (runs["day"] > as_of - pd.Timedelta(days=35))
        & (runs["day"] <= as_of - pd.Timedelta(days=7))
    ]

    change = (ratio - 1) * 100
    if band == "Optimal":
        summary = (
            f"In the optimal range (ratio {ratio:.2f}): this week's load is close to "
            "what you're used to."
        )
    else:
        direction = "above" if change > 0 else "below"
        summary = (
            f"{band} (ratio {ratio:.2f}): your load over the last 7 days was {abs(change):.0f}% "
            f"{direction} what you're used to over the last 4 weeks."
        )

    reasons: list[str] = []

    # 1. Runs on consecutive days, ending today or yesterday.
    run_days = set(week["day"])
    last_run_day = max(run_days) if run_days else None
    if last_run_day is not None and (as_of - last_run_day).days <= 1:
        streak, d = 0, last_run_day
        while d in run_days:
            streak += 1
            d -= pd.Timedelta(days=1)
        if streak >= 3:
            first = last_run_day - pd.Timedelta(days=streak - 1)
            reasons.append(
                f"You ran {streak} days in a row, from {first:%A} to {last_run_day:%A}, "
                "with no rest day between."
            )

    # 2. An unusually hard session: average HR well above your usual runs.
    usual_hr = before["average_heartrate"].dropna()
    week_hr = week.dropna(subset=["average_heartrate"])
    if len(usual_hr) >= 5 and not week_hr.empty:
        hardest = week_hr.loc[week_hr["average_heartrate"].idxmax()]
        median = usual_hr.median()
        if hardest["average_heartrate"] >= max(usual_hr.quantile(0.8), median + 8):
            reasons.append(
                f"The {hardest['dist']:.1f} {units} {_on_day(hardest['start_date_local'], as_of)} "
                f"was a hard session: average HR {hardest['average_heartrate']:.0f} bpm, "
                f"against {median:.0f} bpm on a usual run."
            )

    # 3. Load bunched into the last 3 days (the EWMA model weights recent days most).
    last7 = acwr_df["load"].iloc[-7:]
    if last7.sum() > 0:
        share = acwr_df["load"].iloc[-3:].sum() / last7.sum()
        if share >= 0.6 and band in ("Elevated", "High"):
            note = (
                " The ratio weights recent days most, so it reacts quickly."
                if method == "ewma"
                else ""
            )
            reasons.append(f"{share:.0%} of this week's load came in the last 3 days.{note}")

    # 4. More distance than a usual week.
    if not before.empty:
        usual_week = before["dist"].sum() / 4
        this_week = week["dist"].sum()
        if usual_week > 0:
            diff = this_week / usual_week - 1
            if diff >= 0.15:
                reasons.append(
                    f"You ran {this_week:.0f} {units} in the last 7 days, {diff:.0%} more than "
                    f"your usual week ({usual_week:.0f} {units})."
                )
            elif diff <= -0.15 and band == "Low":
                reasons.append(
                    f"You ran {this_week:.0f} {units} in the last 7 days, {-diff:.0%} less than "
                    f"your usual week ({usual_week:.0f} {units})."
                )

        # 5. A longer long run than usual.
        weekly_long = before.groupby((as_of - before["day"]).dt.days // 7)["dist"].max()
        if not week.empty and len(weekly_long) >= 2:
            longest = week.loc[week["dist"].idxmax()]
            usual_long = weekly_long.mean()
            if longest["dist"] >= 1.25 * usual_long:
                when = _on_day(longest["start_date_local"], as_of)
                reasons.append(
                    f"The {longest['dist']:.1f} {units} {when} "
                    f"was longer than your usual long run ({usual_long:.1f} {units})."
                )

        # 6. A light week in the base makes the 28-day average low.
        weeks = [
            before[(as_of - before["day"]).dt.days // 7 == k]["dist"].sum() for k in (1, 2, 3, 4)
        ]
        typical = np.mean(weeks)
        if band in ("Elevated", "High") and typical > 0 and min(weeks) < 0.6 * typical:
            k = int(np.argmin(weeks)) + 1
            reasons.append(
                f"{k} week{'s' if k > 1 else ''} ago you ran only {min(weeks):.0f} {units}, "
                "which lowers the 4-week base this week is compared with."
            )

    if not reasons and band in ("Elevated", "High"):
        reasons.append("No single cause stands out: the load built up over the whole week.")

    return {
        "ratio": ratio,
        "band": band,
        "summary": summary,
        "reasons": reasons,
        "advice": LOAD_ADVICE[band],
    }


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
