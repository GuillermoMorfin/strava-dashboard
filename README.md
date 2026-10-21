# strava-dashboard

Pull your running activities from the Strava API into a local DuckDB file and explore
them in a Streamlit dashboard:

- **Weekly mileage**: distance, run count, hours and long run per week
- **Pace trends**: every run plus a rolling median
- **Heart-rate zone distribution**: time in zone overall and per week, from second-by-second HR streams
- **Acute:chronic training load**: TRIMP, distance or duration load with EWMA or rolling ACWR
- **Race predictions**: 5K / 10K / half / marathon by Daniels VDOT and Riegel

Everything runs locally. Your data and tokens never leave your machine except to talk to Strava.

## Quick start (no Strava account needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

strava-dash demo                               # ~200 days of synthetic runs → data/demo.duckdb
strava-dash dashboard --db data/demo.duckdb    # opens http://localhost:8501
```

Requires Python 3.11+.

## Connecting your Strava account

### 1. Create a Strava API application (one time)

1. Go to <https://www.strava.com/settings/api> and create an app.
2. Fill in any name, category and website (e.g. `http://localhost`).
3. Set **Authorization Callback Domain** to `localhost`. This must match the redirect
   URL the app uses (`http://localhost:8765/callback`), or Strava will reject the login.
4. Copy the **Client ID** and **Client Secret**.

### 2. Configure

```bash
cp .env.example .env
# edit .env: set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET
```

`.env` and the token file are git-ignored. Never commit them.

### 3. Authorize

```bash
strava-dash auth
```

This opens Strava in your browser. Approve the request, leaving **"View data about your
private activities"** checked, and you'll land on a "You can close this tab" page.

What happens under the hood (OAuth 2.0 authorization-code flow):

1. A one-shot HTTP server starts on `127.0.0.1:8765` **before** the browser opens, so a
   busy port fails immediately (change it with `STRAVA_REDIRECT_PORT`, keeping the callback
   domain `localhost`).
2. The authorize URL requests scopes `read,activity:read_all,profile:read_all` and carries
   a random `state` value.
3. Strava redirects to `/callback?code=…&state=…&scope=…`. The app rejects the callback if
   `state` doesn't match (CSRF protection) or if `activity:read_all` wasn't granted.
4. The code is exchanged at `https://www.strava.com/oauth/token` for an access token
   (valid ~6 hours) and a refresh token.
5. Tokens are written atomically to `.strava_tokens.json` with `0600` permissions.

You only do this once. Every later command checks the token's expiry and refreshes it
automatically, saving the new refresh token (Strava may rotate it).

`profile:read_all` is optional. It lets the app read the HR zones from your Strava
profile. Without it, zones are estimated from max HR.

Use `strava-dash auth --no-browser` to print the URL instead of opening it.

### 4. Sync and view

```bash
strava-dash sync         # incremental: only runs newer than the latest one stored
strava-dash dashboard
```

| Command | What it does |
|---|---|
| `strava-dash sync` | Downloads new runs (Run, TrailRun, VirtualRun), your HR zones, and up to 50 HR streams |
| `strava-dash sync --streams 200` | Fetch more HR streams this time (each costs one API request) |
| `strava-dash sync --full` | Re-download every activity (picks up edits to old runs) |
| `strava-dash dashboard [--db PATH]` | Launch the dashboard; extra args go to `streamlit run` |
| `strava-dash demo [--db PATH]` | Write synthetic data (overwrites PATH) |

**Rate limits.** Strava limits requests per 15 minutes and per day; see your app's API
settings page for the exact numbers. One activity-list request returns 200 runs, but HR
streams cost one request each, so a first sync with years of history takes a few runs of
`sync`. Sync is resumable: activities are requested oldest-first and saved in batches,
so if Strava returns `429`, everything fetched so far is kept and the next run continues
from there.

## Configuration

Set these in `.env` or as environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | — | From your Strava API app (needed for `auth` and `sync`) |
| `STRAVA_REDIRECT_PORT` | `8765` | Port for the local OAuth callback |
| `STRAVA_DB_PATH` | `data/strava.duckdb` | DuckDB file |
| `STRAVA_TOKEN_PATH` | `.strava_tokens.json` | Where tokens are stored |
| `STRAVA_UNITS` | `mi` | Default units in the dashboard: `mi` or `km` |
| `MAX_HR` | highest max HR in your runs | Used for TRIMP and estimated zones |
| `RESTING_HR` | `60` | Used for TRIMP |

Units, max/resting HR, the period, and the load and ACWR models can also be changed in the dashboard sidebar.

## How the metrics are calculated

All metric code is in [`src/strava_dashboard/metrics.py`](src/strava_dashboard/metrics.py)
as pure pandas functions.

**Weekly mileage.** Runs are grouped by Monday-start week using the run's *local* start
time, and empty weeks show as zero.

**Pace.** Moving time ÷ distance for each run of 1 km or more, plus a rolling median over
the last *N* runs (10 by default). The median keeps one race or one recovery jog from
swinging the trend.

**HR zones.** Time in zone comes from the per-second HR stream: each interval between
samples is credited to the zone of its starting sample, and gaps over 30 s (pauses) are
skipped. Zones come from your Strava profile when available, otherwise they're set at
60/70/80/90% of max HR. Streams are stored raw, so changing zones recomputes history
without re-downloading anything.

**Training load.**
- *TRIMP* (default): Banister's `minutes × HRr × 0.64 × e^(1.92 × HRr)`, where
  `HRr = (avg HR − resting) / (max − resting)`. Runs without HR are estimated from your
  median TRIMP per minute, and the dashboard shows what share was estimated. (1.92 is the
  standard male coefficient; 1.67 is used for women. Change `k` in `banister_trimp` if needed.)
- *Distance* or *Duration*: simpler options that need no heart-rate data.

**Acute:chronic workload ratio.** Daily load (rest days count as 0) feeds two models:
- *EWMA* (default): exponentially weighted means with α = 2/(N+1) for 7 and 28 days
  (Williams et al., 2017). This model reacts more smoothly than rolling sums.
- *Rolling*: plain 7-day ÷ 28-day means.

The ratio stays blank until 28 days of history exist. The bands (<0.8 low,
0.8–1.3 "optimal", 1.3–1.5 elevated, >1.5 high) come from team-sport research and are
debated, so treat a spike as a prompt to look at your training rather than as an injury
forecast.

**Race predictions.** The app takes the run with the highest **VDOT** (Daniels–Gilbert
oxygen-cost equations) among runs of 3 km or more in the lookback window (90 days by
default), ignoring implausible GPS speeds. It then predicts each distance two ways:
- *VDOT*: solves the same equations for the time at each distance (matches Daniels'
  published tables within seconds; see the tests).
- *Riegel*: `T2 = T1 × (D2/D1)^1.06`.

Predictions are only as good as the effort behind them. A recent race or hard time trial
works best, and marathon predictions run optimistic without long-run training.

## Project layout

```
src/strava_dashboard/
  config.py     settings from env / .env
  auth.py       OAuth: authorize URL, callback server, token exchange/refresh/storage
  client.py     Strava API client (activities, zones, HR streams, 429 handling)
  db.py         DuckDB schema, upserts, loaders
  sync.py       incremental, resumable sync
  metrics.py    weekly volume, pace, HR zones, TRIMP, ACWR, VDOT/Riegel
  dashboard.py  Streamlit app
  demo.py       synthetic data generator
  cli.py        `strava-dash` command
tests/          pytest suite (HTTP mocked; dashboard smoke-tested with Streamlit AppTest)
```

### Database schema

| Table | Contents |
|---|---|
| `activities` | One row per run: distance, times, HR averages, `start_date` (UTC) and `start_date_local`, plus the full `raw` JSON |
| `hr_streams` | `time_s[]` and `heartrate[]` per run; empty lists mean "fetched, no HR" |
| `hr_zones` | Your Strava HR zones (`max_bpm = -1` is open-ended) |

You can query the data directly:

```bash
duckdb data/strava.duckdb "SELECT date_trunc('month', start_date_local) m, round(sum(distance_m)/1609.344) mi FROM activities GROUP BY m ORDER BY m"
```

## Development

```bash
pip install -e ".[dev]"
pytest              # full suite; no network or Strava account needed
ruff check . && ruff format --check .
```

Tests mock all HTTP with `httpx.MockTransport`, use in-memory DuckDB, check the metrics
against known values (Daniels' VDOT tables, hand-computed TRIMP/ACWR), and render the
dashboard headlessly against demo data.

## Troubleshooting

| Problem | Fix |
|---|---|
| Strava says `redirect_uri invalid` | Set the app's Authorization Callback Domain to `localhost` |
| `Missing required scope(s): activity:read_all` | Re-run `strava-dash auth` and keep the private-activities box checked |
| `Can't listen on port 8765` | Set `STRAVA_REDIRECT_PORT` to a free port |
| `Not authorized yet` | Run `strava-dash auth` |
| `Strava rate limit reached` | Wait ~15 minutes and run `sync` again. Progress is saved |
| Dashboard says database is locked | A `sync` is writing to it. Wait for it to finish |
| HR zone tab is empty | Run `sync` a few more times to fetch streams (`--streams N`) |
| An edited old run looks stale | `strava-dash sync --full` |

To disconnect the app, revoke it at <https://www.strava.com/settings/apps> and delete
`.strava_tokens.json`.
