"""Thin Strava API v3 client for the endpoints this project needs."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx

API_BASE = "https://www.strava.com/api/v3"
RUN_SPORT_TYPES = frozenset({"Run", "TrailRun", "VirtualRun"})


class RateLimitError(RuntimeError):
    """Strava returned 429. Limits reset every 15 minutes and daily at midnight UTC."""

    def __init__(self, usage: str | None, limit: str | None):
        self.usage, self.limit = usage, limit
        super().__init__(
            f"Strava rate limit hit (usage {usage}, limit {limit}). "
            "Wait ~15 minutes and run sync again; progress so far is saved."
        )


def is_run(activity: dict) -> bool:
    return (activity.get("sport_type") or activity.get("type")) in RUN_SPORT_TYPES


class StravaClient:
    def __init__(
        self,
        access_token: Callable[[], str],
        http: httpx.Client | None = None,
        per_page: int = 200,
    ):
        # A callable so long syncs can pick up refreshed tokens.
        self._access_token = access_token
        self._http = http or httpx.Client(base_url=API_BASE, timeout=30)
        self.per_page = per_page

    def _get(self, path: str, params: dict | None = None):
        response = self._http.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status_code == 429:
            raise RateLimitError(
                response.headers.get("X-RateLimit-Usage"),
                response.headers.get("X-RateLimit-Limit"),
            )
        response.raise_for_status()
        return response.json()

    def iter_activities(self, after: int | None = None) -> Iterator[dict]:
        """Yield summary activities (all sports), oldest first when `after` is set."""
        page = 1
        while True:
            params = {"page": page, "per_page": self.per_page}
            if after is not None:
                params["after"] = after
            batch = self._get("/athlete/activities", params)
            if not batch:
                return
            yield from batch
            page += 1

    def get_heart_rate_zones(self) -> list[dict] | None:
        """Athlete HR zones as [{"min": 0, "max": 123}, ..., {"min": 180, "max": -1}].

        Returns None if the token lacks profile:read_all or no zones are set.
        """
        try:
            data = self._get("/athlete/zones")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                return None
            raise
        zones = (data.get("heart_rate") or {}).get("zones")
        return zones or None

    def get_hr_stream(self, activity_id: int) -> tuple[list[int], list[int]]:
        """Return (time_s, heartrate) samples; empty lists if the run has no HR."""
        try:
            data = self._get(
                f"/activities/{activity_id}/streams",
                {"keys": "time,heartrate", "key_by_type": "true"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return [], []
            raise
        hr = (data.get("heartrate") or {}).get("data") or []
        t = (data.get("time") or {}).get("data") or []
        if not hr or len(hr) != len(t):
            return [], []
        return t, hr
