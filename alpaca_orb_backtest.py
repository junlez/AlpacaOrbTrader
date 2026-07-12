#!/usr/bin/env python3
"""
Backtest the ORB strategy implemented in alpaca_orb.py over historical bars.

Simulates the live script being invoked every minute during regular trading
hours: opening range from the first 15-minute candle, entries evaluated on
each completed 5-minute candle close, stop/target/time-exit evaluated minute
by minute using 1-minute bars (the same resolution the live script effectively
gets by polling current price every minute).

Usage: python3 alpaca_orb_backtest.py TSLA [--days 30]
"""
import sys
import json
import bisect
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
DEFAULT_CACHE_DIR = SCRIPT_DIR / "bar_cache"

OPENING_RANGE_MINUTES = 15
EXIT_BUFFER_MINUTES = 10
TRADE_QTY = 1
DEFAULT_STOP_PCT = 75.0
DEFAULT_REWARD_PCT = 175.0


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


def get_calendar(trade_base, headers, start_date, end_date):
    url = f"{trade_base}/v2/calendar?start={start_date}&end={end_date}"
    return api_request("GET", url, headers)


def get_all_bars(headers, symbol, timeframe, start, end, cache_dir=None):
    # If a cache directory is given, try to load from pre-downloaded files first.
    # Falls back to the API for any year not found in the cache.
    if cache_dir is not None:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        years = range(start_dt.year, end_dt.year + 1)
        bars = []
        missing_years = []
        for year in years:
            cache_file = Path(cache_dir) / f"{symbol}_{timeframe}_{year}.json"
            if cache_file.exists():
                year_bars = json.loads(cache_file.read_text())
                # Filter to the requested window (file covers the full year)
                bars.extend(
                    b for b in year_bars
                    if start <= b["t"].replace("Z", "+00:00") < end
                       or start <= b["t"] < end
                )
            else:
                missing_years.append(year)
        if missing_years:
            print(f"  [cache miss] {symbol} {timeframe} {missing_years} — fetching from API")
            for year in missing_years:
                y_start = f"{year}-01-01T00:00:00+00:00"
                y_end = f"{year + 1}-01-01T00:00:00+00:00"
                bars.extend(get_all_bars(headers, symbol, timeframe, y_start, y_end))
            bars.sort(key=lambda b: b["t"])
        return bars

    bars = []
    page_token = None
    while True:
        params = {
            "timeframe": timeframe,
            "start": start,
            "end": end,
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


def build_index(bars):
    """Pre-parse timestamps into parallel (keys, bars) arrays for O(log n) slicing."""
    pairs = sorted(
        ((datetime.fromisoformat(b["t"].replace("Z", "+00:00")), b) for b in bars),
        key=lambda x: x[0],
    )
    keys = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    return keys, vals


def bars_between(index, start_dt, end_dt):
    """O(log n) slice of a pre-built bar index between [start_dt, end_dt)."""
    keys, vals = index
    lo = bisect.bisect_left(keys, start_dt)
    hi = bisect.bisect_left(keys, end_dt)
    return list(zip(keys[lo:hi], vals[lo:hi]))


def parse_timeframe_minutes(tf):
    """Convert an Alpaca timeframe string like '5Min' or '10Min' to integer minutes."""
    tf = tf.strip()
    if tf.endswith("Min"):
        return int(tf[:-3])
    if tf.endswith("Hour"):
        return int(tf[:-4]) * 60
    raise ValueError(f"Unsupported timeframe: {tf}")


def simulate_day(symbol, day, calendar_day, idx_15m, idx_entry, idx_1m, entry_field="vw", exit_mode="prev-hl", stop_pct=DEFAULT_STOP_PCT, reward_pct=DEFAULT_REWARD_PCT, entry_timeframe="10Min"):
    date_str = day
    open_dt = datetime.strptime(f"{date_str} {calendar_day['open']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    close_dt = datetime.strptime(f"{date_str} {calendar_day['close']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    range_end_dt = open_dt + timedelta(minutes=OPENING_RANGE_MINUTES)
    exit_cutoff_dt = close_dt - timedelta(minutes=EXIT_BUFFER_MINUTES)

    range_bar = bars_between(idx_15m, open_dt.astimezone(timezone.utc), range_end_dt.astimezone(timezone.utc))
    if not range_bar:
        return {"date": date_str, "skipped": "no_opening_range_data"}
    range_high = range_bar[0][1]["h"]
    range_low = range_bar[0][1]["l"]

    entry_candles = bars_between(idx_entry, range_end_dt.astimezone(timezone.utc), exit_cutoff_dt.astimezone(timezone.utc))

    entry = None
    for t, b in entry_candles:
        signal_price = b[entry_field]
        if signal_price > range_high:
            entry = {"time": t, "price": signal_price, "side": "long"}
            break
        elif signal_price < range_low:
            entry = {"time": t, "price": signal_price, "side": "short"}
            break

    if entry is None:
        return {
            "date": date_str, "range_high": range_high, "range_low": range_low,
            "entered": False, "exit_reason": "no_entry",
        }

    side = entry["side"]
    # The breakout candle closes at entry["time"] + entry_timeframe duration.
    # The market order fills at the open of the next 1-minute bar, which is the
    # first price available after the signal. Fall back to the signal price if missing.
    tf_minutes = parse_timeframe_minutes(entry_timeframe)
    fill_time = entry["time"] + timedelta(minutes=tf_minutes)
    fill_bars = bars_between(idx_1m, fill_time, fill_time + timedelta(minutes=1))
    entry_price = fill_bars[0][1]["o"] if fill_bars else entry["price"]

    range_size = range_high - range_low
    stop_distance = (stop_pct / 100.0) * range_size
    reward_distance = (reward_pct / 100.0) * range_size

    if side == "long":
        stop_price = range_high - stop_distance
        target_price = range_high + reward_distance
    else:
        stop_price = range_low + stop_distance
        target_price = range_low - reward_distance

    one_min = bars_between(idx_1m, fill_time, close_dt.astimezone(timezone.utc))

    exit_price = None
    exit_reason = None
    exit_time = None

    if exit_mode == "prev-hl":
        # Experimental: at minute x, check the previous bar's high/low for
        # stop/target detection, then fill at the current bar's open.
        for i, (t, b) in enumerate(one_min):
            if i == 0:
                # No previous bar yet; only check time exit at cutoff.
                if t >= exit_cutoff_dt.astimezone(timezone.utc):
                    exit_price, exit_reason, exit_time = b["o"], "time", t
                continue
            prev_b = one_min[i - 1][1]
            hit_stop = (side == "long" and prev_b["l"] <= stop_price) or (side == "short" and prev_b["h"] >= stop_price)
            hit_target = (side == "long" and prev_b["h"] >= target_price) or (side == "short" and prev_b["l"] <= target_price)
            if hit_stop:
                exit_price, exit_reason, exit_time = b["o"], "stop", t
                break
            elif hit_target:
                exit_price, exit_reason, exit_time = b["o"], "target", t
                break
            elif t >= exit_cutoff_dt.astimezone(timezone.utc):
                exit_price, exit_reason, exit_time = b["o"], "time", t
                break
    else:
        # Default: simulate the live script's once-per-minute polling using each
        # bar's close for detection, filling at the next bar's open.
        pending = None  # (reason, t) detected at bar close, fill next bar's open
        for i, (t, b) in enumerate(one_min):
            if pending is not None:
                exit_price, exit_reason, exit_time = b["o"], pending[0], pending[1]
                break
            close = b["c"]
            hit_stop = (side == "long" and close <= stop_price) or (side == "short" and close >= stop_price)
            hit_target = (side == "long" and close >= target_price) or (side == "short" and close <= target_price)
            if hit_stop:
                pending = ("stop", t)
            elif hit_target:
                pending = ("target", t)
            elif t >= exit_cutoff_dt.astimezone(timezone.utc) - timedelta(minutes=1):
                # The bar ending just before 3:50pm is the last one the live script
                # sees before deciding to exit. Queue a time exit so the next bar's
                # open (the 3:50pm bar open) is used as the fill price.
                pending = ("time", t)

    if exit_price is None:
        last_t, last_b = one_min[-1] if one_min else (fill_time, {"c": entry_price})
        exit_price, exit_reason, exit_time = last_b["c"], "time", last_t

    pnl = (exit_price - entry_price) * TRADE_QTY if side == "long" else (entry_price - exit_price) * TRADE_QTY
    pnl_pct = pnl / (entry_price * TRADE_QTY) * 100

    return {
        "date": date_str, "range_high": range_high, "range_low": range_low,
        "entered": True, "side": side, "entry_time": fill_time.astimezone(ET).isoformat(),
        "entry_price": entry_price, "stop_price": stop_price, "target_price": target_price,
        "exit_time": exit_time.astimezone(ET).isoformat(), "exit_price": exit_price,
        "exit_reason": exit_reason, "pnl": pnl, "pnl_pct": pnl_pct,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("--days", type=int, default=30, help="lookback window if --start/--end not given")
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--entry-timeframe", default="10Min",
                         help="candle resolution used for the entry/breakout signal (e.g. 1Min, 2Min, 3Min, 4Min, 5Min, 10Min)")
    parser.add_argument("--entry-field", choices=["vw", "c"], default="vw",
                         help="bar field used for breakout signal: 'vw' (VWAP, default) or 'c' (close)")
    parser.add_argument("--exit-mode", choices=["prev-hl", "close"], default="prev-hl",
                         help="exit detection: 'prev-hl' (previous bar high/low, default) or 'close' (current bar close)")
    parser.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT,
                         help="stop distance as %% of opening range size (default: 100)")
    parser.add_argument("--reward-pct", type=float, default=DEFAULT_REWARD_PCT,
                         help="reward distance as %% of opening range size (default: 150)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                         help="directory of pre-downloaded bar files from download_bars.py (default: bar_cache/)")
    parser.add_argument("--no-cache", action="store_true",
                         help="ignore local cache and always fetch from Alpaca API")
    args = parser.parse_args()
    symbol = args.symbol.upper()

    env = load_env(SCRIPT_DIR / "alpaca_PAPER.env")
    trade_base = env["APCA_API_BASE_URL"].rstrip("/")
    if trade_base.endswith("/v2"):
        trade_base = trade_base[: -len("/v2")]
    headers = {
        "APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"],
    }

    if args.start and args.end:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        end_date = datetime.now(ET).date()
        start_date = end_date - timedelta(days=args.days)

    calendar = get_calendar(trade_base, headers, start_date.isoformat(), end_date.isoformat())
    trading_days = {d["date"]: d for d in calendar}
    if not trading_days:
        print("No trading days found in range.")
        return

    range_start_utc = datetime.combine(start_date, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc).isoformat()
    range_end_utc = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=ET).astimezone(timezone.utc).isoformat()

    cache_dir = None if args.no_cache else (args.cache_dir if args.cache_dir.exists() else None)
    source = "cache" if cache_dir else "API"
    print(f"Fetching {symbol} bars from {start_date} to {end_date} (entry timeframe: {args.entry_timeframe}, source: {source})...")
    bars_15m = get_all_bars(headers, symbol, "15Min", range_start_utc, range_end_utc, cache_dir=cache_dir)
    bars_1m = get_all_bars(headers, symbol, "1Min", range_start_utc, range_end_utc, cache_dir=cache_dir)
    if args.entry_timeframe == "1Min":
        bars_entry = bars_1m
    else:
        bars_entry = get_all_bars(headers, symbol, args.entry_timeframe, range_start_utc, range_end_utc, cache_dir=cache_dir)
    print(f"Got {len(bars_15m)} 15-min bars, {len(bars_entry)} entry-timeframe bars, {len(bars_1m)} 1-min bars.")
    print("Building bar indexes...", end=" ", flush=True)
    idx_15m = build_index(bars_15m)
    idx_entry = build_index(bars_entry)
    idx_1m = build_index(bars_1m)
    print("done.\n")

    results = []
    for date_str in sorted(trading_days):
        res = simulate_day(symbol, date_str, trading_days[date_str], idx_15m, idx_entry, idx_1m, entry_field=args.entry_field, exit_mode=args.exit_mode, stop_pct=args.stop_pct, reward_pct=args.reward_pct, entry_timeframe=args.entry_timeframe)
        results.append(res)

    print(f"{'Date':<12}{'Side':<7}{'Entry':<10}{'Exit':<10}{'Reason':<8}{'PnL':>10}{'PnL%':>9}")
    total_pnl = 0.0
    total_pnl_pct = 0.0
    wins = losses = no_entries = skipped = 0
    for r in results:
        if r.get("skipped"):
            print(f"{r['date']:<12}{'(skipped: ' + r['skipped'] + ')'}")
            skipped += 1
            continue
        if not r["entered"]:
            print(f"{r['date']:<12}{'-':<7}{'-':<10}{'-':<10}{'no_entry':<8}{0:>10.2f}{0:>9.2f}")
            no_entries += 1
            continue
        pnl = r["pnl"]
        pnl_pct = r["pnl_pct"]
        total_pnl += pnl
        total_pnl_pct += pnl_pct
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        print(
            f"{r['date']:<12}{r['side']:<7}{r['entry_price']:<10.2f}{r['exit_price']:<10.2f}"
            f"{r['exit_reason']:<8}{pnl:>10.2f}{pnl_pct:>8.2f}%"
        )

    traded = wins + losses
    print("\n=== Summary ===")
    print(f"Symbol: {symbol}")
    print(f"Trading days: {len(results)} (skipped: {skipped}, no entry: {no_entries}, traded: {traded})")
    if traded:
        print(f"Wins: {wins}  Losses: {losses}  Win rate: {wins / traded * 100:.1f}%")
        print(f"Average PnL per trade: ${total_pnl / traded:.2f}  ({total_pnl_pct / traded:.3f}% avg return/trade)")
    print(f"Total PnL (qty={TRADE_QTY} share/day): ${total_pnl:.2f}")
    print(f"Cumulative return (sum of per-trade %, normalized for position value): {total_pnl_pct:.2f}%")


if __name__ == "__main__":
    main()
