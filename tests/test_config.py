from pathlib import Path

import pytest

from strava_dashboard.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in [
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_REDIRECT_PORT",
        "STRAVA_DB_PATH",
        "STRAVA_TOKEN_PATH",
        "STRAVA_UNITS",
        "MAX_HR",
        "RESTING_HR",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    s = Settings.from_env(dotenv=False)
    assert s.units == "mi"
    assert s.db_path == Path("data/strava.duckdb")
    assert s.redirect_uri == "http://localhost:8765/callback"
    assert s.max_hr is None
    assert s.resting_hr == 60


def test_reads_env(monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shh")
    monkeypatch.setenv("STRAVA_UNITS", "KM")
    monkeypatch.setenv("STRAVA_REDIRECT_PORT", "9000")
    monkeypatch.setenv("MAX_HR", "190")
    s = Settings.from_env(dotenv=False)
    assert (s.client_id, s.client_secret, s.units, s.max_hr) == ("123", "shh", "km", 190)
    assert s.redirect_uri == "http://localhost:9000/callback"
    s.require_credentials()


def test_missing_credentials():
    with pytest.raises(ConfigError, match="STRAVA_CLIENT_ID"):
        Settings.from_env(dotenv=False).require_credentials()


def test_invalid_units(monkeypatch):
    monkeypatch.setenv("STRAVA_UNITS", "furlongs")
    with pytest.raises(ConfigError):
        Settings.from_env(dotenv=False)


def test_invalid_int(monkeypatch):
    monkeypatch.setenv("MAX_HR", "high")
    with pytest.raises(ConfigError, match="MAX_HR"):
        Settings.from_env(dotenv=False)


@pytest.mark.parametrize(
    ("max_hr", "resting", "match"),
    [
        ("44", "179", "RESTING_HR must be between"),
        ("44", "60", "MAX_HR must be between .* got 44 \\(are MAX_HR and RESTING_HR swapped"),
        ("250", "60", "MAX_HR must be between"),
    ],
)
def test_implausible_heart_rates(monkeypatch, max_hr, resting, match):
    monkeypatch.setenv("MAX_HR", max_hr)
    monkeypatch.setenv("RESTING_HR", resting)
    with pytest.raises(ConfigError, match=match):
        Settings.from_env(dotenv=False)
