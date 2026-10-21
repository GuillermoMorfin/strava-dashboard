"""Incremental sync from Strava into DuckDB."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from . import db
from .client import RateLimitError, StravaClient, is_run


@dataclass
class SyncResult:
    activities: int = 0
    streams: int = 0
    zones_saved: bool = False
    rate_limited: bool = False
    streams_remaining: int = 0


def sync(
    con: duckdb.DuckDBPyConnection,
    client: StravaClient,
    *,
    full: bool = False,
    stream_limit: int = 50,
    batch_size: int = 200,
) -> SyncResult:
    """Fetch new runs, HR zones, and up to `stream_limit` HR streams.

    Passing `after` (0 for a full sync) makes Strava return activities oldest
    first, so saving batch by batch keeps progress resumable after a rate limit.
    """
    result = SyncResult()
    after = 0 if full else (db.latest_start_epoch(con) or 0)

    batch: list[dict] = []
    try:
        for activity in client.iter_activities(after=after):
            if is_run(activity):
                batch.append(activity)
            if len(batch) >= batch_size:
                result.activities += db.upsert_activities(con, batch)
                batch = []
    except RateLimitError:
        result.rate_limited = True
    finally:
        result.activities += db.upsert_activities(con, batch)

    if result.rate_limited:
        return result

    try:
        zones = client.get_heart_rate_zones()
        if zones:
            db.save_hr_zones(con, zones)
            result.zones_saved = True

        for activity_id in db.activities_missing_streams(con, stream_limit):
            time_s, heartrate = client.get_hr_stream(activity_id)
            db.save_hr_stream(con, activity_id, time_s, heartrate)
            result.streams += 1
    except RateLimitError:
        result.rate_limited = True

    result.streams_remaining = len(db.activities_missing_streams(con, 1_000_000))
    return result
