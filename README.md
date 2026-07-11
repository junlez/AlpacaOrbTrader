# AlpacaOrbTrader

A minute-by-minute Opening Range Breakout (ORB) trading bot for [Alpaca](https://alpaca.markets/), plus a backtester and a simple account viewer. Pure Python standard library — no pip dependencies.

> **Risk disclaimer:** this script places real orders against your brokerage account when pointed at a live env file. It is provided as-is, with no warranty, and is not financial advice. Use at your own risk, and test thoroughly on a paper account before considering live trading.

## The strategy

For a given symbol and trading day:
1. **Opening range**: the high/low of the first 15 minutes of regular trading (9:30–9:45am ET) defines the range.
2. **Entry**: once a subsequent 5-minute candle's *VWAP* (`vw`) crosses above the range high (long) or below the range low (short), it enters a position. The close price (`c`) is also supported via `--entry-field c`.
3. **Stop-loss**: the opposite side of the opening range (range low for longs, range high for shorts).
4. **Target**: 1.5x the initial risk (1.5R).
5. **Exit detection**: each minute, the previous fully-closed 1-minute bar's high/low is checked against the stop and target (`--exit-mode prev-hl`, default). Checking the current live price from the position is also supported via `--exit-mode close`.
6. **Time exit**: if neither stop nor target is hit, the position is closed 10 minutes before market close (3:50pm ET).

Each invocation of `alpaca_orb.py` does one step of this process and persists its progress to `orb_state/<TICKER>.json`, so it's designed to be invoked repeatedly (e.g., once a minute) rather than run once as a long-lived process.

## Requirements

- Python 3.9+ (uses `zoneinfo`)
- No third-party packages required
- An [Alpaca](https://alpaca.markets/) account (paper and/or live)

## Setup

1. Create `alpaca_PAPER.env` and/or `alpaca_LIVE.env` in this directory using the templates below, filled in with your real API keys from Alpaca. These files are gitignored and should never be committed.
2. Set up a scheduler (e.g., cron, or `launchd` on macOS) to invoke `alpaca_orb.py` once per minute, Monday–Friday, covering at least **9:30am–4:00pm ET** (regular market hours). The script checks Alpaca's market clock itself and safely no-ops outside trading hours, so a slightly wider scheduling window (e.g., 9am to 5pm ET) is harmless.

`alpaca_PAPER.env` template:
```
APCA_API_KEY_ID=your_actual_api_key_here
APCA_API_SECRET_KEY=your_actual_secret_key_here
APCA_API_BASE_URL=https://paper-api.alpaca.markets/v2
```

`alpaca_LIVE.env` template:
```
APCA_API_KEY_ID=your_actual_api_key_here
APCA_API_SECRET_KEY=your_actual_secret_key_here
APCA_API_BASE_URL=https://api.alpaca.markets
```

## Usage

```
python alpaca_orb.py SYMBOL [QTY] [--env-file ENV_FILE] [--entry-field vw|c] [--exit-mode prev-hl|close]
```

- `SYMBOL` — ticker to trade (required).
- `QTY` — shares to trade per day (optional, default: 1).
- `--env-file` — path to credentials file (optional, default: `alpaca_PAPER.env`).

Example commands:
```
python alpaca_orb.py TSLA 8 --env-file alpaca_PAPER.env
python alpaca_orb.py TSLA 8 --env-file alpaca_LIVE.env
```

### Output

Each run appends timestamped log lines to both stdout and `orb_state/<SYMBOL>.log`. Day-by-day state (range, entry/exit prices, timestamps, reasons) is persisted to `orb_state/<SYMBOL>.json`.

### Market data feed

By default, bar data is fetched using Alpaca's free `iex` feed. If you have a paid market data subscription, edit the `feed` parameter in `get_bars()` in `alpaca_orb.py` to `sip` for fuller market coverage.

## Other scripts

- **`alpaca_orb_backtest.py`** — backtests the same ORB logic against historical bars for a given symbol and date range.
  ```
  python alpaca_orb_backtest.py SYMBOL --start YYYY-MM-DD --end YYYY-MM-DD [--entry-timeframe 5Min|1Min] [--entry-field vw|c] [--exit-mode prev-hl|close]
  ```
- **`alpaca_view.py`** — prints your account summary and open positions using `alpaca_PAPER.env`.
  ```
  python alpaca_view.py
  ```
- **`test_alpaca_orb.py`** — a mocked test harness exercising `alpaca_orb.py`'s control flow (range formation, entries, stop/target/time exits, edge cases) without hitting the network. Run with:
  ```
  python test_alpaca_orb.py
  ```

## Acknowledgements

This project was designed and built in collaboration with [Claude](https://claude.ai) (Anthropic), which assisted with strategy implementation, backtesting, bug fixing, and code review throughout development.
