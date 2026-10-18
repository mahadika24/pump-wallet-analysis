#!/usr/bin/env python3

import json
import csv
import base64
import statistics
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone

WALLET = "GRCkjXgEEWQnqveX4oWTNSZroMo4wMwx7EWZFqzZubeB"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
GUARD = "8rdfF3LCRFPVhASWD8cFPpbmVvqSRXq5ZLqX7rgK2Zkd"

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "transactions.jsonl"
OUTPUT = ROOT / "output"

OUTPUT.mkdir(exist_ok=True)

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ALPHABET_MAP = {c: i for i, c in enumerate(ALPHABET)}


def b58decode(s):
    n = 0

    for char in s:
        n = n * 58 + ALPHABET_MAP[char]

    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""

    pad = 0
    for char in s:
        if char == "1":
            pad += 1
        else:
            break

    return b"\x00" * pad + raw


def account_key(x):
    if isinstance(x, str):
        return x

    if isinstance(x, dict):
        return x.get("pubkey")

    return None


def load_transactions():
    txs = []

    with INPUT.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()

            if not line:
                continue

            try:
                obj = json.loads(line)
            except Exception as exc:
                print(f"WARNING line {lineno}: invalid JSON: {exc}")
                continue

            result = obj.get("result")

            if result:
                txs.append(result)

    return txs


def get_keys(tx):
    msg = (
        tx.get("transaction", {})
          .get("message", {})
    )

    return [
        account_key(x)
        for x in msg.get("accountKeys", [])
    ]


def resolve_program_id(ix, keys):
    if "programId" in ix:
        pid = ix["programId"]

        if isinstance(pid, dict):
            return pid.get("pubkey")

        return pid

    idx = ix.get("programIdIndex")

    if isinstance(idx, int) and 0 <= idx < len(keys):
        return keys[idx]

    return None


def all_instructions(tx):
    keys = get_keys(tx)

    outer = (
        tx.get("transaction", {})
          .get("message", {})
          .get("instructions", [])
    )

    for ix in outer:
        yield ix, resolve_program_id(ix, keys), "outer"

    inner_groups = (
        tx.get("meta", {})
          .get("innerInstructions", [])
        or []
    )

    for group in inner_groups:
        for ix in group.get("instructions", []):
            yield ix, resolve_program_id(ix, keys), "inner"


def logs(tx):
    return tx.get("meta", {}).get("logMessages", []) or []


def detect_action(tx):
    text = "\n".join(logs(tx)).lower()

    # Order matters: BuyExactQuoteIn also contains "buy"
    if "instruction: buyexactquotein" in text:
        return "BUY"

    if "instruction: buy_exact_quote_in" in text:
        return "BUY"

    if "instruction: sell" in text:
        return "SELL"

    if "instruction: buy" in text:
        return "BUY"

    return "OTHER"


def tx_error(tx):
    err = tx.get("meta", {}).get("err")

    if err is None:
        return ""

    return json.dumps(err, separators=(",", ":"))


def wallet_sol_delta(tx):
    keys = get_keys(tx)

    try:
        idx = keys.index(WALLET)
    except ValueError:
        return None

    pre = tx.get("meta", {}).get("preBalances", [])
    post = tx.get("meta", {}).get("postBalances", [])

    if idx >= len(pre) or idx >= len(post):
        return None

    return (post[idx] - pre[idx]) / 1_000_000_000


def token_balances_for_wallet(tx):
    meta = tx.get("meta", {})

    pre = meta.get("preTokenBalances", []) or []
    post = meta.get("postTokenBalances", []) or []

    before = defaultdict(float)
    after = defaultdict(float)

    for item in pre:
        if item.get("owner") != WALLET:
            continue

        mint = item.get("mint")

        try:
            amount = float(
                item.get("uiTokenAmount", {}).get("uiAmountString", "0")
            )
        except Exception:
            amount = 0.0

        before[mint] += amount

    for item in post:
        if item.get("owner") != WALLET:
            continue

        mint = item.get("mint")

        try:
            amount = float(
                item.get("uiTokenAmount", {}).get("uiAmountString", "0")
            )
        except Exception:
            amount = 0.0

        after[mint] += amount

    changes = {}

    for mint in set(before) | set(after):
        delta = after[mint] - before[mint]

        if abs(delta) > 0:
            changes[mint] = delta

    return changes


def primary_token_change(tx):
    changes = token_balances_for_wallet(tx)

    if not changes:
        return "", None

    # Ignore wrapped SOL when possible.
    WSOL = "So11111111111111111111111111111111111111112"

    candidates = {
        mint: delta
        for mint, delta in changes.items()
        if mint != WSOL
    }

    if not candidates:
        candidates = changes

    mint, delta = max(
        candidates.items(),
        key=lambda x: abs(x[1])
    )

    return mint, delta


def guard_info(tx):
    keys = get_keys(tx)

    for ix, pid, where in all_instructions(tx):
        if pid != GUARD:
            continue

        data = ix.get("data")

        if not data:
            return True, "", None, None

        try:
            raw = b58decode(data)

            if len(raw) == 8:
                deadline = int.from_bytes(raw, "little")
                slot = tx.get("slot")

                distance = (
                    deadline - slot
                    if isinstance(slot, int)
                    else None
                )

                return True, data, deadline, distance

        except Exception:
            pass

        return True, data, None, None

    return False, "", None, None


def program_ids(tx):
    return sorted({
        pid
        for _, pid, _ in all_instructions(tx)
        if pid
    })


def timestamp_iso(block_time):
    if not block_time:
        return ""

    return datetime.fromtimestamp(
        block_time,
        tz=timezone.utc
    ).isoformat()


def main():
    print("Loading:", INPUT)

    txs = load_transactions()

    print("Transactions loaded:", len(txs))

    rows = []
    programs = Counter()
    actions = Counter()
    guard_distances = Counter()

    for tx in txs:
        action = detect_action(tx)
        actions[action] += 1

        for pid in program_ids(tx):
            programs[pid] += 1

        mint, token_delta = primary_token_change(tx)

        has_guard, guard_data, deadline, distance = guard_info(tx)

        if distance is not None:
            guard_distances[distance] += 1

        signatures = (
            tx.get("transaction", {})
              .get("signatures", [])
        )

        signature = signatures[0] if signatures else ""

        meta = tx.get("meta", {})

        fee_sol = (
            meta.get("fee", 0) / 1_000_000_000
            if isinstance(meta.get("fee", 0), int)
            else None
        )

        rows.append({
            "signature": signature,
            "slot": tx.get("slot"),
            "block_time": tx.get("blockTime"),
            "utc_time": timestamp_iso(tx.get("blockTime")),
            "action": action,
            "success": meta.get("err") is None,
            "error": tx_error(tx),
            "token_mint": mint,
            "token_delta": token_delta,
            "wallet_native_sol_delta": wallet_sol_delta(tx),
            "network_fee_sol": fee_sol,
            "has_8rdf_guard": has_guard,
            "guard_data_base58": guard_data,
            "guard_deadline_slot": deadline,
            "guard_distance_slots": distance,
            "has_pumpswap": PUMPSWAP in program_ids(tx),
        })

    rows.sort(
        key=lambda r: (
            r["block_time"] or 0,
            r["slot"] or 0
        )
    )

    trades_file = OUTPUT / "transactions.csv"

    with trades_file.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys())
        )

        writer.writeheader()
        writer.writerows(rows)

    programs_file = OUTPUT / "programs.csv"

    with programs_file.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "program_id",
            "transaction_count"
        ])

        for pid, count in programs.most_common():
            writer.writerow([pid, count])

    summary = {
        "wallet": WALLET,
        "transactions": len(rows),
        "actions": dict(actions),
        "unique_token_mints": len({
            r["token_mint"]
            for r in rows
            if r["token_mint"]
        }),
        "pumpswap_transactions": sum(
            1 for r in rows if r["has_pumpswap"]
        ),
        "8rdf_guard_transactions": sum(
            1 for r in rows if r["has_8rdf_guard"]
        ),
        "guard_distance_distribution": {
            str(k): v
            for k, v in sorted(guard_distances.items())
        },
        "top_programs": programs.most_common(20),
    }

    summary_file = OUTPUT / "summary.json"

    summary_file.write_text(
        json.dumps(
            summary,
            indent=2
        ),
        encoding="utf-8"
    )

    print()
    print("========== SUMMARY ==========")
    print(json.dumps(summary, indent=2))
    print("=============================")
    print()
    print("Created:")
    print(" ", trades_file)
    print(" ", programs_file)
    print(" ", summary_file)


if __name__ == "__main__":
    main()
