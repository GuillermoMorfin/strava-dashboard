"""Entry point for the hosted demo on Streamlit Community Cloud.

Always serves synthetic data: it never reads a Strava account or your real
database. Run locally with `streamlit run streamlit_app.py`; for your own data
use `strava-dash dashboard` instead.
"""

import os
import runpy
from datetime import date
from pathlib import Path

import streamlit as st

import strava_dashboard
from strava_dashboard.demo import ensure_fresh_demo

DEMO_DB = Path(__file__).parent / "data" / "demo.duckdb"

# Set unconditionally so a real STRAVA_DB_PATH in the shell or .env is never overwritten.
os.environ["STRAVA_DB_PATH"] = str(DEMO_DB)
os.environ["STRAVA_UNITS"] = "km"
os.environ["STRAVA_DEMO_MODE"] = "1"


@st.cache_resource(show_spinner="Generating demo runs…")
def prepare_demo(day: str) -> None:
    """Runs once per day per server process (`day` is the cache key)."""
    ensure_fresh_demo(DEMO_DB)


prepare_demo(date.today().isoformat())
runpy.run_path(str(Path(strava_dashboard.__file__).with_name("dashboard.py")), run_name="__main__")
