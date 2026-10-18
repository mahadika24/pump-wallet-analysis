#!/usr/bin/env python3

import csv
import json
from pathlib import Path
from collections import Counter


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "transactions.jsonl"
OUTPUT = ROOT / "output"

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

# PumpSwap buy_exact_quote_in
BUY_EXACT_QUOTE_IN = bytes([
    198, 46, 21, 82, 180, 217, 232, 112
])

ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


def b58decode(value):
    n = 0

    for char in value:
        n = n * 58 + ALPHABET.index(char)

    if n:
        raw = n.to_bytes(
            (n.bit_length() + 7) // 8,
            "big"
        )
    else:
        raw = b""

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


def get_all_keys(result):
    tx = result.get("transaction") or {}
    message = tx.get("message") or {}
    meta = result.get("meta") or {}

    keys = []

    for value in message.get("accountKeys", []):
        key = pubkey_value(value)

        if key:
            keys.append(key)

    loaded = meta.get("loadedAddresses") or {}

    keys.extend(
        loaded.get("writable") or []
    )

    keys.extend(
        loaded.get("readonly") or []
    )

    return keys


def resolve_program(ix, keys):
    program_id = ix.get("programId")

    if program_id:
        return program_id

    index = ix.get("programIdIndex")

    if (
        isinstance(index, int)
        and 0 <= index < len(keys)
    ):
        return keys[index]

    return None


def classify(logs):
    text = "\n".join(logs or []).lower()

    if "instruction: buyexactquotein" in text:
        return "BUY"

    if "instruction: buy_exact_quote_in" in text:
        return "BUY"

    if "instruction: sell" in text:
        return "SELL"

    if "instruction: buy" in text:
        return "BUY"

    return "OTHER"


def get_signature(wrapper, result):
    signature = wrapper.get("requestedSignature")

    if signature:
        return signature

    tx = result.get("transaction") or {}

    signatures = tx.get("signatures") or []

    if signatures:
        return signatures[0]

    return None


def get_error(meta):
    err = meta.get("err")

    if err is None:
        return None, None

    if not isinstance(err, dict):
        return None, err

    instruction_error = err.get("InstructionError")

    if (
        isinstance(instruction_error, list)
        and len(instruction_error) >= 2
    ):
        return (
            instruction_error[0],
            instruction_error[1]
        )

    return None, err


def token_deltas(meta):
    """
    Return raw integer token balance changes by:

        (accountIndex, mint)

    Uses raw integer token amounts rather than float.
    """

    pre = {}
    post = {}

    for item in meta.get("preTokenBalances") or []:
        account_index = item.get("accountIndex")
        mint = item.get("mint")

        ui = item.get("uiTokenAmount") or {}

        amount = ui.get("amount")
        decimals = ui.get("decimals")

        if (
            account_index is None
            or not mint
            or amount is None
        ):
            continue

        pre[(account_index, mint)] = {
            "amount": int(amount),
            "decimals": decimals,
        }

    for item in meta.get("postTokenBalances") or []:
        account_index = item.get("accountIndex")
        mint = item.get("mint")

        ui = item.get("uiTokenAmount") or {}

        amount = ui.get("amount")
        decimals = ui.get("decimals")

        if (
            account_index is None
            or not mint
            or amount is None
        ):
            continue

        post[(account_index, mint)] = {
            "amount": int(amount),
            "decimals": decimals,
        }

    changes = []

    all_keys = set(pre) | set(post)

    for key in all_keys:
        before = pre.get(key)
        after = post.get(key)

        decimals = None

        if after:
            decimals = after["decimals"]
        elif before:
            decimals = before["decimals"]

        before_amount = (
            before["amount"]
            if before else 0
        )

        after_amount = (
            after["amount"]
            if after else 0
        )

        delta = after_amount - before_amount

        if delta == 0:
            continue

        changes.append({
            "account_index": key[0],
            "mint": key[1],
            "raw_delta": delta,
            "decimals": decimals,
        })

    return changes


def find_positive_token_change(meta):
    """
    For successful BUYs, choose the largest positive
    raw token balance change as an initial estimate of
    received base token amount.

    This is diagnostic only. Exact account ownership
    validation can be added in the next stage.
    """

    changes = token_deltas(meta)

    positive = [
        item
        for item in changes
        if item["raw_delta"] > 0
    ]

    if not positive:
        return None

    positive.sort(
        key=lambda item: item["raw_delta"],
        reverse=True
    )

    return positive[0]


def parse_buy_exact_quote_in(raw):
    """
    Layout:

    bytes 0..7   discriminator
    bytes 8..15  spendable_quote_in u64 LE
    bytes 16..23 min_base_amount_out u64 LE

    Additional bytes, if present, are retained
    separately because track_volume serialization
    may depend on the deployed instruction version.
    """

    if len(raw) < 24:
        return None

    if raw[:8] != BUY_EXACT_QUOTE_IN:
        return None

    spendable_quote_in = int.from_bytes(
        raw[8:16],
        "little"
    )

    min_base_amount_out = int.from_bytes(
        raw[16:24],
        "little"
    )

    trailing = raw[24:]

    return {
        "spendable_quote_in":
            spendable_quote_in,

        "min_base_amount_out":
            min_base_amount_out,

        "trailing_hex":
            trailing.hex(),

        "instruction_length":
            len(raw),
    }


def find_log_numbers(logs):
    """
    Preserve numeric assertion-style log lines.

    We do NOT assume Left/Right semantics yet.
    """

    left = None
    right = None

    for line in logs or []:
        stripped = line.strip()

        if "Left:" in stripped:
            try:
                left = stripped.split(
                    "Left:", 1
                )[1].strip()
            except Exception:
                pass

        if "Right:" in stripped:
            try:
                right = stripped.split(
                    "Right:", 1
                )[1].strip()
            except Exception:
                pass

    return left, right


def main():
    OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    rows = []

    discriminators = Counter()
    instruction_lengths = Counter()
    spend_amounts = Counter()
    trailing_values = Counter()

    total_buy_attempts = 0
    decoded_buys = 0
    successful_decoded = 0
    failed_decoded = 0

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
            logs = meta.get("logMessages") or []

            action = classify(logs)

            if action != "BUY":
                continue

            total_buy_attempts += 1

            success = meta.get("err") is None

            signature = get_signature(
                wrapper,
                result
            )

            slot = result.get("slot")
            block_time = result.get("blockTime")

            keys = get_all_keys(result)

            tx = result.get("transaction") or {}
            message = tx.get("message") or {}

            instructions = (
                message.get("instructions") or []
            )

            error_index, error_detail = (
                get_error(meta)
            )

            found = False

            for outer_index, ix in enumerate(
                instructions
            ):
                program = resolve_program(
                    ix,
                    keys
                )

                if program != PUMPSWAP:
                    continue

                data = ix.get("data")

                if not data:
                    continue

                try:
                    raw = b58decode(data)
                except Exception:
                    continue

                discriminator = raw[:8].hex()

                discriminators[
                    discriminator
                ] += 1

                instruction_lengths[
                    len(raw)
                ] += 1

                parsed = parse_buy_exact_quote_in(
                    raw
                )

                if not parsed:
                    continue

                found = True
                decoded_buys += 1

                if success:
                    successful_decoded += 1
                else:
                    failed_decoded += 1

                spend = parsed[
                    "spendable_quote_in"
                ]

                minimum = parsed[
                    "min_base_amount_out"
                ]

                spend_amounts[
                    str(spend)
                ] += 1

                trailing_values[
                    parsed["trailing_hex"]
                ] += 1

                token_change = None

                if success:
                    token_change = (
                        find_positive_token_change(
                            meta
                        )
                    )

                actual_raw_out = None
                token_mint = None
                token_decimals = None

                if token_change:
                    actual_raw_out = (
                        token_change["raw_delta"]
                    )

                    token_mint = (
                        token_change["mint"]
                    )

                    token_decimals = (
                        token_change["decimals"]
                    )

                headroom_raw = None
                headroom_pct = None

                if (
                    actual_raw_out is not None
                    and minimum > 0
                ):
                    headroom_raw = (
                        actual_raw_out - minimum
                    )

                    headroom_pct = (
                        headroom_raw
                        / minimum
                        * 100
                    )

                left_log, right_log = (
                    find_log_numbers(logs)
                )

                rows.append({
                    "signature":
                        signature,

                    "slot":
                        slot,

                    "block_time":
                        block_time,

                    "success":
                        success,

                    "error_instruction_index":
                        error_index,

                    "error_detail":
                        json.dumps(
                            error_detail,
                            separators=(",", ":")
                        )
                        if error_detail is not None
                        else "",

                    "pumpswap_outer_index":
                        outer_index,

                    "instruction_length":
                        parsed[
                            "instruction_length"
                        ],

                    "discriminator_hex":
                        discriminator,

                    "spendable_quote_in_raw":
                        spend,

                    "spendable_quote_in_sol":
                        spend / 1_000_000_000,

                    "min_base_amount_out_raw":
                        minimum,

                    "trailing_hex":
                        parsed[
                            "trailing_hex"
                        ],

                    "token_mint_estimate":
                        token_mint,

                    "token_decimals":
                        token_decimals,

                    "actual_token_out_raw_estimate":
                        actual_raw_out,

                    "success_headroom_raw":
                        headroom_raw,

                    "success_headroom_pct":
                        headroom_pct,

                    "log_left":
                        left_log,

                    "log_right":
                        right_log,

                    "instruction_hex":
                        raw.hex(),
                })

                break

            if not found:
                # BUY classified from logs but no matching
                # buy_exact_quote_in outer instruction.
                pass

    csv_path = (
        OUTPUT / "pumpswap_buys.csv"
    )

    if rows:
        with csv_path.open(
            "w",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=list(
                    rows[0].keys()
                )
            )

            writer.writeheader()
            writer.writerows(rows)

    successful_rows = [
        row
        for row in rows
        if row["success"]
    ]

    failed_rows = [
        row
        for row in rows
        if not row["success"]
    ]

    success_headrooms = [
        row["success_headroom_pct"]
        for row in successful_rows
        if row["success_headroom_pct"]
        is not None
    ]

    summary = {
        "total_buy_attempts":
            total_buy_attempts,

        "decoded_buy_exact_quote_in":
            decoded_buys,

        "successful_decoded":
            successful_decoded,

        "failed_decoded":
            failed_decoded,

        "decode_coverage_pct":
            (
                decoded_buys
                / total_buy_attempts
                * 100
            )
            if total_buy_attempts
            else None,

        "discriminator_counts":
            dict(discriminators),

        "instruction_length_counts":
            dict(instruction_lengths),

        "spendable_quote_in_counts":
            dict(spend_amounts),

        "trailing_hex_counts":
            dict(trailing_values),

        "success_headroom_pct": {
            "count":
                len(success_headrooms),

            "min":
                min(success_headrooms)
                if success_headrooms
                else None,

            "max":
                max(success_headrooms)
                if success_headrooms
                else None,

            "average":
                (
                    sum(success_headrooms)
                    / len(success_headrooms)
                )
                if success_headrooms
                else None,
        },

        "failed_with_left_log":
            sum(
                1
                for row in failed_rows
                if row["log_left"]
                is not None
            ),

        "failed_with_right_log":
            sum(
                1
                for row in failed_rows
                if row["log_right"]
                is not None
            ),

        "notes": [
            (
                "spendable_quote_in and "
                "min_base_amount_out are decoded "
                "directly from PumpSwap instruction."
            ),
            (
                "actual_token_out_raw_estimate is "
                "currently derived from successful "
                "post/pre token balance changes."
            ),
            (
                "Left/Right log values are preserved "
                "without assigning semantics yet."
            ),
        ],
    }

    summary_path = (
        OUTPUT
        / "pumpswap_buy_summary.json"
    )

    summary_path.write_text(
        json.dumps(
            summary,
            indent=2
        ),
        encoding="utf-8"
    )

    print("=" * 60)
    print(
        "PHASE 2E — PUMPSWAP BUY DECODER"
    )
    print("=" * 60)

    print(
        "BUY attempts       :",
        total_buy_attempts
    )

    print(
        "Decoded exact quote:",
        decoded_buys
    )

    print(
        "Successful decoded :",
        successful_decoded
    )

    print(
        "Failed decoded     :",
        failed_decoded
    )

    print()

    print(
        "Spend amounts:"
    )

    for amount, count in (
        spend_amounts.most_common()
    ):
        sol = int(amount) / 1_000_000_000

        print(
            f"  {amount} lamports "
            f"({sol:.9f} SOL): "
            f"{count}"
        )

    print()

    print(
        "Instruction lengths:"
    )

    for length, count in (
        instruction_lengths.most_common()
    ):
        print(
            f"  {length}: {count}"
        )

    print()

    print(
        "Failed with Left log :",
        summary[
            "failed_with_left_log"
        ]
    )

    print(
        "Failed with Right log:",
        summary[
            "failed_with_right_log"
        ]
    )

    print()

    print("Output:")
    print(" ", csv_path)
    print(" ", summary_path)


if __name__ == "__main__":
    main()
