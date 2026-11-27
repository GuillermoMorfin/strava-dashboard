"""The hosted-demo entry point and its daily demo refresh."""

import os
from datetime import datetime, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from strava_dashboard import db
from strava_dashboard.demo import ensure_fresh_demo

ROOT = Path(__file__).parents[1]


def test_ensure_fresh_demo_builds_then_skips(tmp_path):
    path = tmp_path / "sub" / "demo.duckdb"
    assert ensure_fresh_demo(path) is True
    con = db.connect(path, read_only=True)
    assert len(db.load_activities(con)) > 50
    con.close()
    assert ensure_fresh_demo(path) is False


def test_ensure_fresh_demo_rebuilds_stale_file(tmp_path):
    path = tmp_path / "demo.duckdb"
    ensure_fresh_demo(path)
    old = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(path, (old, old))
    assert ensure_fresh_demo(path) is True
    assert not list(tmp_path.glob(".*.tmp"))


def test_hosted_demo_renders_with_banner(monkeypatch):
    # The app overwrites these in os.environ; setting them here makes monkeypatch
    # restore them afterwards so later tests aren't left in demo mode.
    for key in ("STRAVA_UNITS", "STRAVA_DEMO_MODE"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("STRAVA_DB_PATH", "/should/be/ignored.duckdb")
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90).run()
    assert not at.exception, at.exception
    assert "synthetic data" in at.info[0].value
    assert len(at.tabs) == 6
    assert (ROOT / "data" / "demo.duckdb").exists()
