#!/usr/bin/env python3

import base64
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_KEY = os.environ.get("SOLSCAN_API_KEY")

BASE_URL = "https://pro-api.solscan.io/playground/account/transactions/enhanced"

POOL = "Ffh2wCSHeerCDDScPXbpgaib9k87UbjqGqmyFyqxQC7w"

BUY_SLOT = 458084847

START_SLOT = BUY_SLOT - 100
END_SLOT = BUY_SLOT

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

BUY_DISC = bytes.fromhex("c62e1552b4d9e870")
SELL_DISC = bytes.fromhex("33e685a4017f83ad")

LIMIT = 40
MAX_PAGES = 5


if not API_KEY:
    print("ERROR: SOLSCAN_API_KEY belum tersedia.")
    print("Jalankan: source ~/.bashrc")
    sys.exit(1)


def decode_instruction_data(data):

    if not isinstance(data, str):
        return None

    try:
        raw = base64.b64decode(data)
    except Exception:
        return None

    if raw.startswith(BUY_DISC):

        if len(raw) >= 25:
            quote_in = int.from_bytes(
                raw[8:16],
                "little",
            )

            min_base_out = int.from_bytes(
                raw[16:24],
                "little",
            )

            return {
                "type": "BUY",
                "length": len(raw),
                "quote_in": quote_in,
                "min_base_out": min_base_out,
                "trailing_byte": raw[24],
            }

        return {
            "type": "BUY",
            "length": len(raw),
        }

    if raw.startswith(SELL_DISC):

        if len(raw) >= 24:
            base_in = int.from_bytes(
                raw[8:16],
                "little",
            )

            min_quote_out = int.from_bytes(
                raw[16:24],
                "little",
            )

            return {
                "type": "SELL",
                "length": len(raw),
                "base_in": base_in,
                "min_quote_out": min_quote_out,
            }

        return {
            "type": "SELL",
            "length": len(raw),
        }

    return None


def request_page(cursor=None):

    params = [
        ("address", POOL),
        ("from_slot", str(START_SLOT)),
        ("to_slot", str(END_SLOT)),
        ("limit", str(LIMIT)),
        ("encoding", "jsonParsed"),
        ("program[]", PUMPSWAP),
    ]

    if cursor:
        params.append(("cursor", cursor))

    url = f"{BASE_URL}?{urlencode(params)}"

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

        return json.loads(raw)

    except HTTPError as e:

        body = e.read().decode(
            "utf-8",
            errors="replace",
        )

        print("HTTP ERROR:", e.code)
        print(body[:3000])

        sys.exit(1)

    except URLError as e:

        print("NETWORK ERROR:", e)
        sys.exit(1)


def extract_transactions(data):

    payload = data.get("data", {})

    if isinstance(payload, dict):
        return (
            payload.get("transactions", []),
            payload.get("cursor"),
        )

    return payload, None


def analyze_transaction(item):

    slot = item.get("slot")
    block_time = item.get("blockTime")

    tx = item.get("transaction", {})

    signatures = tx.get("signatures", [])

    signature = signatures[0] if signatures else ""

    message = tx.get("message", {})

    account_keys = message.get("accountKeys", [])

    signers = []

    for account in account_keys:

        if isinstance(account, dict):

            if account.get("signer"):
                signers.append(
                    account.get("pubkey")
                )

        elif isinstance(account, str):

            signers.append(account)

    instructions = message.get("instructions", [])

    swaps = []

    for index, ix in enumerate(instructions):

        if not isinstance(ix, dict):
            continue

        if ix.get("programId") != PUMPSWAP:
            continue

        decoded = decode_instruction_data(
            ix.get("data")
        )

        if decoded:

            decoded["instruction_index"] = index
            swaps.append(decoded)

    return {
        "slot": slot,
        "blockTime": block_time,
        "signature": signature,
        "signer": signers[0] if signers else None,
        "pump_swap_instruction_count": len(swaps),
        "swaps": swaps,
    }


def main():

    print("Phase 4D.3 — Solscan Playground Pagination + Decoder Pilot")
    print()
    print("Pool:", POOL)
    print("BUY slot:", BUY_SLOT)
    print("Slot window:", START_SLOT, "->", END_SLOT)
    print("Page limit:", LIMIT)
    print("Maximum pages:", MAX_PAGES)
    print()

    all_transactions = []
    seen_signatures = set()

    cursor = None

    for page_number in range(1, MAX_PAGES + 1):

        print(
            f"[PAGE {page_number}/{MAX_PAGES}] "
            f"requesting..."
        )

        data = request_page(cursor)

        if not data.get("success"):

            print("API returned success=false")
            print(json.dumps(data, indent=2))
            sys.exit(1)

        transactions, next_cursor = extract_transactions(data)

        print(
            "  received:",
            len(transactions),
        )

        added = 0

        for item in transactions:

            analyzed = analyze_transaction(item)

            signature = analyzed["signature"]

            if signature and signature not in seen_signatures:

                seen_signatures.add(signature)

                all_transactions.append(
                    analyzed
                )

                added += 1

        print(
            "  unique added:",
            added,
        )

        print(
            "  total unique:",
            len(all_transactions),
        )

        if not next_cursor:

            print("  no next cursor")
            break

        if next_cursor == cursor:

            print("  cursor did not change")
            break

        cursor = next_cursor

        print(
            "  next cursor:",
            cursor,
        )

        print()

    print()
    print("=== SUMMARY ===")
    print("Pages requested:", page_number)
    print("Unique transactions:", len(all_transactions))

    swap_transactions = []

    buy_count = 0
    sell_count = 0

    for tx in all_transactions:

        if tx["swaps"]:

            swap_transactions.append(tx)

            for swap in tx["swaps"]:

                if swap["type"] == "BUY":
                    buy_count += 1

                elif swap["type"] == "SELL":
                    sell_count += 1

    print(
        "Transactions with PumpSwap swap:",
        len(swap_transactions),
    )

    print("Decoded BUY:", buy_count)
    print("Decoded SELL:", sell_count)

    print()

    print("=== PUMPSWAP ACTIVITY ===")

    for tx in swap_transactions:

        print()
        print("slot:", tx["slot"])
        print("blockTime:", tx["blockTime"])
        print("signature:", tx["signature"])
        print("signer:", tx["signer"])

        for swap in tx["swaps"]:

            print(
                "  type:",
                swap["type"],
            )

            print(
                "  instruction_index:",
                swap["instruction_index"],
            )

            print(
                "  length:",
                swap["length"],
            )

            if swap["type"] == "BUY":

                print(
                    "  quote_in:",
                    swap.get("quote_in"),
                )

                print(
                    "  min_base_out:",
                    swap.get("min_base_out"),
                )

                print(
                    "  trailing_byte:",
                    swap.get("trailing_byte"),
                )

            elif swap["type"] == "SELL":

                print(
                    "  base_in:",
                    swap.get("base_in"),
                )

                print(
                    "  min_quote_out:",
                    swap.get("min_quote_out"),
                )

    output = {
        "phase": "4D.3",
        "pool": POOL,
        "buy_slot": BUY_SLOT,
        "start_slot": START_SLOT,
        "end_slot": END_SLOT,
        "pages_requested": page_number,
        "unique_transactions": len(all_transactions),
        "pumpswap_transactions": len(swap_transactions),
        "decoded_buy": buy_count,
        "decoded_sell": sell_count,
        "transactions": all_transactions,
    }

    with open(
        "output/solscan_pool_pagination_pilot.json",
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            output,
            f,
            indent=2,
        )

    print()
    print("Written:")
    print(
        "output/solscan_pool_pagination_pilot.json"
    )


if __name__ == "__main__":
    main()
