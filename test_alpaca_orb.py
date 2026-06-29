#!/usr/bin/env python3
"""
Ad-hoc test harness for alpaca_orb.py. Monkeypatches urllib.request.urlopen
to serve canned Alpaca API responses so the full main() control flow can be
exercised without hitting the network or touching a real account.

Not a permanent part of the repo's test suite -- just used to probe for bugs.
"""
import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import alpaca_orb as orb

SCRIPT_DIR = Path(__file__).parent
STATE_DIR = SCRIPT_DIR / "orb_state"
TEST_ENV = SCRIPT_DIR / "alpaca_test.env"
TEST_ENV.write_text(
    "APCA_API_KEY_ID=test\nAPCA_API_SECRET_KEY=test\nAPCA_API_BASE_URL=https://paper-api.alpaca.markets/v2\n"
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_case(name, mocks, argv):
    """mocks: list of (METHOD, path_substring, response_or_exception) tuples, checked in order.
    Returns the list of (method, url, decoded_json_body_or_None) for every request made."""
    print(f"\n=== {name} ===")
    requests_made = []

    def fake_urlopen(req, *a, **kw):
        url = req.full_url
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        requests_made.append((method, url, body))
        for want_method, path, resp in mocks:
            if method == want_method and path in url:
                if isinstance(resp, Exception):
                    raise resp
                return FakeResponse(resp)
        raise AssertionError(f"No mock response configured for {method} {url}")

    # reset logging handlers between cases (real deployment is a fresh process each run)
    logging.getLogger().handlers.clear()

    old_argv = sys.argv
    sys.argv = ["alpaca_orb.py"] + argv
    try:
        # wait_for_clock_sync's polling behavior is tested separately/directly;
        # here just pass through to the mocked /v2/clock response immediately,
        # since these scenarios use fictional timestamps that won't match real time.
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch.object(orb, "wait_for_clock_sync", side_effect=lambda tb, h, sn, **kw: orb.get_clock(tb, h)):
            orb.main()
    except Exception as e:
        print(f"!! EXCEPTION: {type(e).__name__}: {e}")
        raise
    finally:
        sys.argv = old_argv
    return requests_made


def reset_state():
    state_file = STATE_DIR / "TEST.json"
    if state_file.exists():
        state_file.unlink()


def write_state(day_state):
    state_file = STATE_DIR / "TEST.json"
    state_file.write_text(json.dumps({"2026-06-24": day_state}))
    return state_file


# --- Case 1: market closed ---
reset_state()
run_case(
    "Market closed",
    [("GET", "/v2/clock", {"timestamp": "2026-06-24T08:00:00-04:00", "is_open": False, "next_open": "x", "next_close": "x"})],
    ["TEST", "1", "--env-file", str(TEST_ENV)],
)

# --- Case 2: still forming opening range ---
reset_state()
run_case(
    "Still forming opening range",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:35:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
    ],
    ["TEST", "1", "--env-file", str(TEST_ENV)],
)

# --- Case 3: opening range established, no breakout yet ---
reset_state()
run_case(
    "Opening range set, no breakout",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:50:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "timeframe=15Min", {"bars": [{"h": 105.0, "l": 95.0, "c": 100.0, "t": "2026-06-24T13:30:00Z"}]}),
        ("GET", "timeframe=5Min", {"bars": [{"h": 102.0, "l": 98.0, "c": 100.0, "t": "2026-06-24T13:45:00Z"}]}),
    ],
    ["TEST", "1", "--env-file", str(TEST_ENV)],
)

# --- Case 4: long breakout entry ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": False, "side": None,
    "entry_price": None, "entry_time": None, "stop_price": None, "target_price": None,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Long breakout entry",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:55:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/stocks/TEST/bars", {"bars": [{"h": 110.0, "l": 108.0, "c": 109.0, "t": "2026-06-24T13:50:00Z"}]}),
        ("POST", "/v2/orders", {"id": "order-123", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after entry:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 5: holding position, stop hit ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "long",
    "entry_price": 109.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 95.0, "target_price": 130.0, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Holding long, stop hit",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T10:30:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", {"current_price": "90.0", "qty": "3"}),
        ("POST", "/v2/orders", {"id": "exit-order", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after stop:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 6: position no longer held (closed externally / never filled) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "long",
    "entry_price": 109.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 95.0, "target_price": 130.0, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Position missing (404) on lookup",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T10:30:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", orb.urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO(b'{"message":"position does not exist"}'))),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after 404:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 7: degenerate opening range (flat, high == low) ---
reset_state()
run_case(
    "Degenerate flat opening range",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:50:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "timeframe=15Min", {"bars": [{"h": 100.0, "l": 100.0, "c": 100.0, "t": "2026-06-24T13:30:00Z"}]}),
        ("GET", "timeframe=5Min", {"bars": [{"h": 100.0, "l": 100.0, "c": 100.0, "t": "2026-06-24T13:45:00Z"}]}),
    ],
    ["TEST", "1", "--env-file", str(TEST_ENV)],
)
state_file = STATE_DIR / "TEST.json"
print("Saved state after flat range:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 8: target hit ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "long",
    "entry_price": 109.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 95.0, "target_price": 130.0, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Holding long, target hit",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T11:00:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", {"current_price": "131.0", "qty": "3"}),
        ("POST", "/v2/orders", {"id": "exit-order", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after target:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 9: time exit (past cutoff, still holding, price between stop/target) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "long",
    "entry_price": 109.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 95.0, "target_price": 130.0, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Holding long, time exit at 3:50pm",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T15:50:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", {"current_price": "112.0", "qty": "3"}),
        ("POST", "/v2/orders", {"id": "exit-order", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after time exit:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 10: order rejected by broker (e.g. non-shortable / insufficient buying power) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": False, "side": None,
    "entry_price": None, "entry_time": None, "stop_price": None, "target_price": None,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Short breakout, order rejected",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:55:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/stocks/TEST/bars", {"bars": [{"h": 92.0, "l": 90.0, "c": 91.0, "t": "2026-06-24T13:50:00Z"}]}),
        ("POST", "/v2/orders", orb.urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO(b'{"message":"asset not shortable"}'))),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
print("Saved state after rejected order:", json.loads(state_file.read_text())["2026-06-24"])

# --- Case 11: short breakout entry (price closes below range low) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": False, "side": None,
    "entry_price": None, "entry_time": None, "stop_price": None, "target_price": None,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Short breakout entry",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:55:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/stocks/TEST/bars", {"bars": [{"h": 92.0, "l": 90.0, "c": 91.0, "t": "2026-06-24T13:50:00Z"}]}),
        ("POST", "/v2/orders", {"id": "short-order-1", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
short_entry_state = json.loads(state_file.read_text())["2026-06-24"]
print("Saved state after short entry:", short_entry_state)
# stop should be the range high (105.0), target should be below entry (1.5R to the downside)
assert short_entry_state["side"] == "short"
assert short_entry_state["stop_price"] == 105.0
assert short_entry_state["entry_price"] == 91.0
expected_target = 91.0 - 1.5 * (105.0 - 91.0)
assert abs(short_entry_state["target_price"] - expected_target) < 1e-9, short_entry_state["target_price"]
assert short_entry_state["qty"] == 3

# --- Case 12: holding short, stop hit (price rises back above range high) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "short",
    "entry_price": 91.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 105.0, "target_price": expected_target, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
requests = run_case(
    "Holding short, stop hit",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T10:30:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", {"current_price": "106.0", "qty": "-3"}),
        ("POST", "/v2/orders", {"id": "cover-order-1", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
short_stop_state = json.loads(state_file.read_text())["2026-06-24"]
print("Saved state after short stop:", short_stop_state)
assert short_stop_state["exit_reason"] == "stop"
assert short_stop_state["exited"] is True
cover_order = next(b for m, u, b in requests if m == "POST" and "/v2/orders" in u)
print("Cover order body:", cover_order)
assert cover_order["side"] == "buy", "short exit should buy-to-cover, not sell"
assert cover_order["qty"] == "3", "exit qty should match the entered qty, not the full account position"

# --- Case 13: holding short, target hit (price falls to/below target) ---
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": True, "side": "short",
    "entry_price": 91.0, "entry_time": "2026-06-24T09:55:00-04:00",
    "stop_price": 105.0, "target_price": expected_target, "qty": 3,
    "exited": False, "exit_reason": None, "exit_time": None,
})
run_case(
    "Holding short, target hit",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T11:00:00-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        ("GET", "/v2/positions/TEST", {"current_price": str(expected_target - 0.5), "qty": "-3"}),
        ("POST", "/v2/orders", {"id": "cover-order-2", "status": "accepted"}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
short_target_state = json.loads(state_file.read_text())["2026-06-24"]
print("Saved state after short target:", short_target_state)
assert short_target_state["exit_reason"] == "target"
assert short_target_state["exited"] is True

# --- Case 14: mid-candle poll must not use the still-forming bar's live close ---
# At 09:52 (mid-way through the 09:50-09:55 candle), the only fully-closed candle
# since the range ended (09:45) is the 09:45-09:50 one. The script should query
# with end=09:45 (the start of that already-closed candle), not reach into the
# still-forming 09:50-09:55 candle.
state_file = write_state({
    "date": "2026-06-24", "range_high": 105.0, "range_low": 95.0, "range_set": True,
    "range_set_time": "2026-06-24T09:50:00-04:00", "entered": False, "side": None,
    "entry_price": None, "entry_time": None, "stop_price": None, "target_price": None,
    "exited": False, "exit_reason": None, "exit_time": None,
})
requests = run_case(
    "Mid-candle poll clamps to last closed boundary",
    [
        ("GET", "/v2/clock", {"timestamp": "2026-06-24T09:52:30-04:00", "is_open": True}),
        ("GET", "/v2/calendar", [{"date": "2026-06-24", "open": "09:30", "close": "16:00"}]),
        # Only the fully-closed 09:45-09:50 candle should ever be requested/used.
        ("GET", "/v2/stocks/TEST/bars", {"bars": [{"h": 100.0, "l": 98.0, "c": 99.0, "t": "2026-06-24T13:45:00Z"}]}),
    ],
    ["TEST", "3", "--env-file", str(TEST_ENV)],
)
mid_candle_state = json.loads(state_file.read_text())["2026-06-24"]
print("Saved state after mid-candle poll:", mid_candle_state)
assert mid_candle_state["entered"] is False, "should not enter based on a still-forming candle"
bars_request = next(u for m, u, b in requests if m == "GET" and "/v2/stocks/TEST/bars" in u)
assert "end=2026-06-24T13%3A45%3A00" in bars_request, f"expected end clamped to 09:45 ET (start of last closed candle), got: {bars_request}"
print("Bars request URL (end correctly clamped to last closed boundary):", bars_request)

print("\nAll cases ran without unhandled exceptions (see output above for behavior).")
