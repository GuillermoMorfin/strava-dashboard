import pytest

from strava_dashboard import db


@pytest.fixture
def con():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_activity(id, start="2024-03-04T07:00:00Z", sport_type="Run", **overrides):
    """A Strava SummaryActivity-shaped dict."""
    activity = {
        "id": id,
        "name": f"Run {id}",
        "sport_type": sport_type,
        "type": sport_type,
        "start_date": start,
        "start_date_local": start.replace("07:00", "08:00"),
        "distance": 8046.72,
        "moving_time": 2400,
        "elapsed_time": 2500,
        "total_elevation_gain": 30.0,
        "average_speed": 3.35,
        "average_heartrate": 150.0,
        "max_heartrate": 172.0,
        "has_heartrate": True,
        "workout_type": 0,
    }
    activity.update(overrides)
    return activity
