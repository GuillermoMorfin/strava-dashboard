from conftest import make_activity

from strava_dashboard import db
from strava_dashboard.client import RateLimitError
from strava_dashboard.sync import sync


class FakeClient:
    def __init__(
        self,
        activities,
        zones=None,
        streams=None,
        limit_after_activities=None,
        limit_after_streams=None,
    ):
        self.activities = activities
        self.zones = zones
        self.streams = streams or {}
        self.limit_after_activities = limit_after_activities
        self.limit_after_streams = limit_after_streams
        self.after_seen = []
        self.stream_calls = 0

    def iter_activities(self, after=None):
        self.after_seen.append(after)
        for i, a in enumerate(self.activities):
            if self.limit_after_activities is not None and i >= self.limit_after_activities:
                raise RateLimitError("100,100", "100,1000")
            yield a

    def get_heart_rate_zones(self):
        return self.zones

    def get_hr_stream(self, activity_id):
        if self.limit_after_streams is not None and self.stream_calls >= self.limit_after_streams:
            raise RateLimitError("100,100", "100,1000")
        self.stream_calls += 1
        return self.streams.get(activity_id, ([], []))


ZONES = [{"min": 0, "max": 120}, {"min": 120, "max": -1}]


def test_first_sync_keeps_runs_only_and_fetches_zones_and_streams(con):
    client = FakeClient(
        [make_activity(1), make_activity(2, sport_type="Ride"), make_activity(3)],
        zones=ZONES,
        streams={1: ([0, 1], [130, 131])},
    )
    result = sync(con, client)
    assert client.after_seen == [0]
    assert result.activities == 2
    assert result.zones_saved
    assert result.streams == 2
    assert result.streams_remaining == 0
    assert set(db.load_activities(con)["id"]) == {1, 3}
    assert db.load_hr_zones(con) == [(0, 120), (120, -1)]


def test_incremental_sync_uses_latest_start(con):
    db.upsert_activities(con, [make_activity(1, start="2024-01-02T00:00:00Z")])
    client = FakeClient([])
    sync(con, client)
    assert client.after_seen == [1704153600]
    full = FakeClient([])
    sync(con, full, full=True)
    assert full.after_seen == [0]


def test_rate_limit_during_activities_saves_progress(con):
    client = FakeClient([make_activity(i) for i in range(1, 6)], limit_after_activities=3)
    result = sync(con, client, batch_size=2)
    assert result.rate_limited
    assert result.activities == 3
    assert len(db.load_activities(con)) == 3
    assert client.stream_calls == 0  # stops before using more quota


def test_stream_limit_and_rate_limit_during_streams(con):
    activities = [make_activity(i, start=f"2024-01-0{i}T00:00:00Z") for i in range(1, 5)]
    result = sync(con, FakeClient(activities), stream_limit=2)
    assert (result.streams, result.streams_remaining) == (2, 2)

    result = sync(con, FakeClient([], limit_after_streams=1))
    assert result.rate_limited
    assert (result.streams, result.streams_remaining) == (1, 1)
