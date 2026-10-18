import os, csv, json, time, urllib.request, urllib.parse

API = "https://pro-api.solscan.io/playground/account/transactions/enhanced"
KEY = os.getenv("SOLSCAN_API_KEY")
PUMP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

BUY = bytes.fromhex("c62e1552b4d9e870")
SELL = bytes.fromhex("33e685a4017f83ad")

MAX_ROUNDTRIPS = 10
MAX_PAGES = 5


def b58(s):
    chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for c in s:
        n = n * 58 + chars.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


roundtrips = load("output/roundtrips.csv")
pool_rows = load("output/pumpswap_pools.csv")
pools = {r["signature"]: r["pool"] for r in pool_rows}

os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)

out = "data/market_context_4d4.jsonl"
summary = []

if not KEY:
    raise SystemExit("SOLSCAN_API_KEY tidak ditemukan. Jalankan: source ~/.bashrc")


for num, r in enumerate(roundtrips[:MAX_ROUNDTRIPS], 1):

    buy_sig = r["buy_signature"]
    sell_sig = r["sell_signature"]
    buy_slot = int(r["buy_slot"])
    sell_slot = int(r["sell_slot"])
    pool = pools.get(buy_sig)

    if not pool:
        print(f"[{num}/{MAX_ROUNDTRIPS}] POOL TIDAK DITEMUKAN")
        continue

    start = buy_slot - 85
    end = sell_slot
    cursor = None
    pages = 0
    swaps = 0

    print(f"[{num}/{MAX_ROUNDTRIPS}] {start} -> {end}")

    while pages < MAX_PAGES:

        params = {
            "address": pool,
            "program[]": PUMP,
            "from_slot": start,
            "to_slot": end,
            "limit": 40
        }

        if cursor:
            params["cursor"] = cursor

        url = API + "?" + urllib.parse.urlencode(params, doseq=True)

        success = False

        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "accept": "application/json",
                        "token": KEY
                    }
                )

                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.loads(response.read())

                if data.get("success"):
                    success = True
                    break

            except Exception as e:
                if attempt == 2:
                    print("  request gagal:", str(e)[:80])
                time.sleep(2)

        if not success:
            break

        txs = data.get("data", {}).get("transactions", [])

        if not txs:
            break

        with open(out, "a") as f:

            for tx in txs:

                slot = int(tx.get("slot", 0))
                transaction = tx.get("transaction", {})
                message = transaction.get("message", {})
                signature_list = transaction.get("signatures", [])

                if not signature_list:
                    continue

                signature = signature_list[0]

                signer = ""
                for account in message.get("accountKeys", []):
                    if account.get("signer"):
                        signer = account.get("pubkey", "")
                        break

                for ix in message.get("instructions", []):

                    if ix.get("programId") != PUMP:
                        continue

                    raw = b58(ix.get("data", ""))

                    if raw[:8] == BUY:
                        side = "BUY"
                    elif raw[:8] == SELL:
                        side = "SELL"
                    else:
                        continue

                    if signature in (buy_sig, sell_sig):
                        continue

                    record = {
                        "roundtrip": num,
                        "bot_buy": buy_sig,
                        "bot_sell": sell_sig,
                        "pool": pool,
                        "slot": slot,
                        "signature": signature,
                        "signer": signer,
                        "side": side,
                        "distance_from_buy": slot - buy_slot
                    }

                    if side == "BUY" and len(raw) >= 24:
                        record["quote_in"] = int.from_bytes(
                            raw[8:16], "little"
                        )
                        record["min_base_out"] = int.from_bytes(
                            raw[16:24], "little"
                        )

                    elif side == "SELL" and len(raw) >= 24:
                        record["base_in"] = int.from_bytes(
                            raw[8:16], "little"
                        )
                        record["min_quote_out"] = int.from_bytes(
                            raw[16:24], "little"
                        )

                    f.write(json.dumps(record) + "\n")
                    swaps += 1

        pages += 1
        cursor = data.get("data", {}).get("next")

        if not cursor:
            break

        time.sleep(0.6)

    print(f"  pages={pages} market_swaps={swaps}")

    summary.append({
        "roundtrip": num,
        "buy_signature": buy_sig,
        "sell_signature": sell_sig,
        "pool": pool,
        "pages": pages,
        "market_swaps": swaps
    })


with open("output/market_context_4d4_summary.json", "w") as f:
    json.dump({
        "phase": "4D.4",
        "max_roundtrips": MAX_ROUNDTRIPS,
        "results": summary
    }, f, indent=2)

print()
print("DONE")
print("Data: data/market_context_4d4.jsonl")
print("Summary: output/market_context_4d4_summary.json")
