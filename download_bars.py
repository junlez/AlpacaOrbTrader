#!/usr/bin/env python3
"""
Download historical bar data from Alpaca and save to a local cache directory.

Creates files named: <cache_dir>/<SYMBOL>_<timeframe>_<year>.json

Usage:
  python3 download_bars.py TSLA NVDA AMD --years 2021 2022 2023 2024
  python3 download_bars.py TSLA --years 2024 --timeframes 1Min 5Min
"""
import sys
import json
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCRIPT_DIR = Path(__file__).parent
DATA_BASE = "https://data.alpaca.markets"
DEFAULT_TIMEFRAMES = ["15Min", "5Min", "1Min"]
DEFAULT_CACHE_DIR = SCRIPT_DIR / "bar_cache"


def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def api_request(method, url, headers):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read().decode()}") from e


def fetch_all_bars(headers, symbol, timeframe, start_iso, end_iso):
    bars = []
    page_token = None
    while True:
        params = {
            "timeframe": timeframe,
            "start": start_iso,
            "end": end_iso,
            "limit": "10000",
            "feed": "iex",
            "adjustment": "raw",
        }
        if page_token:
            params["page_token"] = page_token
        url = f"{DATA_BASE}/v2/stocks/{symbol}/bars?" + urllib.parse.urlencode(params)
        resp = api_request("GET", url, headers)
        bars.extend(resp.get("bars") or [])
        page_token = resp.get("next_page_token")
        if not page_token:
            break
    return bars


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+", help="ticker symbols to download")
    parser.add_argument("--years", nargs="+", type=int, required=True, help="years to download (e.g. 2021 2022)")
    parser.add_argument("--timeframes", nargs="+", default=DEFAULT_TIMEFRAMES,
                        help=f"bar timeframes (default: {' '.join(DEFAULT_TIMEFRAMES)})")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help=f"directory to save bar files (default: bar_cache/)")
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR / "alpaca_PAPER.env")
    parser.add_argument("--overwrite", action="store_true", help="re-download even if file already exists")
    args = parser.parse_args()

    args.cache_dir.mkdir(exist_ok=True)
    env = load_env(args.env_file)
    headers = {
        "APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"],
    }

    symbols = [s.upper() for s in args.symbols]
    total = len(symbols) * len(args.years) * len(args.timeframes)
    done = 0

    for symbol in symbols:
        for year in sorted(args.years):
            # Full calendar year in ET, converted to UTC for the API
            start_dt = datetime(year, 1, 1, tzinfo=ET).astimezone(timezone.utc)
            end_dt = datetime(year + 1, 1, 1, tzinfo=ET).astimezone(timezone.utc)
            start_iso = start_dt.isoformat()
            end_iso = end_dt.isoformat()

            for tf in args.timeframes:
                done += 1
                out_path = args.cache_dir / f"{symbol}_{tf}_{year}.json"
                if out_path.exists() and not args.overwrite:
                    print(f"[{done}/{total}] {out_path.name} already exists, skipping (--overwrite to force)")
                    continue

                print(f"[{done}/{total}] Downloading {symbol} {tf} {year}...", end=" ", flush=True)
                bars = fetch_all_bars(headers, symbol, tf, start_iso, end_iso)
                out_path.write_text(json.dumps(bars))
                print(f"{len(bars)} bars -> {out_path.name}")

    print(f"\nDone. Cache directory: {args.cache_dir}")


if __name__ == "__main__":
    main()
