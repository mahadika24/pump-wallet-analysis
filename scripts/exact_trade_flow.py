#!/usr/bin/env python3

import csv
import json
from pathlib import Path
from collections import Counter


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "transactions.jsonl"
OUTPUT = ROOT / "output"

WALLET = "GRCkjXgEEWQnqveX4oWTNSZroMo4wMwx7EWZFqzZubeB"

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

BUY_EXACT_QUOTE_IN = bytes([
    198, 46, 21, 82, 180, 217, 232, 112
])

SELL = bytes([
    51, 230, 133, 164, 1, 127, 131, 173
])

WSOL = "So11111111111111111111111111111111111111112"

ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


def b58decode(value):
    n = 0

    for char in value:
        n = n * 58 + ALPHABET.index(char)

    raw = (
        n.to_bytes((n.bit_length() + 7) // 8, "big")
        if n
        else b""
    )

    padding = 0

    for char in value:
        if char == "1":
            padding += 1
        else:
            break

    return b"\x00" * padding + raw


def pubkey_value(value):
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return value.get("pubkey")

    return None


def get_keys(result):
    tx = result.get("transaction") or {}
    message = tx.get("message") or {}
    meta = result.get("meta") or {}

    keys = []

    for item in message.get("accountKeys") or []:
        key = pubkey_value(item)

        if key:
            keys.append(key)

    loaded = meta.get("loadedAddresses") or {}

    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])

    return keys


def resolve_program(ix, keys):
    if ix.get("programId"):
        return ix["programId"]

    index = ix.get("programIdIndex")

    if isinstance(index, int) and index < len(keys):
        return keys[index]

    return None


def get_signature(wrapper, result):
    signature = wrapper.get("requestedSignature")

    if signature:
        return signature

    signatures = (
        (result.get("transaction") or {})
        .get("signatures") or []
    )

    return signatures[0] if signatures else None


def wallet_index(keys):
    try:
        return keys.index(WALLET)
    except ValueError:
        return None


def native_delta(meta, index):
    if index is None:
        return None

    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []

    if index >= len(pre) or index >= len(post):
        return None

    return post[index] - pre[index]


def wallet_token_changes(meta, keys):
    """
    Calculate raw token balance changes only for token
    accounts whose owner is our target wallet.

    Uses raw integer token units.
    """

    pre = {}
    post = {}

    for item in meta.get("preTokenBalances") or []:
        owner = item.get("owner")

        if owner != WALLET:
            continue

        index = item.get("accountIndex")
        mint = item.get("mint")
        ui = item.get("uiTokenAmount") or {}

        amount = ui.get("amount")
        decimals = ui.get("decimals")

        if (
            index is None
            or mint is None
            or amount is None
        ):
            continue

        pre[(index, mint)] = {
            "amount": int(amount),
            "decimals": decimals,
        }

    for item in meta.get("postTokenBalances") or []:
        owner = item.get("owner")

        if owner != WALLET:
            continue

        index = item.get("accountIndex")
        mint = item.get("mint")
        ui = item.get("uiTokenAmount") or {}

        amount = ui.get("amount")
        decimals = ui.get("decimals")

        if (
            index is None
            or mint is None
            or amount is None
        ):
            continue

        post[(index, mint)] = {
            "amount": int(amount),
            "decimals": decimals,
        }

    changes = []

    for key in set(pre) | set(post):
        before = pre.get(key)
        after = post.get(key)

        before_amount = (
            before["amount"] if before else 0
        )

        after_amount = (
            after["amount"] if after else 0
        )

        delta = after_amount - before_amount

        if delta == 0:
            continue

        decimals = (
            after["decimals"]
            if after
            else before["decimals"]
        )

        account_index, mint = key

        changes.append({
            "account_index": account_index,
            "account":
                keys[account_index]
                if account_index < len(keys)
                else None,
            "mint": mint,
            "decimals": decimals,
            "raw_delta": delta,
        })

    return changes


def decode_swap_instruction(raw):
    if len(raw) < 24:
        return None

    discriminator = raw[:8]

    a = int.from_bytes(
        raw[8:16],
        "little"
    )

    b = int.from_bytes(
        raw[16:24],
        "little"
    )

    trailing = raw[24:]

    if discriminator == BUY_EXACT_QUOTE_IN:
        return {
            "action": "BUY",
            "input_raw": a,
            "limit_raw": b,
            "input_name": "spendable_quote_in",
            "limit_name": "min_base_amount_out",
            "trailing_hex": trailing.hex(),
        }

    if discriminator == SELL:
        return {
            "action": "SELL",
            "input_raw": a,
            "limit_raw": b,
            "input_name": "base_amount_in",
            "limit_name": "min_quote_amount_out",
            "trailing_hex": trailing.hex(),
        }

    return None


def find_pumpswap_instruction(result, keys):
    message = (
        (result.get("transaction") or {})
        .get("message") or {}
    )

    for index, ix in enumerate(
        message.get("instructions") or []
    ):
        if resolve_program(ix, keys) != PUMPSWAP:
            continue

        data = ix.get("data")

        if not data:
            continue

        try:
            raw = b58decode(data)
        except Exception:
            continue

        decoded = decode_swap_instruction(raw)

        if decoded:
            decoded["outer_index"] = index
            decoded["instruction_hex"] = raw.hex()
            decoded["instruction_length"] = len(raw)

            return decoded

    return None


def extract_left_right(logs):
    left = None
    right = None

    for line in logs or []:
        if "Left:" in line:
            left = line.split(
                "Left:", 1
            )[1].strip()

        if "Right:" in line:
            right = line.split(
                "Right:", 1
            )[1].strip()

    return left, right


def pick_trade_token(changes, action):
    """
    Pick wallet-owned non-WSOL token movement.

    BUY  -> positive token delta
    SELL -> negative token delta
    """

    candidates = [
        item for item in changes
        if item["mint"] != WSOL
    ]

    if action == "BUY":
        candidates = [
            item for item in candidates
            if item["raw_delta"] > 0
        ]
    else:
        candidates = [
            item for item in candidates
            if item["raw_delta"] < 0
        ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: abs(x["raw_delta"]),
        reverse=True
    )

    return candidates[0]


def ui_amount(raw, decimals):
    if raw is None or decimals is None:
        return None

    return raw / (10 ** decimals)


def main():
    OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    rows = []

    action_counts = Counter()
    success_counts = Counter()
    instruction_lengths = Counter()
    trailing_counts = Counter()

    with INPUT.open(
        encoding="utf-8"
    ) as file:

        for line in file:
            if not line.strip():
                continue

            wrapper = json.loads(line)
            result = wrapper.get("result")

            if not result:
                continue

            meta = result.get("meta") or {}
            keys = get_keys(result)

            swap = find_pumpswap_instruction(
                result,
                keys
            )

            if not swap:
                continue

            action = swap["action"]
            success = meta.get("err") is None

            action_counts[action] += 1

            success_counts[
                f"{action}_{'success' if success else 'failed'}"
            ] += 1

            instruction_lengths[
                f"{action}:{swap['instruction_length']}"
            ] += 1

            trailing_counts[
                f"{action}:{swap['trailing_hex']}"
            ] += 1

            w_index = wallet_index(keys)

            sol_delta_raw = native_delta(
                meta,
                w_index
            )

            fee_raw = meta.get("fee")

            token_changes = wallet_token_changes(
                meta,
                keys
            )

            trade_token = pick_trade_token(
                token_changes,
                action
            )

            token_mint = None
            token_decimals = None
            token_delta_raw = None
            token_delta_ui = None
            token_account = None

            if trade_token:
                token_mint = trade_token["mint"]
                token_decimals = trade_token["decimals"]
                token_delta_raw = trade_token["raw_delta"]
                token_delta_ui = ui_amount(
                    token_delta_raw,
                    token_decimals
                )
                token_account = trade_token["account"]

            left, right = extract_left_right(
                meta.get("logMessages") or []
            )

            rows.append({
                "signature":
                    get_signature(
                        wrapper,
                        result
                    ),

                "slot":
                    result.get("slot"),

                "block_time":
                    result.get("blockTime"),

                "action":
                    action,

                "success":
                    success,

                "pumpswap_outer_index":
                    swap["outer_index"],

                "instruction_length":
                    swap["instruction_length"],

                "input_name":
                    swap["input_name"],

                "input_raw":
                    swap["input_raw"],

                "limit_name":
                    swap["limit_name"],

                "limit_raw":
                    swap["limit_raw"],

                "trailing_hex":
                    swap["trailing_hex"],

                "wallet_sol_delta_lamports":
                    sol_delta_raw,

                "wallet_sol_delta":
                    (
                        sol_delta_raw / 1_000_000_000
                        if sol_delta_raw is not None
                        else None
                    ),

                "network_fee_lamports":
                    fee_raw,

                "network_fee_sol":
                    (
                        fee_raw / 1_000_000_000
                        if fee_raw is not None
                        else None
                    ),

                "trade_token_mint":
                    token_mint,

                "trade_token_account":
                    token_account,

                "trade_token_decimals":
                    token_decimals,

                "trade_token_delta_raw":
                    token_delta_raw,

                "trade_token_delta_ui":
                    token_delta_ui,

                "log_left":
                    left,

                "log_right":
                    right,

                "instruction_hex":
                    swap["instruction_hex"],

                "all_wallet_token_changes":
                    json.dumps(
                        token_changes,
                        separators=(",", ":")
                    ),
            })

    if not rows:
        raise RuntimeError(
            "Tidak ada PumpSwap BUY/SELL yang berhasil didecode."
        )

    csv_path = OUTPUT / "exact_trade_flow.csv"

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=list(rows[0].keys())
        )

        writer.writeheader()
        writer.writerows(rows)

    successful_buys = [
        row for row in rows
        if row["action"] == "BUY"
        and row["success"]
    ]

    successful_sells = [
        row for row in rows
        if row["action"] == "SELL"
        and row["success"]
    ]

    failed_buys = [
        row for row in rows
        if row["action"] == "BUY"
        and not row["success"]
    ]

    sell_exact_quantity_matches = 0
    sell_quantity_mismatches = 0

    for row in successful_sells:
        token_delta = row[
            "trade_token_delta_raw"
        ]

        base_in = row["input_raw"]

        if token_delta is None:
            continue

        if abs(token_delta) == base_in:
            sell_exact_quantity_matches += 1
        else:
            sell_quantity_mismatches += 1

    buy_budget_matches = 0
    buy_budget_plus_fee = Counter()

    for row in successful_buys:
        sol_delta = row[
            "wallet_sol_delta_lamports"
        ]

        budget = row["input_raw"]

        if sol_delta is None:
            continue

        spent = -sol_delta

        if spent == budget:
            buy_budget_matches += 1

        difference = spent - budget

        buy_budget_plus_fee[
            str(difference)
        ] += 1

    sell_limit_headrooms = []

    for row in successful_sells:
        sol_delta = row[
            "wallet_sol_delta_lamports"
        ]

        fee = row[
            "network_fee_lamports"
        ] or 0

        minimum = row["limit_raw"]

        if (
            sol_delta is None
            or minimum <= 0
        ):
            continue

        # Approximation:
        # wallet native increase + tx fee
        # reconstructs quote received before
        # the transaction fee was paid.
        reconstructed_quote = (
            sol_delta + fee
        )

        if reconstructed_quote <= 0:
            continue

        headroom = (
            reconstructed_quote
            / minimum
            - 1
        ) * 100

        sell_limit_headrooms.append(
            headroom
        )

    summary = {
        "decoded_swaps":
            len(rows),

        "actions":
            dict(action_counts),

        "success_breakdown":
            dict(success_counts),

        "instruction_lengths":
            dict(instruction_lengths),

        "trailing_values":
            dict(trailing_counts),

        "successful_buy_count":
            len(successful_buys),

        "failed_buy_count":
            len(failed_buys),

        "successful_sell_count":
            len(successful_sells),

        "sell_base_amount_matches_wallet_token_delta":
            sell_exact_quantity_matches,

        "sell_base_amount_mismatches_wallet_token_delta":
            sell_quantity_mismatches,

        "buy_wallet_spend_equals_instruction_budget":
            buy_budget_matches,

        "buy_spend_minus_instruction_budget_lamports":
            dict(
                buy_budget_plus_fee.most_common()
            ),

        "sell_limit_headroom_pct": {
            "count":
                len(sell_limit_headrooms),

            "min":
                min(sell_limit_headrooms)
                if sell_limit_headrooms
                else None,

            "max":
                max(sell_limit_headrooms)
                if sell_limit_headrooms
                else None,

            "average":
                (
                    sum(sell_limit_headrooms)
                    / len(sell_limit_headrooms)
                )
                if sell_limit_headrooms
                else None,
        },

        "notes": [
            (
                "All token amounts use raw integer "
                "token units."
            ),
            (
                "Wallet token deltas are restricted "
                "to token accounts owned by the "
                "target wallet."
            ),
            (
                "SOL balance delta is kept separate "
                "from PumpSwap instruction amounts "
                "and network fee."
            ),
            (
                "Sell quote reconstruction is still "
                "diagnostic; rent/account-close "
                "effects must be validated before "
                "using it as exact proceeds."
            ),
        ],
    }

    summary_path = (
        OUTPUT / "exact_trade_flow_summary.json"
    )

    summary_path.write_text(
        json.dumps(
            summary,
            indent=2
        ),
        encoding="utf-8"
    )

    print("=" * 60)
    print("PHASE 2G/2H — EXACT TRADE FLOW")
    print("=" * 60)

    print("Decoded swaps :", len(rows))
    print("BUY           :", action_counts["BUY"])
    print("SELL          :", action_counts["SELL"])

    print()

    print("Success breakdown:")
    for key, value in success_counts.items():
        print(f"  {key}: {value}")

    print()

    print(
        "SELL exact token quantity matches:",
        sell_exact_quantity_matches
    )

    print(
        "SELL token quantity mismatches:",
        sell_quantity_mismatches
    )

    print()

    print("BUY spend - instruction budget:")

    for value, count in (
        buy_budget_plus_fee.most_common(10)
    ):
        print(
            f"  {value} lamports: {count}"
        )

    print()

    print("Output:")
    print(" ", csv_path)
    print(" ", summary_path)


if __name__ == "__main__":
    main()
