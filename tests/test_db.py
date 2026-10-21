from datetime import datetime

from conftest import make_activity

from strava_dashboard import db


def test_upsert_inserts_and_replaces(con):
    db.upsert_activities(con, [make_activity(1), make_activity(2)])
    db.upsert_activities(con, [make_activity(1, name="Renamed")])
    df = db.load_activities(con)
    assert list(df["id"]) == [1, 2]
    assert df.loc[df["id"] == 1, "name"].item() == "Renamed"


def test_timestamps_utc_and_local(con):
    db.upsert_activities(
        con,
        [make_activity(1, start="2024-03-04T07:00:00Z", start_date_local="2024-03-04T02:00:00Z")],
    )
    row = db.load_activities(con).iloc[0]
    assert row["start_date"] == datetime(2024, 3, 4, 7, 0)
    assert row["start_date_local"] == datetime(2024, 3, 4, 2, 0)


def test_latest_start_epoch(con):
    assert db.latest_start_epoch(con) is None
    db.upsert_activities(
        con,
        [
            make_activity(1, start="2024-01-01T00:00:00Z"),
            make_activity(2, start="2024-01-02T00:00:00Z"),
        ],
    )
    assert db.latest_start_epoch(con) == 1704153600  # 2024-01-02T00:00:00Z


def test_hr_zones_roundtrip_replaces(con):
    db.save_hr_zones(con, [{"min": 0, "max": 100}, {"min": 100, "max": -1}])
    db.save_hr_zones(con, [{"min": 0, "max": 130}, {"min": 130, "max": -1}])
    assert db.load_hr_zones(con) == [(0, 130), (130, -1)]


def test_streams_missing_and_saved(con):
    db.upsert_activities(
        con,
        [
            make_activity(1, start="2024-01-01T00:00:00Z"),
            make_activity(2, start="2024-01-02T00:00:00Z"),
            make_activity(3, has_heartrate=False),
        ],
    )
    assert db.activities_missing_streams(con, 10) == [2, 1]  # newest first, HR only
    db.save_hr_stream(con, 2, [0, 1], [120, 125])
    db.save_hr_stream(con, 1, [], [])  # fetched but empty
    assert db.activities_missing_streams(con, 10) == []
    streams = db.load_hr_streams(con)
    assert list(streams["activity_id"]) == [2]
    assert list(streams.iloc[0]["heartrate"]) == [120, 125]


def test_file_db_persists(tmp_path):
    path = tmp_path / "sub" / "s.duckdb"
    con = db.connect(path)
    db.upsert_activities(con, [make_activity(1)])
    con.close()
    ro = db.connect(path, read_only=True)
    assert len(db.load_activities(ro)) == 1
