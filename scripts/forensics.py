#!/usr/bin/env python3

import json
import csv
import base64
from pathlib import Path
from collections import Counter


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "transactions.jsonl"
OUT = ROOT / "output"

GUARD = "8rdfF3LCRFPVhASWD8cFPpbmVvqSRXq5ZLqX7rgK2Zkd"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


def b58decode(s):
    n = 0

    for c in s:
        n = n * 58 + ALPHABET.index(c)

    if n:
        raw = n.to_bytes(
            (n.bit_length() + 7) // 8,
            "big"
        )
    else:
        raw = b""

    pad = 0

    for c in s:
        if c == "1":
            pad += 1
        else:
            break

    return b"\x00" * pad + raw


def pubkey_value(key):
    """
    accountKeys dari jsonParsed bisa berupa:
    - string
    - object {"pubkey": "...", ...}
    """

    if isinstance(key, str):
        return key

    if isinstance(key, dict):
        return key.get("pubkey")

    return None


def get_all_keys(tx):
    """
    Resolve seluruh account index Solana:

    static account keys
        +
    loadedAddresses.writable
        +
    loadedAddresses.readonly

    Ini penting untuk versioned transactions.
    """

    result = tx.get("result") or {}

    transaction = result.get("transaction") or {}
    message = transaction.get("message") or {}
    meta = result.get("meta") or {}

    keys = []

    for key in message.get("accountKeys", []):
        value = pubkey_value(key)

        if value:
            keys.append(value)

    loaded = meta.get("loadedAddresses") or {}

    keys.extend(
        loaded.get("writable") or []
    )

    keys.extend(
        loaded.get("readonly") or []
    )

    return keys


def resolve_program(ix, keys):
    """
    Resolve program ID dari instruction.
    """

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


def decode_ix_data(data):
    """
    Decode raw instruction data.

    Solana JSON biasanya memakai base58.
    Fallback base64 disediakan untuk berjaga-jaga.
    """

    if not data:
        return None

    try:
        raw = b58decode(data)

        return {
            "encoding": "base58",
            "hex": raw.hex(),
            "length": len(raw),
            "u64_le": (
                int.from_bytes(raw, "little")
                if len(raw) == 8
                else None
            ),
        }

    except Exception:
        pass

    try:
        raw = base64.b64decode(data)

        return {
            "encoding": "base64",
            "hex": raw.hex(),
            "length": len(raw),
            "u64_le": (
                int.from_bytes(raw, "little")
                if len(raw) == 8
                else None
            ),
        }

    except Exception:
        return None


def flatten_outer(tx):
    """
    Ambil semua outer instructions dengan index aslinya.
    """

    result = tx.get("result") or {}

    transaction = result.get("transaction") or {}
    message = transaction.get("message") or {}

    keys = get_all_keys(tx)

    output = []

    for index, ix in enumerate(
        message.get("instructions", [])
    ):
        output.append({
            "outer_index": index,
            "program": resolve_program(ix, keys),
            "instruction": ix,
        })

    return output


def flatten_inner(tx):
    """
    Ambil seluruh inner instructions.

    Belum dipakai penuh di summary,
    tetapi disiapkan untuk analisis PumpSwap
    berikutnya.
    """

    result = tx.get("result") or {}
    meta = result.get("meta") or {}

    keys = get_all_keys(tx)

    output = []

    groups = meta.get("innerInstructions") or []

    for group in groups:
        outer_index = group.get("index")

        for inner_index, ix in enumerate(
            group.get("instructions") or []
        ):
            output.append({
                "outer_index": outer_index,
                "inner_index": inner_index,
                "program": resolve_program(ix, keys),
                "instruction": ix,
            })

    return output


def classify(logs):
    """
    Tentukan BUY / SELL / OTHER berdasarkan logs.
    """

    text = "\n".join(
        logs or []
    ).lower()

    if "instruction: buyexactquotein" in text:
        return "BUY"

    if "instruction: buy_exact_quote_in" in text:
        return "BUY"

    if "instruction: sell" in text:
        return "SELL"

    if "instruction: buy" in text:
        return "BUY"

    return "OTHER"


def instruction_error(meta):
    """
    Parse:

    {
      "InstructionError": [
        7,
        {"Custom": 6040}
      ]
    }

    menjadi:

    index = 7
    detail = {"Custom":6040}
    """

    err = meta.get("err")

    if not isinstance(err, dict):
        return None, None

    instruction_error_data = err.get(
        "InstructionError"
    )

    if (
        not isinstance(
            instruction_error_data,
            list
        )
        or len(instruction_error_data) < 2
    ):
        return None, None

    return (
        instruction_error_data[0],
        instruction_error_data[1],
    )


def extract_logs_around_error(logs):
    """
    Simpan 20 log terakhir.

    Biasanya bagian akhir log paling berguna
    untuk mengetahui program yang abort.
    """

    if not logs:
        return []

    return logs[-20:]


def main():

    OUT.mkdir(
        parents=True,
        exist_ok=True
    )

    records = []

    program_counts = Counter()
    error_programs = Counter()
    error_codes = Counter()

    guard_success = Counter()
    guard_failed = Counter()

    guard_raw_success = Counter()
    guard_raw_failed = Counter()

    sequence_counts = Counter()

    total = 0

    with INPUT.open(
        encoding="utf-8"
    ) as file:

        for line in file:

            if not line.strip():
                continue

            tx = json.loads(line)

            result = tx.get("result")

            if not result:
                continue

            total += 1

            meta = result.get("meta") or {}

            logs = (
                meta.get("logMessages")
                or []
            )

            action = classify(logs)

            success = (
                meta.get("err") is None
            )

            slot = result.get("slot")

            signature = tx.get(
                "requestedSignature"
            )

            if not signature:

                transaction = (
                    result.get("transaction")
                    or {}
                )

                signatures = (
                    transaction.get("signatures")
                    or []
                )

                if signatures:
                    signature = signatures[0]

            keys = get_all_keys(tx)

            outer = flatten_outer(tx)

            # ---------------------------------
            # Program execution sequence
            # ---------------------------------

            sequence = []

            for item in outer:

                program = item["program"]

                if program:
                    sequence.append(program)
                    program_counts[program] += 1
                else:
                    sequence.append("UNKNOWN")

            sequence_string = " -> ".join(
                sequence
            )

            sequence_counts[
                sequence_string
            ] += 1

            # ---------------------------------
            # Transaction error
            # ---------------------------------

            (
                error_index,
                error_detail,
            ) = instruction_error(meta)

            failing_program = None

            if (
                isinstance(error_index, int)
                and error_index < len(outer)
            ):
                failing_program = (
                    outer[error_index]["program"]
                )

            if not success:

                error_programs[
                    failing_program
                    or "UNKNOWN"
                ] += 1

                error_codes[
                    json.dumps(
                        error_detail,
                        sort_keys=True
                    )
                ] += 1

            # ---------------------------------
            # Guard 8rdf
            # ---------------------------------

            guard_items = [
                item
                for item in outer
                if item["program"] == GUARD
            ]

            guard_index = None
            guard_hex = None
            guard_length = None
            guard_u64 = None
            guard_distance = None

            if guard_items:

                guard = guard_items[0]

                guard_index = guard[
                    "outer_index"
                ]

                instruction = guard[
                    "instruction"
                ]

                decoded = decode_ix_data(
                    instruction.get("data")
                )

                if decoded:

                    guard_hex = decoded["hex"]

                    guard_length = decoded[
                        "length"
                    ]

                    guard_u64 = decoded[
                        "u64_le"
                    ]

                    if (
                        guard_u64 is not None
                        and slot is not None
                    ):
                        guard_distance = (
                            guard_u64 - slot
                        )

                    if success:
                        guard_raw_success[
                            guard_hex
                        ] += 1

                    else:
                        guard_raw_failed[
                            guard_hex
                        ] += 1

                if success:
                    guard_success[
                        str(guard_distance)
                    ] += 1

                else:
                    guard_failed[
                        str(guard_distance)
                    ] += 1

            # ---------------------------------
            # PumpSwap instruction positions
            # ---------------------------------

            pumpswap_indices = [
                item["outer_index"]
                for item in outer
                if item["program"] == PUMPSWAP
            ]

            # ---------------------------------
            # Inner instructions count
            # ---------------------------------

            inner = flatten_inner(tx)

            # ---------------------------------
            # Save forensic row
            # ---------------------------------

            records.append({

                "signature":
                    signature,

                "slot":
                    slot,

                "block_time":
                    result.get("blockTime"),

                "action":
                    action,

                "success":
                    success,

                "resolved_account_keys":
                    len(keys),

                "outer_instruction_count":
                    len(outer),

                "inner_instruction_count":
                    len(inner),

                "error_instruction_index":
                    error_index,

                "error_detail":
                    json.dumps(
                        error_detail,
                        separators=(",", ":")
                    )
                    if error_detail is not None
                    else "",

                "failing_program":
                    failing_program,

                "guard_index":
                    guard_index,

                "guard_hex":
                    guard_hex,

                "guard_length":
                    guard_length,

                "guard_u64":
                    guard_u64,

                "guard_distance_slots":
                    guard_distance,

                "pumpswap_indices":
                    ";".join(
                        map(
                            str,
                            pumpswap_indices
                        )
                    ),

                "program_sequence":
                    sequence_string,

                "last_logs":
                    " || ".join(
                        extract_logs_around_error(
                            logs
                        )
                    ),
            })

    if not records:
        raise RuntimeError(
            "Tidak ada transaksi yang berhasil dibaca."
        )

    # =====================================
    # CSV OUTPUT
    # =====================================

    csv_path = (
        OUT / "forensics.csv"
    )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=list(
                records[0].keys()
            )
        )

        writer.writeheader()
        writer.writerows(records)

    # =====================================
    # FAILED TRANSACTIONS
    # =====================================

    failed = [
        record
        for record in records
        if not record["success"]
    ]

    failed_buy = [
        record
        for record in failed
        if record["action"] == "BUY"
    ]

    fail_target = Counter()

    for record in failed_buy:

        program = record[
            "failing_program"
        ]

        if program == GUARD:

            fail_target[
                "8rdf_guard"
            ] += 1

        elif program == PUMPSWAP:

            fail_target[
                "pumpswap"
            ] += 1

        else:

            fail_target[
                program or "UNKNOWN"
            ] += 1

    # =====================================
    # SUMMARY
    # =====================================

    summary = {

        "transactions":
            total,

        "failed_transactions":
            len(failed),

        "failed_buys":
            len(failed_buy),

        "error_codes":
            dict(error_codes),

        "failing_programs":
            dict(error_programs),

        "failed_buy_target":
            dict(fail_target),

        "guard": {

            "program":
                GUARD,

            "successful_distance_distribution":
                dict(guard_success),

            "failed_distance_distribution":
                dict(guard_failed),

            "unique_success_raw_instructions":
                len(guard_raw_success),

            "unique_failed_raw_instructions":
                len(guard_raw_failed),

            "top_success_raw":
                guard_raw_success.most_common(
                    20
                ),

            "top_failed_raw":
                guard_raw_failed.most_common(
                    20
                ),
        },

        "top_outer_programs":
            program_counts.most_common(
                30
            ),

        "top_program_sequences":
            sequence_counts.most_common(
                20
            ),
    }

    json_path = (
        OUT / "forensics_summary.json"
    )

    json_path.write_text(
        json.dumps(
            summary,
            indent=2
        ),
        encoding="utf-8"
    )

    # =====================================
    # TERMINAL REPORT
    # =====================================

    print("=" * 60)
    print(
        "PHASE 2 — TRANSACTION FORENSICS"
    )
    print("=" * 60)

    print(
        "Transactions :",
        total
    )

    print(
        "Failed       :",
        len(failed)
    )

    print(
        "Failed BUY   :",
        len(failed_buy)
    )

    print()

    print(
        "FAILED BUY TARGET:"
    )

    for key, value in (
        fail_target.most_common()
    ):
        print(
            f"  {key}: {value}"
        )

    print()

    print(
        "ERROR CODES:"
    )

    for key, value in (
        error_codes.most_common()
    ):
        print(
            f"  {key}: {value}"
        )

    print()

    print(
        "GUARD SUCCESS DISTANCE:"
    )

    for key, value in (
        guard_success.most_common()
    ):
        print(
            f"  {key}: {value}"
        )

    print()

    print(
        "GUARD FAILED DISTANCE:"
    )

    for key, value in (
        guard_failed.most_common()
    ):
        print(
            f"  {key}: {value}"
        )

    print()

    print(
        "Output:"
    )

    print(
        " ",
        csv_path
    )

    print(
        " ",
        json_path
    )


if __name__ == "__main__":
    main()
