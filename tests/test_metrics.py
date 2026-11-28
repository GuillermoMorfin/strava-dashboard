import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from strava_dashboard import metrics as m


def frame(rows):
    """Build an activities DataFrame like db.load_activities returns."""
    defaults = {
        "name": "Run",
        "average_heartrate": 150.0,
        "max_heartrate": 180.0,
        "workout_type": 0,
    }
    df = pd.DataFrame([{**defaults, **r} for r in rows])
    df["start_date_local"] = pd.to_datetime(df["start_date_local"])
    return df


# --- formatting --------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected", [(59.6, "1:00"), (605, "10:05"), (3725, "1:02:05"), (None, "–")]
)
def test_format_duration(seconds, expected):
    assert m.format_duration(seconds) == expected


def test_format_pace():
    assert m.format_pace(8.5) == "8:30"
    assert m.format_pace(float("nan")) == "–"


# --- weekly distance ---------------------------------------------------------


def test_weekly_distance_groups_by_monday_and_fills_gaps():
    df = frame(
        [
            # Sun 2024-03-03 belongs to week of Mon 2024-02-26
            {
                "id": 1,
                "start_date_local": "2024-03-03 09:00",
                "distance_m": 1609.344,
                "moving_time_s": 600,
            },
            {
                "id": 2,
                "start_date_local": "2024-03-04 09:00",
                "distance_m": 3218.688,
                "moving_time_s": 1200,
            },
            {
                "id": 3,
                "start_date_local": "2024-03-10 09:00",
                "distance_m": 1609.344,
                "moving_time_s": 600,
            },
            # skip a week
            {
                "id": 4,
                "start_date_local": "2024-03-20 09:00",
                "distance_m": 1609.344,
                "moving_time_s": 600,
            },
        ]
    )
    weekly = m.weekly_distance(df, "mi")
    assert list(weekly["week_start"].dt.strftime("%Y-%m-%d")) == [
        "2024-02-26",
        "2024-03-04",
        "2024-03-11",
        "2024-03-18",
    ]
    assert weekly["distance"].round(3).tolist() == [1.0, 3.0, 0.0, 1.0]
    assert weekly["runs"].tolist() == [1, 2, 0, 1]
    assert weekly.loc[1, "long_run"] == pytest.approx(2.0)
    assert weekly.loc[1, "hours"] == pytest.approx(0.5)


def test_weekly_distance_km_and_empty():
    df = frame(
        [{"id": 1, "start_date_local": "2024-03-04", "distance_m": 5000, "moving_time_s": 1500}]
    )
    assert m.weekly_distance(df, "km")["distance"].item() == pytest.approx(5.0)
    assert m.weekly_distance(df.iloc[0:0], "km").empty


# --- pace ----------------------------------------------------------------------


def test_pace_trend_minutes_per_unit_and_rolling_median():
    df = frame(
        [
            {
                "id": i,
                "start_date_local": f"2024-03-0{i}",
                "distance_m": 1000,
                "moving_time_s": secs,
            }
            for i, secs in [(1, 300), (2, 360), (3, 330), (4, 100)]
        ]
        + [{"id": 9, "start_date_local": "2024-03-05", "distance_m": 400, "moving_time_s": 60}]
    )
    trend = m.pace_trend(df, "km", window=3)
    assert trend["id"].tolist() == [1, 2, 3, 4]  # short run filtered out
    assert trend["pace"].tolist() == pytest.approx([5.0, 6.0, 5.5, 100 / 60])
    assert trend["rolling_pace"].tolist() == pytest.approx([5.0, 5.5, 5.5, 5.5])


# --- HR zones ----------------------------------------------------------------


def test_default_hr_zones():
    assert m.default_hr_zones(200) == [(0, 120), (120, 140), (140, 160), (160, 180), (180, -1)]


def test_zone_indices_boundaries():
    zones = [(0, 120), (120, 150), (150, -1)]
    assert m.zone_indices(np.array([0, 119, 120, 149, 150, 220]), zones).tolist() == [
        0,
        0,
        1,
        1,
        2,
        2,
    ]


def test_time_in_zones_ignores_pauses():
    zones = [(0, 120), (120, 150), (150, -1)]
    t = [0, 10, 20, 30, 330, 340]  # 300 s pause between 30 and 330
    hr = [100, 130, 130, 160, 160, 160]
    secs = m.time_in_zones(t, hr, zones)
    assert secs.tolist() == [10, 20, 10]


def test_time_in_zones_short_stream():
    assert m.time_in_zones([0], [100], [(0, -1)]).tolist() == [0]


def test_zone_time_by_activity_long_form():
    zones = [(0, 120), (120, -1)]
    streams = pd.DataFrame(
        {"activity_id": [1], "time_s": [[0, 10, 20]], "heartrate": [[100, 130, 130]]}
    )
    acts = frame([{"id": 1, "start_date_local": "2024-03-06", "distance_m": 1, "moving_time_s": 1}])
    out = m.zone_time_by_activity(streams, acts, zones)
    assert out["zone"].tolist() == [1, 2]
    assert out["seconds"].tolist() == [10, 10]
    assert out["week_start"].iloc[0] == pd.Timestamp("2024-03-04")


# --- load / ACWR ---------------------------------------------------------------


def test_banister_trimp_known_value():
    # 60 min at HRr 0.5 -> 60 * 0.5 * 0.64 * e^0.96
    assert m.banister_trimp(60, 120, 60, 180) == pytest.approx(60 * 0.5 * 0.64 * math.exp(0.96))


def test_activity_load_trimp_estimates_runs_without_hr():
    df = frame(
        [
            {
                "id": 1,
                "start_date_local": "2024-03-01",
                "distance_m": 10000,
                "moving_time_s": 3600,
                "average_heartrate": 120.0,
            },
            {
                "id": 2,
                "start_date_local": "2024-03-02",
                "distance_m": 5000,
                "moving_time_s": 1800,
                "average_heartrate": None,
            },
        ]
    )
    loads = m.activity_load(df, "trimp", max_hr=180, resting_hr=60)
    hr_load = 60 * 0.5 * 0.64 * math.exp(0.96)
    assert loads["load"].tolist() == pytest.approx([hr_load, hr_load / 2])
    assert loads["estimated"].tolist() == [False, True]


def test_activity_load_trimp_without_any_hr_falls_back_to_minutes():
    df = frame(
        [
            {
                "id": 1,
                "start_date_local": "2024-03-01",
                "distance_m": 1000,
                "moving_time_s": 600,
                "average_heartrate": None,
                "max_heartrate": None,
            }
        ]
    )
    loads = m.activity_load(df, "trimp")
    assert loads["load"].item() == 10 and loads["estimated"].item()


def test_activity_load_distance_and_duration():
    df = frame(
        [{"id": 1, "start_date_local": "2024-03-01", "distance_m": 5000, "moving_time_s": 1500}]
    )
    assert m.activity_load(df, "distance", units="km")["load"].item() == 5
    assert m.activity_load(df, "duration")["load"].item() == 25
    with pytest.raises(ValueError):
        m.activity_load(df, "vibes")


def test_daily_load_sums_and_fills_rest_days():
    loads = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-03-01", "2024-03-01", "2024-03-03"]),
            "load": [10.0, 5.0, 20.0],
        }
    )
    daily = m.daily_load(loads, end=datetime(2024, 3, 5, 18, 0))
    assert daily.tolist() == [15, 0, 20, 0, 0]
    assert m.daily_load(loads.iloc[0:0]).empty


def test_acwr_rolling_constant_load_is_one():
    daily = pd.Series(10.0, index=pd.date_range("2024-01-01", periods=40))
    out = m.acwr(daily, "rolling")
    assert out["ratio"].iloc[:27].isna().all()
    assert out["ratio"].iloc[27:].tolist() == pytest.approx([1.0] * 13)


def test_acwr_spike_raises_ratio_and_ewma_responds():
    values = [10.0] * 35 + [30.0] * 7
    daily = pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values)))
    rolling = m.acwr(daily, "rolling")
    # acute = 30, chronic = (21*10 + 7*30) / 28 = 15
    assert rolling["ratio"].iloc[-1] == pytest.approx(2.0)
    ewma = m.acwr(daily, "ewma")
    assert 1.3 < ewma["ratio"].iloc[-1] < 2.0
    with pytest.raises(ValueError):
        m.acwr(daily, "magic")


@pytest.mark.parametrize(
    "ratio, band",
    [
        (0.5, "Low"),
        (0.8, "Optimal"),
        (1.29, "Optimal"),
        (1.4, "Elevated"),
        (2.0, "High"),
        (float("nan"), "Insufficient data"),
    ],
)
def test_acwr_band(ratio, band):
    assert m.acwr_band(ratio) == band


# --- race predictions ------------------------------------------------------------


def test_vdot_matches_daniels_table():
    # Daniels' Running Formula: 20:00 5K ~ VDOT 49.8
    assert m.vdot(5000, 20 * 60) == pytest.approx(49.8, abs=0.1)


@pytest.mark.parametrize(
    "meters, expected_s",
    [
        (5000, 19 * 60 + 57),
        (10000, 41 * 60 + 21),
        (21097.5, 3600 + 31 * 60 + 35),
        (42195, 3 * 3600 + 10 * 60 + 49),
    ],
)
def test_time_for_vdot_matches_daniels_table_for_vdot_50(meters, expected_s):
    assert m.time_for_vdot(50, meters) == pytest.approx(expected_s, abs=30)


def test_vdot_roundtrip():
    assert m.time_for_vdot(m.vdot(10000, 2700), 10000) == pytest.approx(2700, abs=0.5)


def test_riegel():
    assert m.riegel(1200, 5000, 10000) == pytest.approx(1200 * 2**1.06)


def test_best_effort_picks_highest_vdot_in_window_and_skips_gps_glitches():
    df = frame(
        [
            {
                "id": 1,
                "name": "Easy",
                "start_date_local": "2024-03-01",
                "distance_m": 10000,
                "moving_time_s": 3300,
            },
            {
                "id": 2,
                "name": "Parkrun",
                "start_date_local": "2024-03-02",
                "distance_m": 5000,
                "moving_time_s": 1200,
            },
            {
                "id": 3,
                "name": "Glitch",
                "start_date_local": "2024-03-03",
                "distance_m": 5000,
                "moving_time_s": 500,
            },
            {
                "id": 4,
                "name": "Old PR",
                "start_date_local": "2023-01-01",
                "distance_m": 5000,
                "moving_time_s": 1000,
            },
            {
                "id": 5,
                "name": "Strides",
                "start_date_local": "2024-03-04",
                "distance_m": 800,
                "moving_time_s": 150,
            },
        ]
    )
    best = m.best_effort(df, as_of=datetime(2024, 3, 10), lookback_days=90)
    assert best["name"] == "Parkrun"
    assert best["vdot"] == pytest.approx(m.vdot(5000, 1200))
    assert m.best_effort(df, as_of=datetime(2030, 1, 1)) is None


def test_race_predictions_table():
    effort = pd.Series({"distance_m": 5000.0, "moving_time_s": 1197.0, "vdot": 50.0})
    preds = m.race_predictions(effort)
    assert preds["race"].tolist() == list(m.RACE_DISTANCES)
    five_k = preds.iloc[0]
    assert five_k["vdot_time_s"] == pytest.approx(1197, abs=30)
    assert five_k["riegel_time_s"] == pytest.approx(1197)
    assert (preds["vdot_time_s"].diff().dropna() > 0).all()


# --- intensity distribution ---------------------------------------------------


def zone_rows(rows):
    """zone_time_by_activity-shaped frame from (date, zone, seconds) tuples."""
    df = pd.DataFrame(rows, columns=["start_date_local", "zone", "seconds"])
    df["start_date_local"] = pd.to_datetime(df["start_date_local"])
    df["activity_id"] = range(len(df))
    df["week_start"] = m.week_start(df["start_date_local"])
    return df


def test_intensity_distribution_groups_zones_by_week():
    zt = zone_rows(
        [
            ("2024-03-04", 1, 1800),  # Mon: easy
            ("2024-03-05", 2, 1800),  # easy
            ("2024-03-06", 3, 600),  # moderate
            ("2024-03-07", 4, 300),  # hard
            ("2024-03-07", 5, 300),  # hard
            ("2024-03-11", 2, 3600),  # next week, all easy
        ]
    )
    out = m.intensity_distribution(zt, "W")
    assert out["period_start"].dt.strftime("%Y-%m-%d").tolist() == ["2024-03-04", "2024-03-11"]
    first = out.iloc[0]
    assert first["easy"] == pytest.approx(3600 / 4800)
    assert first["moderate"] == pytest.approx(600 / 4800)
    assert first["hard"] == pytest.approx(600 / 4800)
    assert first["hours"] == pytest.approx(4800 / 3600)
    assert out.iloc[1][["easy", "moderate", "hard"]].tolist() == [1, 0, 0]


def test_intensity_distribution_by_month_and_min_hours():
    zt = zone_rows([("2024-03-04", 2, 1800), ("2024-03-28", 3, 1800), ("2024-04-02", 1, 600)])
    out = m.intensity_distribution(zt, "M", min_hours=0.5)
    assert out["period_start"].dt.strftime("%Y-%m").tolist() == ["2024-03"]  # April < 30 min
    assert out.iloc[0]["easy"] == pytest.approx(0.5)


def test_intensity_distribution_empty_and_bad_freq():
    empty = m.zone_time_by_activity(pd.DataFrame(), pd.DataFrame(), [])
    assert m.intensity_distribution(empty).empty
    with pytest.raises(ValueError):
        m.intensity_distribution(empty, "D")


# --- aerobic efficiency ---------------------------------------------------------

ZONES = [(0, 130), (130, 150), (150, 170), (170, -1)]


def test_aerobic_efficiency_selects_steady_zone2_road_runs():
    acts = frame(
        [
            # 10 km in 50 min at 140 bpm: zone 2, kept
            {
                "id": 1,
                "start_date_local": "2024-03-01",
                "distance_m": 10000,
                "moving_time_s": 3000,
                "average_heartrate": 140.0,
                "sport_type": "Run",
            },
            # too hard (zone 3)
            {
                "id": 2,
                "start_date_local": "2024-03-02",
                "distance_m": 10000,
                "moving_time_s": 2700,
                "average_heartrate": 160.0,
                "sport_type": "Run",
            },
            # too short
            {
                "id": 3,
                "start_date_local": "2024-03-03",
                "distance_m": 2000,
                "moving_time_s": 600,
                "average_heartrate": 140.0,
                "sport_type": "Run",
            },
            # trail
            {
                "id": 4,
                "start_date_local": "2024-03-04",
                "distance_m": 10000,
                "moving_time_s": 3600,
                "average_heartrate": 140.0,
                "sport_type": "TrailRun",
            },
            # no HR
            {
                "id": 5,
                "start_date_local": "2024-03-05",
                "distance_m": 10000,
                "moving_time_s": 3000,
                "average_heartrate": None,
                "sport_type": "Run",
            },
            # faster at the same HR
            {
                "id": 6,
                "start_date_local": "2024-03-06",
                "distance_m": 10000,
                "moving_time_s": 2880,
                "average_heartrate": 140.0,
                "sport_type": "Run",
            },
        ]
    )
    out = m.aerobic_efficiency(acts, ZONES, "km", window=2)
    assert out["id"].tolist() == [1, 6]
    assert out["pace"].tolist() == pytest.approx([5.0, 4.8])
    assert out["ef"].tolist() == pytest.approx([200 / 140, (10000 / 48) / 140])
    assert out["rolling_ef"].iloc[-1] == pytest.approx(out["ef"].mean())
    with_trail = m.aerobic_efficiency(acts, ZONES, "km", road_only=False)
    assert with_trail["id"].tolist() == [1, 4, 6]


def test_aerobic_efficiency_without_sport_type_or_zones():
    acts = frame(
        [
            {
                "id": 1,
                "start_date_local": "2024-03-01",
                "distance_m": 10000,
                "moving_time_s": 3000,
                "average_heartrate": 140.0,
            }
        ]
    )
    assert m.aerobic_efficiency(acts, ZONES)["id"].tolist() == [1]
    assert m.aerobic_efficiency(acts, [(0, -1)]).empty


def test_aerobic_efficiency_open_ended_zone2():
    acts = frame(
        [
            {
                "id": 1,
                "start_date_local": "2024-03-01",
                "distance_m": 10000,
                "moving_time_s": 3000,
                "average_heartrate": 199.0,
            }
        ]
    )
    assert len(m.aerobic_efficiency(acts, [(0, 130), (130, -1)])) == 1


# --- year-over-year comparison --------------------------------------------------

AS_OF = "2026-11-28"


def test_period_comparison_lines_up_the_same_months_each_year():
    acts = frame(
        [
            # this year: 10 km in 50 min (zone 2) and 5 km in 30 min -> 80 min / 15 km
            {
                "id": 1,
                "start_date_local": "2026-11-28 07:00",
                "distance_m": 10000,
                "moving_time_s": 3000,
                "average_heartrate": 140.0,
                "sport_type": "Run",
            },
            {
                "id": 2,
                "start_date_local": "2026-06-01 07:00",
                "distance_m": 5000,
                "moving_time_s": 1800,
                "sport_type": "Run",
            },
            # a slow trail run: counts for volume, not for pace
            {
                "id": 3,
                "start_date_local": "2026-07-01 07:00",
                "distance_m": 10000,
                "moving_time_s": 4800,
                "sport_type": "TrailRun",
            },
            # exactly at the window's start: belongs to the window before
            {
                "id": 4,
                "start_date_local": "2026-05-28 07:00",
                "distance_m": 5000,
                "moving_time_s": 1500,
                "sport_type": "Run",
            },
            # one year ago: 10 km in 55 min
            {
                "id": 5,
                "start_date_local": "2025-09-01 07:00",
                "distance_m": 10000,
                "moving_time_s": 3300,
                "sport_type": "Run",
            },
        ]
    )
    zones = [(0, 130), (130, 150), (150, 170), (170, -1)]
    cmp = m.period_comparison(acts, AS_OF, months=6, years=3, units="km", zones=zones)

    assert cmp["label"].tolist() == ["Last 6 months", "1 year ago", "2 years ago"]
    assert cmp["runs"].tolist() == [3, 1, 0]
    assert cmp["avg_pace"].iloc[0] == pytest.approx(80 / 15)
    assert cmp["avg_pace"].iloc[1] == pytest.approx(5.5)
    assert np.isnan(cmp["avg_pace"].iloc[2])
    weeks = (pd.Timestamp("2026-11-28") - pd.Timestamp("2026-05-28")).days / 7
    assert cmp["distance_per_week"].iloc[0] == pytest.approx(25 / weeks)
    assert cmp["z2_pace"].iloc[0] == pytest.approx(5.0) and cmp["z2_runs"].iloc[0] == 1
    assert cmp["long_run"].iloc[0] == pytest.approx(10)
    assert cmp["start"].iloc[1] == pd.Timestamp("2025-05-29")

    text = m.comparison_summary(cmp, "km")
    assert text.startswith(
        "Average pace over the last 6 months was 5:20 /km, 10 s faster than 1 year ago."
    )
    assert "zone 2" not in text  # no zone 2 runs a year ago to compare with

    with_trail = m.period_comparison(
        acts, AS_OF, months=6, units="km", zones=zones, road_only=False
    )
    assert with_trail["avg_pace"].iloc[0] == pytest.approx(160 / 25)


def test_comparison_summary_without_runs():
    cmp = m.period_comparison(
        frame(
            [{"id": 1, "start_date_local": "2020-01-01", "distance_m": 5000, "moving_time_s": 1500}]
        ),
        AS_OF,
    )
    assert m.comparison_summary(cmp) == "No runs in this period yet."


# --- training load diagnosis ----------------------------------------------------


def _training(last_week):
    """Five steady weeks (8 km, 48 min, 130 bpm on Mon/Wed/Fri/Sun), then `last_week` rows."""
    rows, i = [], 0
    for day in pd.date_range("2026-10-19", "2026-11-20"):
        if day.day_of_week in (0, 2, 4, 6):
            i += 1
            rows.append(
                {
                    "id": i,
                    "start_date_local": day + pd.Timedelta(hours=7),
                    "distance_m": 8000,
                    "moving_time_s": 2880,
                    "average_heartrate": 130.0,
                }
            )
    for offset, (km, hr) in enumerate(last_week):
        if km:
            i += 1
            rows.append(
                {
                    "id": i,
                    "start_date_local": pd.Timestamp("2026-11-21 07:00")
                    + pd.Timedelta(days=offset),
                    "distance_m": km * 1000,
                    "moving_time_s": km * 360,
                    "average_heartrate": hr,
                }
            )
    return frame(rows)


def _diagnose(acts, end="2026-11-27", method="ewma"):
    loads = m.activity_load(acts, "distance", units="km")
    return m.load_diagnosis(
        acts, m.acwr(m.daily_load(loads, end=pd.Timestamp(end)), method), units="km"
    )


def test_diagnosis_explains_a_block_of_hard_consecutive_days():
    # Sat-Tue rest, then Wed 11 km, Thu 16 km hard, Fri 11 km
    acts = _training([(0, 0), (0, 0), (0, 0), (0, 0), (11, 132), (16, 155), (11, 128)])
    d = _diagnose(acts)
    assert d["band"] in ("Elevated", "High")
    text = " ".join(d["reasons"])
    assert "3 days in a row, from Wednesday to Friday" in text
    assert "The 16.0 km yesterday was a hard session: average HR 155 bpm" in text
    assert "of this week's load came in the last 3 days" in text
    assert "longer than your usual long run (8.0 km)" in text
    assert d["advice"] == m.LOAD_ADVICE[d["band"]]


def test_diagnosis_of_a_steady_week_is_optimal_and_quiet():
    acts = _training([(0, 0), (8, 130), (0, 0), (8, 130), (0, 0), (8, 130), (0, 0)])
    # On a run day: with runs every other day, EWMA swings between ~0.8 (rest day) and ~1.0.
    d = _diagnose(acts, end="2026-11-26")
    assert d["band"] == "Optimal"
    assert d["reasons"] == []
    assert d["summary"].startswith("In the optimal range")


def test_diagnosis_of_a_light_week_is_low():
    acts = _training([(0, 0), (0, 0), (0, 0), (4, 120), (0, 0), (0, 0), (0, 0)])
    d = _diagnose(acts)
    assert d["band"] == "Low"
    assert "less than your usual week" in " ".join(d["reasons"])


def test_diagnosis_needs_history():
    d = m.load_diagnosis(
        frame(
            [{"id": 1, "start_date_local": "2026-11-27", "distance_m": 5000, "moving_time_s": 1500}]
        ),
        pd.DataFrame(columns=["load", "ratio"]),
    )
    assert d["band"] == "Insufficient data" and d["reasons"] == []
