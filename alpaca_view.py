import os
import json
import urllib.request
from pathlib import Path

def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def main():
    env = load_env(Path(__file__).parent / "alpaca_PAPER.env")
    base_url = env["APCA_API_BASE_URL"].rstrip("/")
    if base_url.endswith("/v2"):
        base_url = base_url[: -len("/v2")]
    headers = {
        "APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"],
    }

    account = get(f"{base_url}/v2/account", headers)
    print("=== Account ===")
    for k in ["status", "currency", "cash", "portfolio_value", "equity", "buying_power", "pattern_day_trader"]:
        print(f"{k}: {account.get(k)}")

    positions = get(f"{base_url}/v2/positions", headers)
    print(f"\n=== Positions ({len(positions)}) ===")
    for p in positions:
        print(f"{p['symbol']:>6}  qty={p['qty']:>10}  avg_entry={p['avg_entry_price']:>10}  "
              f"current={p['current_price']:>10}  unrealized_pl={p['unrealized_pl']:>10}")

if __name__ == "__main__":
    main()
