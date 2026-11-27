"""Streamlit dashboard. Launch with `strava-dash dashboard` (or `streamlit run` this file)."""

from __future__ import annotations

import os
from datetime import timedelta

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from strava_dashboard import db
from strava_dashboard import metrics as m
from strava_dashboard.config import MAX_HR_RANGE, ConfigError, Settings

# Palette: one series hue, an ordinal ramp for HR
# zones, a second categorical hue for chronic load, and reserved status colors.
SERIES = "#2a78d6"
SERIES_2 = "#eb6834"
MUTED = "#898781"
ZONE_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#184f95"]
STATUS = {"Optimal": "#0ca30c", "Elevated": "#fab219", "High": "#d03b3b"}

REPO_URL = "https://github.com/GuillermoMorfin/strava-dashboard"
GITHUB_URL = "https://github.com/GuillermoMorfin"
LINKEDIN_URL = "https://www.linkedin.com/in/guillermo-morfin-guerrero"

PERIODS = {"Last 12 weeks": 84, "Last 6 months": 182, "Last year": 365, "All time": None}
LOAD_LABELS = {
    "trimp": "TRIMP (heart rate)",
    "distance": "Distance",
    "duration": "Duration (minutes)",
}
ACWR_LABELS = {"ewma": "Exponentially weighted", "rolling": "Rolling average"}
# Weeks as bands (an ordinal axis) so bars get a real width; tick labels thin out.
WEEK_AXIS = alt.X(
    "week_start:O",
    timeUnit="yearmonthdate",
    title="Week of",
    axis=alt.Axis(format="%b %d", labelAngle=0, labelOverlap="greedy"),
)
PACE_AXIS = (
    "floor(datum.value) + ':' + "
    "(round(datum.value % 1 * 60) < 10 ? '0' : '') + round(datum.value % 1 * 60)"
)


@st.cache_data(ttl=300, show_spinner=False)
def load_data(db_path: str, mtime: float):
    """Load all tables; `mtime` busts the cache after a sync."""
    con = db.connect(db_path, read_only=True)
    try:
        return db.load_activities(con), db.load_hr_streams(con), db.load_hr_zones(con)
    finally:
        con.close()


def chart(c: alt.Chart) -> None:
    st.altair_chart(c.configure_view(strokeWidth=0), width="stretch")


def weekly_tab(activities: pd.DataFrame, units: str, start: pd.Timestamp) -> None:
    weekly = m.weekly_distance(activities, units)
    weekly = weekly[weekly["week_start"] >= m.week_start(pd.Series([start])).iloc[0]]
    if weekly.empty:
        st.info("No runs in this period.")
        return
    bars = (
        alt.Chart(weekly)
        .mark_bar(color=SERIES, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=WEEK_AXIS,
            y=alt.Y("distance:Q", title=f"Distance ({units})"),
            tooltip=[
                alt.Tooltip("week_start:T", title="Week of", format="%b %d, %Y"),
                alt.Tooltip("distance:Q", title=f"Distance ({units})", format=".1f"),
                alt.Tooltip("runs:Q", title="Runs"),
                alt.Tooltip("hours:Q", title="Hours", format=".1f"),
                alt.Tooltip("long_run:Q", title=f"Long run ({units})", format=".1f"),
            ],
        )
        .properties(height=340)
    )
    chart(bars)
    with st.expander("Table"):
        st.dataframe(
            weekly.sort_values("week_start", ascending=False).round(
                {"distance": 2, "hours": 2, "long_run": 2}
            ),
            hide_index=True,
            width="stretch",
        )


def pace_tab(activities: pd.DataFrame, units: str, start: pd.Timestamp) -> None:
    window = st.slider("Rolling median window (runs)", 3, 30, 10)
    trend = m.pace_trend(activities, units, window=window)
    trend = trend[trend["start_date_local"] >= start]
    if trend.empty:
        st.info("No runs in this period.")
        return
    trend = trend.assign(
        pace_label=trend["pace"].map(m.format_pace),
        rolling_label=trend["rolling_pace"].map(m.format_pace),
    )
    lo, hi = trend["pace"].quantile([0.01, 0.99])
    pace_title = f"Pace (min/{units}), faster is higher"
    pace_scale = alt.Scale(reverse=True, domain=[hi + 0.25, lo - 0.25], clamp=True)
    y = alt.Y(
        "pace:Q",
        title=pace_title,
        scale=pace_scale,
        axis=alt.Axis(labelExpr=PACE_AXIS),
    )
    base = alt.Chart(trend).encode(x=alt.X("start_date_local:T", title=None))
    points = base.mark_circle(size=40, color=MUTED, opacity=0.55).encode(
        y=y,
        tooltip=[
            alt.Tooltip("start_date_local:T", title="Date", format="%b %d, %Y"),
            alt.Tooltip("name:N", title="Run"),
            alt.Tooltip("distance:Q", title=f"Distance ({units})", format=".2f"),
            alt.Tooltip("pace_label:N", title="Pace"),
            alt.Tooltip("average_heartrate:Q", title="Avg HR", format=".0f"),
        ],
    )
    line = base.mark_line(color=SERIES, strokeWidth=2).encode(
        y=alt.Y("rolling_pace:Q", scale=pace_scale, title=pace_title),
        tooltip=[
            alt.Tooltip("start_date_local:T", title="Date", format="%b %d, %Y"),
            alt.Tooltip("rolling_label:N", title=f"Rolling median ({window} runs)"),
        ],
    )
    chart((points + line).properties(height=340))
    st.caption(
        f"Grey dots are individual runs; the blue line is the median of the last {window}. "
        "Pace is moving time ÷ distance, so it includes easy runs and workouts alike."
    )


def zones_tab(
    activities: pd.DataFrame,
    streams: pd.DataFrame,
    zones: m.Zones,
    zones_note: str,
    start: pd.Timestamp,
) -> None:
    st.caption(zones_note)
    long = m.zone_time_by_activity(streams, activities, zones)
    long = long[long["start_date_local"] >= start]
    if long.empty:
        st.info(
            "No heart-rate streams for this period yet. `strava-dash sync` fetches "
            "50 per run by default (`--streams N` to change)."
        )
        return
    labels = {
        i + 1: f"Z{i + 1} {lo}–{hi if hi != -1 else 'max'} bpm" for i, (lo, hi) in enumerate(zones)
    }
    long = long.assign(zone_label=long["zone"].map(labels), hours=long["seconds"] / 3600)
    color = alt.Color(
        "zone_label:N",
        title="Zone",
        scale=alt.Scale(domain=list(labels.values()), range=ZONE_RAMP[: len(zones)]),
        sort=list(labels.values()),
    )

    totals = long.groupby(["zone", "zone_label"], as_index=False)["hours"].sum()
    totals["share"] = totals["hours"] / totals["hours"].sum()
    left, right = st.columns([2, 3])
    with left:
        st.markdown("**Share of time in each zone**")
        bars = (
            alt.Chart(totals)
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                y=alt.Y("zone_label:N", title=None, sort=list(labels.values())),
                x=alt.X("share:Q", title=None, axis=alt.Axis(format="%")),
                color=color.legend(None),
                tooltip=[
                    alt.Tooltip("zone_label:N", title="Zone"),
                    alt.Tooltip("share:Q", title="Share", format=".1%"),
                    alt.Tooltip("hours:Q", title="Hours", format=".1f"),
                ],
            )
        )
        text = bars.mark_text(align="left", dx=4).encode(
            text=alt.Text("share:Q", format=".0%"), color=alt.value(MUTED)
        )
        chart((bars + text).properties(height=260))
    with right:
        st.markdown("**Hours per week by zone**")
        weekly = long.groupby(["week_start", "zone", "zone_label"], as_index=False)["hours"].sum()
        stacked = (
            alt.Chart(weekly)
            .mark_bar()
            .encode(
                x=WEEK_AXIS,
                y=alt.Y("hours:Q", title="Hours"),
                color=color,
                order=alt.Order("zone:Q"),
                tooltip=[
                    alt.Tooltip("week_start:T", title="Week of", format="%b %d, %Y"),
                    alt.Tooltip("zone_label:N", title="Zone"),
                    alt.Tooltip("hours:Q", title="Hours", format=".2f"),
                ],
            )
            .properties(height=260)
        )
        chart(stacked)

    intensity_trend(long)
    with st.expander("Table"):
        st.dataframe(totals.round(3), hide_index=True, width="stretch")


INTENSITY_LABELS = {"easy": "Easy (Z1–2)", "moderate": "Moderate (Z3)", "hard": "Hard (Z4–5)"}


def intensity_trend(zone_time: pd.DataFrame) -> None:
    head, toggle = st.columns([3, 1])
    head.markdown("**Easy vs hard over time**")
    by = toggle.radio("Group by", ["Week", "Month"], horizontal=True, key="intensity_freq")
    dist = m.intensity_distribution(zone_time, freq=by[0])
    if dist.empty:
        st.info("Not enough heart-rate data yet (needs at least 30 minutes per period).")
        return
    lines = dist.melt(
        id_vars=["period_start", "hours"], value_vars=list(INTENSITY_LABELS), var_name="level"
    )
    lines["level"] = lines["level"].map(INTENSITY_LABELS)
    color = alt.Color(
        "level:N",
        title=None,
        scale=alt.Scale(domain=list(INTENSITY_LABELS.values()), range=[SERIES, SERIES_2, MUTED]),
        legend=alt.Legend(orient="top"),
    )
    period_title = "Week of" if by == "Week" else "Month"
    period_format = "%b %d, %Y" if by == "Week" else "%B %Y"
    trend = (
        alt.Chart(lines)
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=25))
        .encode(
            x=alt.X("period_start:T", title=None),
            y=alt.Y(
                "value:Q",
                title="Share of HR time",
                axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, 1]),
            ),
            color=color,
            tooltip=[
                alt.Tooltip("period_start:T", title=period_title, format=period_format),
                alt.Tooltip("level:N", title="Intensity"),
                alt.Tooltip("value:Q", title="Share", format=".0%"),
                alt.Tooltip("hours:Q", title="Hours of HR data", format=".1f"),
            ],
        )
    )
    target = (
        alt.Chart(pd.DataFrame({"y": [0.8]}))
        .mark_rule(strokeDash=[4, 4], color=MUTED)
        .encode(y="y:Q")
    )
    label = target.mark_text(align="left", dx=4, dy=-6, color=MUTED, text="80% easy").encode(
        x=alt.value(0)
    )
    chart((target + label + trend).properties(height=260))
    st.caption(
        "Most endurance plans aim for roughly 80% of time easy. Watch the moderate (Z3) line: "
        "easy runs drifting into Z3 add fatigue without much extra benefit. Shares only cover "
        "runs with heart-rate detail; periods with under 30 minutes of it are hidden."
    )


def efficiency_tab(
    activities: pd.DataFrame, zones: m.Zones, units: str, start: pd.Timestamp
) -> None:
    lo, hi = zones[1]
    left, right = st.columns([3, 2])
    road_only = left.checkbox("Road runs only (trail runs distort pace)", value=True)
    window = right.slider("Rolling median window (runs)", 3, 30, 10, key="ef_window")
    eff = m.aerobic_efficiency(activities, zones, units, road_only=road_only, window=window)
    eff = eff[eff["start_date_local"] >= start]
    if eff.empty:
        st.info(f"No runs of 20+ minutes with an average heart rate in zone 2 ({lo}–{hi} bpm).")
        return
    st.markdown(
        f"Steady runs of 20+ minutes whose **average heart rate was in zone 2** "
        f"({lo}–{hi} bpm). {len(eff)} runs in this period."
    )
    eff = eff.assign(
        pace_label=eff["pace"].map(m.format_pace),
        rolling_label=eff["rolling_pace"].map(m.format_pace),
    )
    base = alt.Chart(eff).encode(x=alt.X("start_date_local:T", title=None))
    run_tooltip = [
        alt.Tooltip("start_date_local:T", title="Date", format="%b %d, %Y"),
        alt.Tooltip("name:N", title="Run"),
        alt.Tooltip("distance:Q", title=f"Distance ({units})", format=".1f"),
        alt.Tooltip("pace_label:N", title="Pace"),
        alt.Tooltip("average_heartrate:Q", title="Avg HR", format=".0f"),
        alt.Tooltip("ef:Q", title="Efficiency factor", format=".2f"),
    ]

    lo_p, hi_p = eff["pace"].quantile([0.01, 0.99])
    pace_scale = alt.Scale(reverse=True, domain=[hi_p + 0.25, lo_p - 0.25], clamp=True)
    pace_y = alt.Y(
        "pace:Q",
        title=f"Pace (min/{units}), faster is higher",
        scale=pace_scale,
        axis=alt.Axis(labelExpr=PACE_AXIS),
    )
    st.markdown("**Zone 2 pace**")
    chart(
        (
            base.mark_circle(size=40, color=MUTED, opacity=0.55).encode(
                y=pace_y, tooltip=run_tooltip
            )
            + base.mark_line(color=SERIES, strokeWidth=2).encode(
                y=alt.Y("rolling_pace:Q", scale=pace_scale),
                tooltip=[
                    alt.Tooltip("start_date_local:T", title="Date", format="%b %d, %Y"),
                    alt.Tooltip("rolling_label:N", title=f"Rolling median ({window} runs)"),
                ],
            )
        ).properties(height=260)
    )

    st.markdown("**Efficiency factor** (meters per minute ÷ average HR)")
    lo_e, hi_e = eff["ef"].quantile([0.01, 0.99])
    ef_scale = alt.Scale(domain=[lo_e * 0.97, hi_e * 1.03], clamp=True, zero=False)
    chart(
        (
            base.mark_circle(size=40, color=MUTED, opacity=0.55).encode(
                y=alt.Y("ef:Q", title="Meters per heartbeat", scale=ef_scale),
                tooltip=run_tooltip,
            )
            + base.mark_line(color=SERIES, strokeWidth=2).encode(
                y=alt.Y("rolling_ef:Q", scale=ef_scale),
                tooltip=[
                    alt.Tooltip("start_date_local:T", title="Date", format="%b %d, %Y"),
                    alt.Tooltip(
                        "rolling_ef:Q", title=f"Rolling median ({window} runs)", format=".2f"
                    ),
                ],
            )
        ).properties(height=220)
    )
    first, last = eff["rolling_ef"].iloc[[min(window, len(eff)) - 1, -1]]
    change = last / first - 1
    st.caption(
        f"Efficiency factor {'up' if change >= 0 else 'down'} {abs(change):.1%} over this period "
        "(rolling median). Pace alone can improve just because you ran a bit harder; the "
        "efficiency factor divides by heart rate, so it shows whether the same effort moves you "
        "further. Heat, hills, wind and fatigue all lower it, so look at the trend over weeks."
    )


def load_tab(acwr_df: pd.DataFrame, loads: pd.DataFrame, method: str, start: pd.Timestamp):
    shown = acwr_df[acwr_df.index >= start].reset_index()
    if shown.empty:
        st.info("No training load in this period.")
        return
    estimated = loads["estimated"].mean()
    if method == "trimp" and estimated > 0:
        st.caption(
            f"{estimated:.0%} of runs had no heart rate; their load is estimated from duration."
        )

    st.markdown("**Acute (7-day) vs chronic (28-day) load**")
    lines = shown.melt(id_vars="date", value_vars=["acute", "chronic"], var_name="series")
    lines["series"] = lines["series"].map({"acute": "Acute (7 d)", "chronic": "Chronic (28 d)"})
    chart(
        alt.Chart(lines)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("value:Q", title=f"Daily load ({method})"),
            color=alt.Color(
                "series:N",
                title=None,
                scale=alt.Scale(range=[SERIES, SERIES_2]),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=[
                alt.Tooltip("date:T", format="%b %d, %Y"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("value:Q", title="Load", format=".1f"),
            ],
        )
        .properties(height=260)
    )

    st.markdown("**Acute : chronic workload ratio**")
    ratio = shown.dropna(subset=["ratio"])
    if ratio.empty:
        st.info("The ratio needs at least 28 days of history.")
        return
    top = max(2.0, float(ratio["ratio"].max()) + 0.1)
    bands = pd.DataFrame(
        [
            {"band": label, "y0": lo, "y1": min(hi, top)}
            for lo, hi, label in m.ACWR_BANDS
            if label in STATUS
        ]
    )
    band_marks = (
        alt.Chart(bands)
        .mark_rect(opacity=0.14)
        .encode(
            y=alt.Y("y0:Q", scale=alt.Scale(domain=[0, top])),
            y2="y1:Q",
            color=alt.Color(
                "band:N",
                title="Zone",
                scale=alt.Scale(domain=list(STATUS), range=list(STATUS.values())),
                legend=alt.Legend(orient="top", symbolOpacity=0.5),
            ),
        )
    )
    ratio_line = (
        alt.Chart(ratio.assign(band=ratio["ratio"].map(m.acwr_band)))
        .mark_line(color=SERIES, strokeWidth=2)
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("ratio:Q", title="Ratio", scale=alt.Scale(domain=[0, top])),
            tooltip=[
                alt.Tooltip("date:T", format="%b %d, %Y"),
                alt.Tooltip("ratio:Q", title="Ratio", format=".2f"),
                alt.Tooltip("band:N", title="Zone"),
            ],
        )
    )
    chart((band_marks + ratio_line).properties(height=260))
    st.caption(
        "The 0.8–1.3 'sweet spot' comes from team-sport research and is debated; treat "
        "the ratio as a prompt to notice sudden jumps in load, not as an injury predictor."
    )


def races_tab(effort: pd.Series | None, units: str, lookback: int) -> None:
    if effort is None:
        st.info(f"No runs of 3 km or more in the last {lookback} days to base predictions on.")
        return
    per_unit = m.METERS_PER_UNIT[units]
    st.markdown(
        f"Based on **{effort['name']}** on {effort['start_date_local']:%b %d, %Y}: "
        f"{effort['distance_m'] / per_unit:.2f} {units} in "
        f"{m.format_duration(effort['moving_time_s'])} (VDOT **{effort['vdot']:.1f}**), "
        f"your strongest effort in the last {lookback} days."
    )
    preds = m.race_predictions(effort)
    table = pd.DataFrame(
        {
            "Race": preds["race"],
            "VDOT (Daniels)": preds["vdot_time_s"].map(m.format_duration),
            f"Pace /{units}": (preds["vdot_time_s"] / 60 / (preds["distance_m"] / per_unit)).map(
                m.format_pace
            ),
            "Riegel": preds["riegel_time_s"].map(m.format_duration),
        }
    )
    st.table(table.set_index("Race"))
    st.caption(
        "Predictions assume equivalent training for the distance. They are most accurate "
        "when based on a recent race or hard time trial; marathon predictions in particular "
        "are optimistic without enough long runs. Moving time is used, so stops are excluded."
    )


def main() -> None:
    st.set_page_config(page_title="Strava Training Dashboard", page_icon="🏃", layout="wide")
    st.title("Strava Training Dashboard")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        st.error(f"Check your `.env` file: {exc}")
        st.stop()
    path = settings.db_path

    demo = bool(os.environ.get("STRAVA_DEMO_MODE"))
    if demo:
        st.info(
            "**Live demo with synthetic data.** These ~200 days of runs are generated, not "
            "real. Run it locally and connect your own Strava account to see yours: "
            f"[GitHub repo]({REPO_URL})."
        )
    if not path.exists():
        st.info(
            f"No database at `{path}` yet. Run `strava-dash auth` then `strava-dash sync`, "
            "or try `strava-dash demo` and `strava-dash dashboard --db data/demo.duckdb`."
        )
        st.stop()
    try:
        activities, streams, stored_zones = load_data(str(path), path.stat().st_mtime)
    except duckdb.IOException as exc:
        st.error(f"Couldn't open the database (is a sync running?): {exc}")
        st.stop()
    if activities.empty:
        st.info("No runs in the database yet. Run `strava-dash sync`.")
        st.stop()

    observed_max = activities["max_heartrate"].max()
    with st.sidebar:
        st.header("Settings")
        units = st.radio(
            "Units",
            m.METERS_PER_UNIT,
            index=list(m.METERS_PER_UNIT).index(settings.units),
            horizontal=True,
        )
        period = st.selectbox("Period", list(PERIODS), index=1)
        load_method = st.selectbox("Training load", list(LOAD_LABELS), format_func=LOAD_LABELS.get)
        acwr_method = st.radio("ACWR model", list(ACWR_LABELS), format_func=ACWR_LABELS.get)
        lookback = st.slider("Race prediction lookback (days)", 30, 365, 90, step=15)
        st.subheader("Heart rate")
        default_max = settings.max_hr or (int(observed_max) if pd.notna(observed_max) else 190)
        # A GPS/strap glitch can record an impossible max; keep the input in range.
        default_max = min(max(default_max, MAX_HR_RANGE[0]), MAX_HR_RANGE[1])
        max_hr = st.number_input("Max HR", *MAX_HR_RANGE, default_max)
        resting_hr = st.number_input("Resting HR", 30, 100, settings.resting_hr)

    today = pd.Timestamp.now().normalize()
    days = PERIODS[period]
    start = today - timedelta(days=days) if days else activities["start_date_local"].min()

    if stored_zones:
        zones = stored_zones
        zones_note = (
            "Demo heart-rate zones (synthetic athlete with a 190 bpm max)."
            if demo
            else "Heart-rate zones from your Strava profile."
        )
    else:
        zones = m.default_hr_zones(int(max_hr))
        zones_note = f"Zones estimated at 60/70/80/90% of max HR ({int(max_hr)} bpm)."

    # Load and ACWR use full history (chronic load needs the prior 28 days).
    loads = m.activity_load(
        activities, load_method, units=units, max_hr=max_hr, resting_hr=resting_hr
    )
    acwr_df = m.acwr(m.daily_load(loads, end=today), acwr_method)
    effort = m.best_effort(activities, as_of=pd.Timestamp.now(), lookback_days=lookback)

    weekly = m.weekly_distance(activities, units).set_index("week_start")
    this_week = m.week_start(pd.Series([today])).iloc[0]
    this_dist = weekly["distance"].get(this_week, 0.0)
    last_dist = weekly["distance"].get(this_week - timedelta(weeks=1), 0.0)
    prior4 = weekly["distance"].reindex(
        pd.date_range(end=this_week - timedelta(weeks=1), periods=4, freq="W-MON"), fill_value=0
    )
    ratio_now = acwr_df["ratio"].iloc[-1] if not acwr_df.empty else float("nan")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
        f"This week ({units})",
        f"{this_dist:.1f}",
        f"{this_dist - last_dist:+.1f} vs last week",
        delta_color="off",
    )
    k2.metric(f"4-week average ({units})", f"{prior4.mean():.1f}")
    k3.metric(
        "Acute : chronic ratio",
        "–" if pd.isna(ratio_now) else f"{ratio_now:.2f}",
        m.acwr_band(ratio_now),
        delta_color="off",
        delta_arrow="off",
    )
    if effort is not None:
        k4.metric(
            "Predicted 5K / half",
            f"{m.format_duration(m.time_for_vdot(effort['vdot'], 5000))} / "
            f"{m.format_duration(m.time_for_vdot(effort['vdot'], 21097.5))}",
            f"VDOT {effort['vdot']:.1f}",
            delta_color="off",
            delta_arrow="off",
        )
    else:
        k4.metric("Predicted 5K / half", "–")

    tabs = st.tabs(
        [
            "Weekly mileage",
            "Pace",
            "HR zones",
            "Aerobic efficiency",
            "Training load",
            "Race predictions",
        ]
    )
    with tabs[0]:
        weekly_tab(activities, units, start)
    with tabs[1]:
        pace_tab(activities, units, start)
    with tabs[2]:
        zones_tab(activities, streams, zones, zones_note, start)
    with tabs[3]:
        efficiency_tab(activities, zones, units, start)
    with tabs[4]:
        load_tab(acwr_df, loads, load_method, start)
    with tabs[5]:
        races_tab(effort, units, lookback)

    source = "synthetic demo data" if demo else f"data from `{path}`"
    st.caption(
        f"{len(activities)} runs · last activity "
        f"{activities['start_date_local'].max():%b %d, %Y} · {source}"
    )
    st.caption(f"Built by Guillermo Morfin · [GitHub]({GITHUB_URL}) · [LinkedIn]({LINKEDIN_URL})")


main()
