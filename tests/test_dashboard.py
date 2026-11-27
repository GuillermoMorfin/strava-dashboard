"""Smoke tests: render the Streamlit app headlessly against demo and empty databases."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from strava_dashboard import db
from strava_dashboard.demo import generate_demo

APP = str(Path(__file__).parents[1] / "src" / "strava_dashboard" / "dashboard.py")


def run_app(monkeypatch, db_path):
    monkeypatch.setenv("STRAVA_DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_UNITS", "mi")
    at = AppTest.from_file(APP, default_timeout=60)
    return at.run()


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.duckdb"
    con = db.connect(path)
    assert generate_demo(con) > 50
    con.close()
    return path


def test_renders_all_tabs_with_demo_data(monkeypatch, demo_db):
    at = run_app(monkeypatch, demo_db)
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == [
        "Weekly mileage",
        "Pace",
        "HR zones",
        "Training load",
        "Race predictions",
    ]
    metrics = {mt.label: mt.value for mt in at.metric}
    assert metrics["Acute : chronic ratio"] != "–"
    assert "/" in metrics["Predicted 5K / half"]
    assert len(at.get("vega_lite_chart")) == 6  # weekly, pace, 2x zones, 2x load


@pytest.mark.parametrize("load_method", ["trimp", "distance", "duration"])
def test_switching_settings_does_not_error(monkeypatch, demo_db, load_method):
    at = run_app(monkeypatch, demo_db)
    at.sidebar.selectbox[1].select(load_method).run()
    at.sidebar.radio[0].set_value("km").run()
    at.sidebar.radio[1].set_value("rolling").run()
    at.sidebar.selectbox[0].select("All time").run()
    assert not at.exception, at.exception


def test_missing_database_shows_instructions(monkeypatch, tmp_path):
    at = run_app(monkeypatch, tmp_path / "nope.duckdb")
    assert not at.exception
    assert "strava-dash auth" in at.info[0].value


def test_empty_database_shows_instructions(monkeypatch, tmp_path):
    path = tmp_path / "empty.duckdb"
    db.connect(path).close()
    at = run_app(monkeypatch, path)
    assert not at.exception
    assert "No runs" in at.info[0].value


def test_bad_heart_rate_settings_show_error(monkeypatch, demo_db):
    monkeypatch.setenv("MAX_HR", "44")
    monkeypatch.setenv("RESTING_HR", "179")
    at = run_app(monkeypatch, demo_db)
    assert not at.exception, at.exception
    assert "RESTING_HR" in at.error[0].value


def test_glitchy_recorded_max_hr_does_not_crash(monkeypatch, tmp_path):
    path = tmp_path / "glitch.duckdb"
    con = db.connect(path)
    generate_demo(con)
    con.execute("UPDATE activities SET max_heartrate = 44")
    con.close()
    monkeypatch.delenv("MAX_HR", raising=False)
    at = run_app(monkeypatch, path)
    assert not at.exception, at.exception
    assert at.sidebar.number_input[0].value == 120
