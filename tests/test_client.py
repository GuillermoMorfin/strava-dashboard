import httpx
import pytest

from strava_dashboard.client import API_BASE, RateLimitError, StravaClient, is_run


def make_client(handler, per_page=2):
    http = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))
    return StravaClient(lambda: "tok", http=http, per_page=per_page)


def test_iter_activities_paginates_until_empty_and_sends_auth():
    pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}
    seen = []

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.url.path == "/api/v3/athlete/activities"
        page = int(request.url.params["page"])
        seen.append((page, request.url.params.get("after")))
        return httpx.Response(200, json=pages[page])

    ids = [a["id"] for a in make_client(handler).iter_activities(after=1700000000)]
    assert ids == [1, 2, 3]
    assert seen == [(1, "1700000000"), (2, "1700000000"), (3, "1700000000")]


def test_iter_activities_without_after_omits_param():
    def handler(request):
        assert "after" not in request.url.params
        return httpx.Response(200, json=[])

    assert list(make_client(handler).iter_activities()) == []


def test_rate_limit_raises_with_usage():
    def handler(request):
        return httpx.Response(
            429, headers={"X-RateLimit-Usage": "101,500", "X-RateLimit-Limit": "100,1000"}
        )

    with pytest.raises(RateLimitError) as info:
        list(make_client(handler).iter_activities())
    assert info.value.usage == "101,500"


def test_other_http_errors_propagate():
    with pytest.raises(httpx.HTTPStatusError):
        list(make_client(lambda r: httpx.Response(500)).iter_activities())


def test_heart_rate_zones():
    zones = [{"min": 0, "max": 120}, {"min": 120, "max": -1}]

    def handler(request):
        assert request.url.path == "/api/v3/athlete/zones"
        return httpx.Response(200, json={"heart_rate": {"custom_zones": False, "zones": zones}})

    assert make_client(handler).get_heart_rate_zones() == zones


@pytest.mark.parametrize("status", [401, 403])
def test_heart_rate_zones_without_scope_returns_none(status):
    assert make_client(lambda r: httpx.Response(status)).get_heart_rate_zones() is None


def test_hr_stream():
    def handler(request):
        assert request.url.path == "/api/v3/activities/9/streams"
        assert request.url.params["keys"] == "time,heartrate"
        return httpx.Response(
            200,
            json={
                "time": {"data": [0, 1, 2]},
                "heartrate": {"data": [100, 110, 120]},
            },
        )

    assert make_client(handler).get_hr_stream(9) == ([0, 1, 2], [100, 110, 120])


def test_hr_stream_missing_heartrate_or_404_is_empty():
    no_hr = make_client(lambda r: httpx.Response(200, json={"time": {"data": [0, 1]}}))
    assert no_hr.get_hr_stream(1) == ([], [])
    gone = make_client(lambda r: httpx.Response(404))
    assert gone.get_hr_stream(1) == ([], [])


@pytest.mark.parametrize(
    "activity, expected",
    [
        ({"sport_type": "Run"}, True),
        ({"sport_type": "TrailRun"}, True),
        ({"sport_type": "Ride"}, False),
        ({"type": "Run"}, True),
        ({}, False),
    ],
)
def test_is_run(activity, expected):
    assert is_run(activity) is expected
