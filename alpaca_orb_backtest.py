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

OPENING_RANGE_MINUTES = 15
EXIT_BUFFER_MINUTES = 10
TARGET_R = 1.5
TRADE_QTY = 1


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


def get_all_bars(headers, symbol, timeframe, start, end):
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


def bars_between(bars, start_dt, end_dt):
    out = []
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        if start_dt <= t < end_dt:
            out.append((t, b))
    return out


def simulate_day(symbol, day, calendar_day, bars_15m, bars_entry, bars_1m):
    date_str = day
    open_dt = datetime.strptime(f"{date_str} {calendar_day['open']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    close_dt = datetime.strptime(f"{date_str} {calendar_day['close']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    range_end_dt = open_dt + timedelta(minutes=OPENING_RANGE_MINUTES)
    exit_cutoff_dt = close_dt - timedelta(minutes=EXIT_BUFFER_MINUTES)

    range_bar = bars_between(bars_15m, open_dt.astimezone(timezone.utc), range_end_dt.astimezone(timezone.utc))
    if not range_bar:
        return {"date": date_str, "skipped": "no_opening_range_data"}
    range_high = range_bar[0][1]["h"]
    range_low = range_bar[0][1]["l"]

    entry_candles = bars_between(bars_entry, range_end_dt.astimezone(timezone.utc), exit_cutoff_dt.astimezone(timezone.utc))

    entry = None
    for t, b in entry_candles:
        close_price = b["c"]
        if close_price > range_high:
            entry = {"time": t, "price": close_price, "side": "long"}
            break
        elif close_price < range_low:
            entry = {"time": t, "price": close_price, "side": "short"}
            break

    if entry is None:
        return {
            "date": date_str, "range_high": range_high, "range_low": range_low,
            "entered": False, "exit_reason": "no_entry",
        }

    side = entry["side"]
    entry_price = entry["price"]
    if side == "long":
        stop_price = range_low
        risk = entry_price - stop_price
        target_price = entry_price + TARGET_R * risk
    else:
        stop_price = range_high
        risk = stop_price - entry_price
        target_price = entry_price - TARGET_R * risk

    one_min = bars_between(bars_1m, entry["time"], close_dt.astimezone(timezone.utc))

    exit_price = None
    exit_reason = None
    exit_time = None
    for t, b in one_min:
        hi, lo = b["h"], b["l"]
        hit_stop = (side == "long" and lo <= stop_price) or (side == "short" and hi >= stop_price)
        hit_target = (side == "long" and hi >= target_price) or (side == "short" and lo <= target_price)
        if hit_stop:
            exit_price, exit_reason, exit_time = stop_price, "stop", t
            break
        if hit_target:
            exit_price, exit_reason, exit_time = target_price, "target", t
            break
        if t >= exit_cutoff_dt.astimezone(timezone.utc):
            exit_price, exit_reason, exit_time = b["c"], "time", t
            break

    if exit_price is None:
        last_t, last_b = one_min[-1] if one_min else (entry["time"], {"c": entry_price})
        exit_price, exit_reason, exit_time = last_b["c"], "time", last_t

    pnl = (exit_price - entry_price) * TRADE_QTY if side == "long" else (entry_price - exit_price) * TRADE_QTY
    pnl_pct = pnl / (entry_price * TRADE_QTY) * 100

    return {
        "date": date_str, "range_high": range_high, "range_low": range_low,
        "entered": True, "side": side, "entry_time": entry["time"].astimezone(ET).isoformat(),
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
    parser.add_argument("--entry-timeframe", choices=["5Min", "1Min"], default="5Min",
                         help="candle resolution used for the entry/breakout signal")
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

    print(f"Fetching {symbol} bars from {start_date} to {end_date} (entry timeframe: {args.entry_timeframe})...")
    bars_15m = get_all_bars(headers, symbol, "15Min", range_start_utc, range_end_utc)
    bars_1m = get_all_bars(headers, symbol, "1Min", range_start_utc, range_end_utc)
    if args.entry_timeframe == "1Min":
        bars_entry = bars_1m
    else:
        bars_entry = get_all_bars(headers, symbol, "5Min", range_start_utc, range_end_utc)
    print(f"Got {len(bars_15m)} 15-min bars, {len(bars_entry)} entry-timeframe bars, {len(bars_1m)} 1-min bars.\n")

    results = []
    for date_str in sorted(trading_days):
        res = simulate_day(symbol, date_str, trading_days[date_str], bars_15m, bars_entry, bars_1m)
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
