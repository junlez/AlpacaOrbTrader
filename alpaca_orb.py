#!/usr/bin/env python3
"""
Opening Range Breakout (ORB) strategy runner for a single symbol.

Intended to be invoked once per minute (e.g. via cron) while the market is
open, as: python3 alpaca_orb.py TICKER

State for each trading day is persisted in orb_state/<TICKER>.json so that
repeated invocations pick up where the last one left off. All log output is
also written, timestamped, to orb_state/<TICKER>.log.
"""
import sys
import json
import time
import logging
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCRIPT_DIR = Path(__file__).parent
STATE_DIR = SCRIPT_DIR / "orb_state"
DATA_BASE = "https://data.alpaca.markets"

OPENING_RANGE_MINUTES = 15
EXIT_BUFFER_MINUTES = 10
TARGET_R = 1.5
DEFAULT_TRADE_QTY = 1
CLOCK_SYNC_POLL_INTERVAL_SECONDS = 0.1
CLOCK_SYNC_MAX_WAIT_SECONDS = 10.0


def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def api_request(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {err_body}") from e


def get_clock(trade_base, headers):
    return api_request("GET", f"{trade_base}/v2/clock", headers)


def get_calendar_day(trade_base, headers, date_str):
    url = f"{trade_base}/v2/calendar?start={date_str}&end={date_str}"
    days = api_request("GET", url, headers)
    return days[0] if days else None


def get_bars(headers, symbol, timeframe, start, end, limit, sort="asc"):
    params = {
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "limit": str(limit),
        "feed": "iex",  # change to "sip" if you have a paid market data subscription
        "adjustment": "raw",
        "sort": sort,
    }
    url = f"{DATA_BASE}/v2/stocks/{symbol}/bars?" + urllib.parse.urlencode(params)
    resp = api_request("GET", url, headers)
    return resp.get("bars") or []


def bar_start_time(bar):
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))


def get_position(trade_base, headers, symbol):
    try:
        return api_request("GET", f"{trade_base}/v2/positions/{symbol}", headers)
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise


def submit_order(trade_base, headers, **kwargs):
    return api_request("POST", f"{trade_base}/v2/orders", headers, body=kwargs)


def state_path(symbol):
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{symbol}.json"


def load_all_state(symbol):
    p = state_path(symbol)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def load_state(symbol, date_str):
    all_state = load_all_state(symbol)
    if date_str in all_state:
        return all_state[date_str]
    return {
        "date": date_str,
        "range_high": None,
        "range_low": None,
        "range_set": False,
        "range_set_time": None,
        "range_bar_timestamp": None,
        "entered": False,
        "side": None,
        "qty": None,
        "entry_price": None,
        "entry_time": None,
        "entry_bar_timestamp": None,
        "stop_price": None,
        "target_price": None,
        "exited": False,
        "exit_reason": None,
        "exit_time": None,
    }


def save_state(symbol, date_str, state):
    all_state = load_all_state(symbol)
    all_state[date_str] = state
    state_path(symbol).write_text(json.dumps(all_state, indent=2))


def setup_logging(symbol):
    STATE_DIR.mkdir(exist_ok=True)
    log_path = STATE_DIR / f"{symbol}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )


def wait_for_clock_sync(trade_base, headers, system_now, max_wait_seconds=CLOCK_SYNC_MAX_WAIT_SECONDS,
                         poll_interval=CLOCK_SYNC_POLL_INTERVAL_SECONDS):
    """Poll Alpaca's clock until its hour:minute (UTC) matches the given system_now.
    Returns the matching raw clock dict once aligned. Raises RuntimeError if they
    haven't aligned within max_wait_seconds."""
    system_hm = (system_now.hour, system_now.minute)
    waited = 0.0
    while True:
        clock = get_clock(trade_base, headers)
        alpaca_now = datetime.fromisoformat(clock["timestamp"]).astimezone(timezone.utc)
        if (alpaca_now.hour, alpaca_now.minute) == system_hm:
            return clock
        if waited >= max_wait_seconds:
            raise RuntimeError(
                f"Alpaca clock ({alpaca_now.strftime('%H:%M')} UTC) did not match "
                f"system clock ({system_now.strftime('%H:%M')} UTC) within {max_wait_seconds}s"
            )
        time.sleep(poll_interval)
        waited += poll_interval


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("qty", type=int, nargs="?", default=DEFAULT_TRADE_QTY,
                         help=f"shares to trade per day (default: {DEFAULT_TRADE_QTY})")
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR / "alpaca_PAPER.env",
                         help="path to the Alpaca credentials env file (default: alpaca_PAPER.env next to this script)")
    parser.add_argument("--entry-field", choices=["vw", "c"], default="vw",
                         help="bar field used for breakout signal: 'vw' (VWAP, default) or 'c' (close)")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    trade_qty = args.qty
    entry_field = args.entry_field
    setup_logging(symbol)

    env = load_env(args.env_file)
    trade_base = env["APCA_API_BASE_URL"].rstrip("/")
    if trade_base.endswith("/v2"):
        trade_base = trade_base[: -len("/v2")]
    headers = {
        "APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"],
        "Content-Type": "application/json",
    }

    system_now = datetime.now(timezone.utc)
    try:
        clock = wait_for_clock_sync(trade_base, headers, system_now)
    except RuntimeError as e:
        logging.info(f"[{symbol}] [{system_now.isoformat()}] Clock sync check failed, aborting this run: {e}")
        return
    now = datetime.fromisoformat(clock["timestamp"])

    if not clock["is_open"]:
        logging.info(f"[{symbol}] [{now.isoformat()}] Market closed. Nothing to do.")
        return

    date_str = now.astimezone(ET).strftime("%Y-%m-%d")
    state = load_state(symbol, date_str)

    if state["exited"]:
        logging.info(f"[{symbol}] [{now.isoformat()}] Already done for {date_str} (exit_reason={state['exit_reason']}).")
        return

    calendar_day = get_calendar_day(trade_base, headers, date_str)
    if calendar_day is None:
        logging.info(f"[{symbol}] [{now.isoformat()}] No trading calendar entry for {date_str}; skipping.")
        return

    open_dt = datetime.strptime(f"{date_str} {calendar_day['open']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    close_dt = datetime.strptime(f"{date_str} {calendar_day['close']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    range_end_dt = open_dt + timedelta(minutes=OPENING_RANGE_MINUTES)
    exit_cutoff_dt = close_dt - timedelta(minutes=EXIT_BUFFER_MINUTES)

    # Step 1: establish the opening range from the first 15-minute candle.
    if not state["range_set"]:
        if now < range_end_dt:
            logging.info(f"[{symbol}] [{now.isoformat()}] Still forming opening range (until {range_end_dt.time()} ET).")
            return
        bars = get_bars(
            headers, symbol, "15Min",
            open_dt.astimezone(timezone.utc).isoformat(),
            range_end_dt.astimezone(timezone.utc).isoformat(),
            limit=1,
        )
        if not bars:
            logging.info(f"[{symbol}] [{now.isoformat()}] Opening range bar not available yet; will retry next run.")
            return
        bar = bars[0]
        expected_bar_start = open_dt.astimezone(timezone.utc)
        if bar_start_time(bar) != expected_bar_start:
            logging.info(
                f"[{symbol}] [{now.isoformat()}] Opening range bar timestamp mismatch "
                f"(got {bar_start_time(bar).isoformat()}, expected {expected_bar_start.isoformat()}); will retry next run."
            )
            return
        state["range_high"] = bar["h"]
        state["range_low"] = bar["l"]
        state["range_set"] = True
        state["range_set_time"] = now.isoformat()
        state["range_bar_timestamp"] = bar["t"]
        save_state(symbol, date_str, state)
        logging.info(
            f"[{symbol}] [{now.isoformat()}] Opening range set: high={state['range_high']} low={state['range_low']} "
            f"(bar timestamp={bar['t']})"
        )
        # No 5-minute candle can have closed yet at this exact instant (the
        # earliest one closes 5 minutes from now), so checking for a breakout
        # this same run would always be a guaranteed no-op. Wait for next run.
        return

    # Step 2: if already in a position, manage stop / target / time exit.
    if state["entered"] and not state["exited"]:
        position = get_position(trade_base, headers, symbol)
        if position is None:
            state["exited"] = True
            state["exit_reason"] = state.get("exit_reason") or "closed_externally"
            state["exit_time"] = now.isoformat()
            save_state(symbol, date_str, state)
            logging.info(f"[{symbol}] [{now.isoformat()}] Position already closed (no longer held).")
            return

        current_price = float(position["current_price"])
        side = state["side"]
        hit_stop = (side == "long" and current_price <= state["stop_price"]) or (
            side == "short" and current_price >= state["stop_price"]
        )
        hit_target = (side == "long" and current_price >= state["target_price"]) or (
            side == "short" and current_price <= state["target_price"]
        )
        time_exit = now >= exit_cutoff_dt

        if hit_stop or hit_target or time_exit:
            reason = "stop" if hit_stop else "target" if hit_target else "time"
            exit_side = "sell" if side == "long" else "buy"
            exit_qty = state.get("qty") or trade_qty
            submit_order(
                trade_base, headers,
                symbol=symbol, side=exit_side, type="market",
                time_in_force="day", qty=str(exit_qty),
            )
            state["exited"] = True
            state["exit_reason"] = reason
            state["exit_time"] = now.isoformat()
            save_state(symbol, date_str, state)
            logging.info(f"[{symbol}] [{now.isoformat()}] Closed {side} position near {current_price} due to {reason}.")
        else:
            logging.info(
                f"[{symbol}] [{now.isoformat()}] Holding {side}; price={current_price}, "
                f"stop={state['stop_price']}, target={state['target_price']}."
            )
        return

    # Step 3: not yet entered. Stop looking once we're past the exit-by-close window.
    if now >= exit_cutoff_dt:
        logging.info(f"[{symbol}] [{now.isoformat()}] Past exit cutoff with no breakout entry; standing down for today.")
        state["exited"] = True
        state["exit_reason"] = "no_entry"
        state["exit_time"] = now.isoformat()
        save_state(symbol, date_str, state)
        return

    # Only evaluate fully-closed 5-minute candles (matches how the backtest sees
    # historical bars, which are always already-finalized). Clamp the query window
    # to the most recent 5-minute boundary so an in-progress candle's live-updating
    # close never gets treated as a breakout signal.
    last_closed_boundary = now.replace(second=0, microsecond=0)
    last_closed_boundary -= timedelta(minutes=last_closed_boundary.minute % 5)
    last_closed_boundary -= timedelta(minutes=5)
    if last_closed_boundary < range_end_dt:
        logging.info(f"[{symbol}] [{now.isoformat()}] No completed 5-minute candle yet since the opening range ended.")
        return

    # Alpaca's bars "end" filter is inclusive on a bar's start time (t <= end), so
    # passing the boundary itself would also match the bar that just started forming
    # at that exact instant. Step back 1 second to exclude it and only match bars
    # that are genuinely already closed.
    bars = get_bars(
        headers, symbol, "5Min",
        range_end_dt.astimezone(timezone.utc).isoformat(),
        last_closed_boundary.astimezone(timezone.utc).isoformat(),
        limit=1,
        sort="desc",
    )
    if not bars:
        logging.info(f"[{symbol}] [{now.isoformat()}] No 5-minute candle data yet.")
        return

    latest = bars[0]
    expected_bar_start = last_closed_boundary.astimezone(timezone.utc)
    if bar_start_time(latest) != expected_bar_start:
        logging.info(
            f"[{symbol}] [{now.isoformat()}] Latest 5-minute candle timestamp mismatch "
            f"(got {bar_start_time(latest).isoformat()}, expected {expected_bar_start.isoformat()}); "
            f"feed may be missing data for this interval, skipping this run."
        )
        return

    signal_price = latest[entry_field]
    range_high = state["range_high"]
    range_low = state["range_low"]

    if signal_price > range_high:
        side = "long"
    elif signal_price < range_low:
        side = "short"
    else:
        logging.info(
            f"[{symbol}] [{now.isoformat()}] No breakout yet ({entry_field}={signal_price}, range=[{range_low}, {range_high}], "
            f"bar timestamp={latest['t']})."
        )
        return

    try:
        if side == "long":
            stop_price = range_low
            risk = signal_price - stop_price
            target_price = signal_price + TARGET_R * risk
            order = submit_order(
                trade_base, headers,
                symbol=symbol, side="buy", type="market",
                time_in_force="day", qty=str(trade_qty),
            )
        else:
            stop_price = range_high
            risk = stop_price - signal_price
            target_price = signal_price - TARGET_R * risk
            order = submit_order(
                trade_base, headers,
                symbol=symbol, side="sell", type="market",
                time_in_force="day", qty=str(trade_qty),
            )
    except RuntimeError as e:
        logging.info(f"[{symbol}] [{now.isoformat()}] Order submission failed, will retry next run: {e}")
        return

    state["entered"] = True
    state["side"] = side
    state["qty"] = trade_qty
    state["entry_price"] = signal_price
    state["entry_time"] = now.isoformat()
    state["entry_bar_timestamp"] = latest["t"]
    state["stop_price"] = stop_price
    state["target_price"] = target_price
    state["order_id"] = order.get("id")
    save_state(symbol, date_str, state)
    logging.info(
        f"[{symbol}] [{now.isoformat()}] Entered {side} @ ~{signal_price} ({entry_field}). stop={stop_price} target={target_price} "
        f"(risk={risk:.4f}, R={TARGET_R}). order_id={order.get('id')} (bar timestamp={latest['t']})"
    )


if __name__ == "__main__":
    main()
