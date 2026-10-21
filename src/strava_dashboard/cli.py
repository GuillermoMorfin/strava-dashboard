"""Command-line entry point: `strava-dash auth | sync | dashboard | demo`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import httpx

from . import db
from .auth import AuthError, TokenStore, authorize_interactive, get_valid_tokens
from .client import StravaClient
from .config import ConfigError, Settings
from .sync import sync


def cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    settings.require_credentials()
    tokens = authorize_interactive(
        settings.client_id,
        settings.client_secret,
        settings.redirect_uri,
        settings.redirect_port,
        TokenStore(settings.token_path),
        open_browser=not args.no_browser,
    )
    print(f"Authorized athlete {tokens.athlete_id}. Tokens saved to {settings.token_path}.")
    return 0


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    settings.require_credentials()
    store = TokenStore(settings.token_path)
    with httpx.Client(timeout=30) as auth_http:

        def access_token() -> str:
            return get_valid_tokens(
                store, auth_http, settings.client_id, settings.client_secret
            ).access_token

        access_token()  # fail fast if not authorized
        con = db.connect(settings.db_path)
        try:
            result = sync(
                con, StravaClient(access_token), full=args.full, stream_limit=args.streams
            )
        finally:
            con.close()

    print(f"Synced {result.activities} run(s) and {result.streams} HR stream(s).")
    if not result.zones_saved:
        print("HR zones not available from Strava; the dashboard will estimate them from max HR.")
    if result.streams_remaining:
        print(f"{result.streams_remaining} HR stream(s) still to fetch; run sync again later.")
    if result.rate_limited:
        print("Stopped early: Strava rate limit reached. Progress is saved; retry in ~15 min.")
    return 0


def cmd_demo(settings: Settings, args: argparse.Namespace) -> int:
    from .demo import generate_demo

    path = Path(args.db)
    if path.exists():
        path.unlink()
    con = db.connect(path)
    try:
        n = generate_demo(con)
    finally:
        con.close()
    print(f"Wrote {n} synthetic runs to {path}.")
    print(f"View them with: strava-dash dashboard --db {path}")
    return 0


def cmd_dashboard(settings: Settings, args: argparse.Namespace) -> int:
    app = Path(__file__).with_name("dashboard.py")
    env = dict(os.environ)
    if args.db:
        env["STRAVA_DB_PATH"] = str(args.db)
    command = [sys.executable, "-m", "streamlit", "run", str(app), *args.streamlit_args]
    return subprocess.call(command, env=env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strava-dash", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="Authorize with Strava (one-time, opens a browser).")
    p.add_argument("--no-browser", action="store_true", help="Print the URL instead.")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("sync", help="Download new runs into DuckDB.")
    p.add_argument("--full", action="store_true", help="Re-download all activities.")
    p.add_argument(
        "--streams",
        type=int,
        default=50,
        metavar="N",
        help="Max HR streams to fetch this run (each costs one API request; default 50).",
    )
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("dashboard", help="Launch the Streamlit dashboard.")
    p.add_argument("--db", help="DuckDB file to open (default: STRAVA_DB_PATH).")
    p.add_argument("streamlit_args", nargs=argparse.REMAINDER, help="Passed to streamlit.")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("demo", help="Write synthetic data to explore the dashboard.")
    p.add_argument("--db", default="data/demo.duckdb", help="Output file (overwritten).")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(Settings.from_env(), args)
    except (ConfigError, AuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"Strava API error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
