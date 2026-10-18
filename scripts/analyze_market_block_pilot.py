#!/usr/bin/env python3

import csv
import json
import os
from collections import Counter, defaultdict

INPUT_JSONL = "data/market_block_candidates.jsonl"
ROUNDTRIPS_CSV = "output/roundtrips.csv"

OUT_SUMMARY = "output/market_block_pilot_summary.json"
OUT_ENTRIES = "output/market_block_pilot_entries.csv"


def load_jsonl():
    rows = []

    with open(INPUT_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    return rows


def load_roundtrips():
    rows = []

    with open(ROUNDTRIPS_CSV, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if (
                row.get("buy_full_position", "").lower() == "true"
                and row.get("sell_full_position", "").lower() == "true"
            ):
                rows.append(row)

    return rows


def main():
    print("Phase 4 pilot analyzer — OFFLINE")
    print()

    blocks = load_jsonl()
    entries = load_roundtrips()

    print(f"Block records: {len(blocks)}")
    print(f"Roundtrip entries: {len(entries)}")

    block_by_slot = {
        int(row["slot"]): row
        for row in blocks
        if "slot" in row
    }

    candidate_signatures = set()
    pool_counter = Counter()
    pump_program_counter = Counter()
    status_counter = Counter()

    for row in blocks:
        status_counter[row.get("status", "unknown")] += 1

        for tx in row.get("candidate_transactions", []):
            sig = tx.get("signature")

            if sig:
                candidate_signatures.add(sig)

            for pool in tx.get("matched_pools", []):
                pool_counter[pool] += 1

            if tx.get("touches_pumpswap"):
                pump_program_counter["yes"] += 1
            else:
                pump_program_counter["no"] += 1

    # Determine which bot entries have their BUY block represented.
    covered_entries = []

    for i, entry in enumerate(entries):
        buy_slot = int(entry["buy_slot"])
        buy_time = int(entry["buy_block_time"])

        row = block_by_slot.get(buy_slot)

        if row is None:
            covered_entries.append({
                "entry_index": i,
                "mint": entry["mint"],
                "buy_signature": entry["buy_signature"],
                "buy_slot": buy_slot,
                "buy_block_time": buy_time,
                "buy_block_present": False,
                "candidate_count": 0,
                "pump_candidate_count": 0,
            })
            continue

        candidates = row.get("candidate_transactions", [])

        pump_candidates = [
            tx for tx in candidates
            if tx.get("touches_pumpswap") is True
        ]

        covered_entries.append({
            "entry_index": i,
            "mint": entry["mint"],
            "buy_signature": entry["buy_signature"],
            "buy_slot": buy_slot,
            "buy_block_time": buy_time,
            "buy_block_present": True,
            "candidate_count": len(candidates),
            "pump_candidate_count": len(pump_candidates),
        })

    covered = [
        x for x in covered_entries
        if x["buy_block_present"]
    ]

    # Check whether the bot's own BUY signature is present in the candidate
    # dataset.
    bot_buy_present = 0
    bot_buy_missing = 0

    for entry in entries:
        if entry["buy_signature"] in candidate_signatures:
            bot_buy_present += 1
        else:
            bot_buy_missing += 1

    # Time span covered by stored blocks.
    times = [
        int(row["block_time"])
        for row in blocks
        if row.get("block_time") is not None
    ]

    # Candidate density.
    total_candidates = sum(
        len(row.get("candidate_transactions", []))
        for row in blocks
    )

    total_pump_candidates = sum(
        sum(
            1
            for tx in row.get("candidate_transactions", [])
            if tx.get("touches_pumpswap") is True
        )
        for row in blocks
    )

    summary = {
        "input": INPUT_JSONL,
        "block_records": len(blocks),
        "roundtrip_entries": len(entries),

        "time_span": {
            "min_block_time": min(times) if times else None,
            "max_block_time": max(times) if times else None,
            "seconds": (
                max(times) - min(times)
                if times
                else None
            ),
        },

        "status_counts": dict(status_counter),

        "candidate_transactions": {
            "total": total_candidates,
            "unique_signatures": len(candidate_signatures),
            "touching_pumpswap": total_pump_candidates,
            "not_touching_pumpswap": (
                total_candidates - total_pump_candidates
            ),
        },

        "entry_coverage": {
            "buy_block_present": len(covered),
            "buy_block_missing": len(entries) - len(covered),
            "coverage_pct": (
                len(covered) / len(entries) * 100
                if entries
                else 0
            ),
            "bot_buy_signature_present": bot_buy_present,
            "bot_buy_signature_missing": bot_buy_missing,
            "bot_buy_signature_coverage_pct": (
                bot_buy_present / len(entries) * 100
                if entries
                else 0
            ),
        },

        "top_pools_by_candidate_count": [
            {
                "pool": pool,
                "candidate_count": count,
            }
            for pool, count in pool_counter.most_common(20)
        ],

        "pump_program_presence": dict(
            pump_program_counter
        ),
    }

    os.makedirs("output", exist_ok=True)

    with open(OUT_SUMMARY, "w") as f:
        json.dump(summary, f, indent=2)

    with open(OUT_ENTRIES, "w", newline="") as f:
        fieldnames = [
            "entry_index",
            "mint",
            "buy_signature",
            "buy_slot",
            "buy_block_time",
            "buy_block_present",
            "candidate_count",
            "pump_candidate_count",
        ]

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(covered_entries)

    print()
    print("=== PILOT SUMMARY ===")
    print(
        f"Block records: {len(blocks)}"
    )
    print(
        f"Candidate transactions: {total_candidates}"
    )
    print(
        f"Unique candidate signatures: "
        f"{len(candidate_signatures)}"
    )
    print(
        f"PumpSwap candidates: "
        f"{total_pump_candidates}"
    )
    print()
    print(
        f"BUY blocks covered: "
        f"{len(covered)}/{len(entries)} "
        f"({summary['entry_coverage']['coverage_pct']:.2f}%)"
    )
    print(
        f"Bot BUY signatures found: "
        f"{bot_buy_present}/{len(entries)} "
        f"({summary['entry_coverage']['bot_buy_signature_coverage_pct']:.2f}%)"
    )

    if times:
        print()
        print(
            f"Time span: "
            f"{min(times)} -> {max(times)} "
            f"({max(times) - min(times)} seconds)"
        )

    print()
    print(f"Written: {OUT_SUMMARY}")
    print(f"Written: {OUT_ENTRIES}")


if __name__ == "__main__":
    main()
