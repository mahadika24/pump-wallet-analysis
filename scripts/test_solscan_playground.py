#!/usr/bin/env python3

import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_KEY = os.environ.get("SOLSCAN_API_KEY")

BASE_URL = "https://pro-api.solscan.io/playground/account/transactions/enhanced"

WALLET = "GRCkjXgEEWQnqveX4oWTNSZroMo4wMwx7EWZFqzZubeB"

BUY_SIGNATURE = (
    "5crYhhyjzC3hmCpQ5z82k9ZrmwywmUZQXtf5dFPKfQM7CEef"
    "hFd3W8qpi3Sk9hjWnZzXbvZqAcWo871TCL7eFgA2"
)

BUY_SLOT = 458084847

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BUY_DISC = "c62e1552b4d9e870"


if not API_KEY:
    print("ERROR: SOLSCAN_API_KEY belum tersedia.")
    print("Jalankan: source ~/.bashrc")
    sys.exit(1)


def main():
    params = [
        ("address", WALLET),

        # Window kecil di sekitar BUY.
        ("from_slot", str(BUY_SLOT - 100)),
        ("to_slot", str(BUY_SLOT + 20)),

        ("limit", "40"),
        ("encoding", "jsonParsed"),

        # Filter PumpSwap.
        ("program[]", PUMPSWAP),

        # Filter khusus PumpSwap BUY.
        ("instruction[]", f"{PUMPSWAP}{BUY_DISC}"),
    ]

    url = f"{BASE_URL}?{urlencode(params)}"

    print("Phase 4D.1 — Solscan Playground Pilot")
    print()
    print("Endpoint:")
    print(BASE_URL)
    print()
    print("BUY signature:")
    print(BUY_SIGNATURE)
    print("BUY slot:", BUY_SLOT)
    print("Slot window:", BUY_SLOT - 100, "->", BUY_SLOT + 20)
    print()

    req = Request(
        url,
        headers={
            "accept": "application/json",
            "token": API_KEY,
            "user-agent": "pump-wallet-audit/1.0",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")

        data = json.loads(raw)

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")

        print("HTTP ERROR:", e.code)
        print(body[:2000])
        sys.exit(1)

    except URLError as e:
        print("NETWORK ERROR:", e)
        sys.exit(1)

    print("SUCCESS:", data.get("success"))

    if not data.get("success"):
        print(json.dumps(data, indent=2))
        sys.exit(1)

    payload = data.get("data", {})

    if isinstance(payload, dict):
        transactions = payload.get("transactions", [])
        cursor = payload.get("cursor")
    else:
        transactions = payload
        cursor = None

    print("TRANSACTIONS:", len(transactions))
    print("CURSOR:", cursor)
    print()

    for i, item in enumerate(transactions):
        slot = item.get("slot")

        tx = item.get("transaction", {})
        signatures = tx.get("signatures", [])

        signature = signatures[0] if signatures else ""

        print(f"[{i+1}] slot={slot}")
        print("    signature:", signature)
        print()

    with open(
        "output/solscan_playground_pilot.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(data, f, indent=2)

    print("Written: output/solscan_playground_pilot.json")


if __name__ == "__main__":
    main()
