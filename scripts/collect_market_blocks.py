#!/usr/bin/env python3

import csv
import json
import os
import time
import urllib.request
import urllib.error
from collections import defaultdict

RPC_URL = "https://api.mainnet-beta.solana.com"

ROUNDTRIPS_CSV = "output/roundtrips.csv"
POOLS_CSV = "output/pumpswap_pools.csv"

OUT_JSONL = "data/market_block_candidates.jsonl"
SUMMARY_JSON = "output/market_block_candidates_summary.json"

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

# We want market context around ENTRY.
BEFORE_SECONDS = 30
AFTER_SECONDS = 10

# Solana ~400 ms/slot nominally.
# Use a deliberately wider slot search range; blockTime later performs
# the exact timestamp filtering.
SLOTS_PER_SECOND_ESTIMATE = 2.5
SLOT_SAFETY_SECONDS = 8

REQUEST_DELAY = 0.55
MAX_RETRIES = 8


def rpc_call(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode()

    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "pump-wallet-market-audit/1.0",
        },
        method="POST",
    )

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode()
                data = json.loads(body)

            if "error" in data:
                err = data["error"]

                # Retry rate limits / transient server errors.
                code = err.get("code") if isinstance(err, dict) else None

                if code in (-32005, -32603):
                    wait = min(2 ** attempt, 30)
                    print(f"    RPC error {err}; retry in {wait}s")
                    time.sleep(wait)
                    continue

                raise RuntimeError(f"RPC {method} error: {err}")

            time.sleep(REQUEST_DELAY)
            return data.get("result")

        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code <= 599:
                wait = min(2 ** attempt, 30)
                print(f"    HTTP {e.code}; retry in {wait}s")
                time.sleep(wait)
                continue
            raise

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
        ) as e:
            wait = min(2 ** attempt, 30)
            print(f"    network error {e}; retry in {wait}s")
            time.sleep(wait)

    raise RuntimeError(
        f"{method} failed after {MAX_RETRIES} retries"
    )


def load_pool_map():
    """
    Map base mint -> PumpSwap pool from the already validated Phase 4A.1
    output.
    """
    mapping = {}

    with open(POOLS_CSV, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            mint = (
                row.get("base_mint")
                or row.get("mint")
                or ""
            ).strip()

            pool = (
                row.get("pool")
                or row.get("pool_address")
                or row.get("pool_pubkey")
                or ""
            ).strip()

            if mint and pool:
                if mint in mapping and mapping[mint] != pool:
                    raise RuntimeError(
                        f"Mint {mint} maps to multiple pools: "
                        f"{mapping[mint]} vs {pool}"
                    )

                mapping[mint] = pool

    if not mapping:
        raise RuntimeError(
            "Could not load mint -> pool mapping from "
            f"{POOLS_CSV}"
        )

    return mapping


def load_entries(pool_map):
    entries = []

    with open(ROUNDTRIPS_CSV, newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # Phase 3A reconstructed full BUY -> full SELL pairs.
            # Still explicitly require both flags.
            if row.get("buy_full_position", "").lower() != "true":
                continue

            if row.get("sell_full_position", "").lower() != "true":
                continue

            mint = row["mint"]
            pool = pool_map.get(mint)

            if not pool:
                print(f"WARNING: no pool mapping for mint {mint}")
                continue

            entries.append({
                "mint": mint,
                "pool": pool,
                "buy_signature": row["buy_signature"],
                "buy_slot": int(row["buy_slot"]),
                "buy_block_time": int(row["buy_block_time"]),
                "sell_signature": row["sell_signature"],
                "sell_slot": int(row["sell_slot"]),
                "sell_block_time": int(row["sell_block_time"]),
            })

    return entries


def build_slot_ranges(entries):
    """
    Create broad slot ranges around each BUY.

    Timestamp filtering is performed later using actual blockTime, so the
    slot conversion here is only used to find candidate blocks.
    """
    before_slots = int(
        (BEFORE_SECONDS + SLOT_SAFETY_SECONDS)
        * SLOTS_PER_SECOND_ESTIMATE
    )

    after_slots = int(
        (AFTER_SECONDS + SLOT_SAFETY_SECONDS)
        * SLOTS_PER_SECOND_ESTIMATE
    )

    ranges = []

    for i, entry in enumerate(entries):
        ranges.append({
            "entry_index": i,
            "start_slot": max(0, entry["buy_slot"] - before_slots),
            "end_slot": entry["buy_slot"] + after_slots,
        })

    return ranges


def merge_slot_ranges(ranges):
    """
    Merge overlapping/adjacent slot ranges so the same block is never
    downloaded repeatedly for nearby bot entries.
    """
    if not ranges:
        return []

    ordered = sorted(
        ranges,
        key=lambda x: (x["start_slot"], x["end_slot"])
    )

    merged = [{
        "start_slot": ordered[0]["start_slot"],
        "end_slot": ordered[0]["end_slot"],
    }]

    for r in ordered[1:]:
        last = merged[-1]

        if r["start_slot"] <= last["end_slot"] + 1:
            last["end_slot"] = max(
                last["end_slot"],
                r["end_slot"]
            )
        else:
            merged.append({
                "start_slot": r["start_slot"],
                "end_slot": r["end_slot"],
            })

    return merged


def load_completed_slots():
    completed = set()

    if not os.path.exists(OUT_JSONL):
        return completed

    with open(OUT_JSONL) as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
                if "slot" in row:
                    completed.add(int(row["slot"]))
            except Exception:
                pass

    return completed


def extract_account_keys(tx_item):
    """
    transactionDetails='accounts' returns account keys without requiring
    full instruction decoding.
    """
    tx = tx_item.get("transaction") or {}

    keys = (
        tx.get("accountKeys")
        or tx.get("message", {}).get("accountKeys")
        or []
    )

    result = []

    for key in keys:
        if isinstance(key, str):
            result.append(key)
        elif isinstance(key, dict):
            pubkey = key.get("pubkey")
            if pubkey:
                result.append(pubkey)

    return result


def relevant_entries_for_block(entries, block_time):
    if block_time is None:
        return []

    relevant = []

    for i, entry in enumerate(entries):
        start_time = entry["buy_block_time"] - BEFORE_SECONDS
        end_time = entry["buy_block_time"] + AFTER_SECONDS

        if start_time <= block_time <= end_time:
            relevant.append(i)

    return relevant


def fetch_block(slot):
    return rpc_call(
        "getBlock",
        [
            slot,
            {
                "commitment": "finalized",
                "encoding": "json",
                "transactionDetails": "accounts",
                "maxSupportedTransactionVersion": 1,
                "rewards": False,
            },
        ],
    )


def append_record(record):
    os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)

    with open(OUT_JSONL, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.flush()


def main():
    print("Phase 4A.2 V2 — slot-based market block collector")
    print(f"RPC: {RPC_URL}")
    print(
        f"Entry window: T-{BEFORE_SECONDS}s -> T+{AFTER_SECONDS}s"
    )
    print()

    pool_map = load_pool_map()
    entries = load_entries(pool_map)

    print(f"Pool mappings: {len(pool_map)}")
    print(f"Full roundtrip entries: {len(entries)}")

    if not entries:
        raise RuntimeError("No entries loaded")

    ranges = build_slot_ranges(entries)
    merged = merge_slot_ranges(ranges)

    raw_slot_count = sum(
        r["end_slot"] - r["start_slot"] + 1
        for r in ranges
    )

    merged_slot_count = sum(
        r["end_slot"] - r["start_slot"] + 1
        for r in merged
    )

    print(f"Raw entry ranges: {len(ranges)}")
    print(f"Merged ranges: {len(merged)}")
    print(f"Raw candidate slots: {raw_slot_count}")
    print(f"After overlap merge: {merged_slot_count}")
    print()

    completed = load_completed_slots()

    if completed:
        print(
            f"Resume: {len(completed)} block slots already stored"
        )
        print()

    total_blocks = 0
    relevant_blocks = 0
    candidate_txs = 0
    skipped_completed = 0
    null_blocks = 0
    errors = []

    all_target_pools = set(e["pool"] for e in entries)

    for range_i, r in enumerate(merged, start=1):
        start_slot = r["start_slot"]
        end_slot = r["end_slot"]

        print(
            f"[range {range_i}/{len(merged)}] "
            f"{start_slot} -> {end_slot}"
        )

        try:
            confirmed_slots = rpc_call(
                "getBlocks",
                [
                    start_slot,
                    end_slot,
                    {
                        "commitment": "finalized"
                    },
                ],
            ) or []

        except Exception as e:
            print(f"  getBlocks FAILED: {e}")
            errors.append({
                "range": [start_slot, end_slot],
                "stage": "getBlocks",
                "error": str(e),
            })
            continue

        print(f"  confirmed blocks: {len(confirmed_slots)}")

        for n, slot in enumerate(confirmed_slots, start=1):
            if slot in completed:
                skipped_completed += 1
                continue

            try:
                block = fetch_block(slot)

                if block is None:
                    null_blocks += 1
                    append_record({
                        "slot": slot,
                        "status": "null_block",
                    })
                    completed.add(slot)
                    continue

                total_blocks += 1

                block_time = block.get("blockTime")

                relevant_entry_indexes = relevant_entries_for_block(
                    entries,
                    block_time,
                )

                # Slot estimate deliberately has safety padding.
                # Blocks outside the exact timestamp windows do not need
                # transaction scanning.
                if not relevant_entry_indexes:
                    append_record({
                        "slot": slot,
                        "block_time": block_time,
                        "status": "outside_exact_time_window",
                        "candidate_transactions": [],
                    })
                    completed.add(slot)
                    continue

                relevant_pools = {
                    entries[i]["pool"]
                    for i in relevant_entry_indexes
                }

                tx_candidates = []

                for tx_item in block.get("transactions") or []:
                    keys = set(extract_account_keys(tx_item))

                    matched_pools = sorted(
                        relevant_pools.intersection(keys)
                    )

                    if not matched_pools:
                        continue

                    # PumpSwap program should normally also be present.
                    # Keep this boolean instead of dropping the transaction,
                    # so Phase 4 audit can verify it independently.
                    touches_pumpswap = PUMPSWAP in keys

                    tx = tx_item.get("transaction") or {}
                    signatures = tx.get("signatures") or []

                    signature = (
                        signatures[0]
                        if signatures
                        else None
                    )

                    tx_candidates.append({
                        "signature": signature,
                        "matched_pools": matched_pools,
                        "touches_pumpswap": touches_pumpswap,
                    })

                if tx_candidates:
                    relevant_blocks += 1
                    candidate_txs += len(tx_candidates)

                append_record({
                    "slot": slot,
                    "block_time": block_time,
                    "status": "ok",
                    "relevant_entry_indexes":
                        relevant_entry_indexes,
                    "candidate_transactions": tx_candidates,
                })

                completed.add(slot)

                if n % 20 == 0 or tx_candidates:
                    print(
                        f"    block {n}/{len(confirmed_slots)} "
                        f"slot={slot} "
                        f"candidates={len(tx_candidates)}"
                    )

            except KeyboardInterrupt:
                print()
                print("Interrupted.")
                print(
                    "Completed block records are already saved. "
                    "Run again to resume."
                )
                return

            except Exception as e:
                print(f"    slot {slot} FAILED: {e}")

                errors.append({
                    "slot": slot,
                    "stage": "getBlock",
                    "error": str(e),
                })

    unique_candidate_signatures = set()

    if os.path.exists(OUT_JSONL):
        with open(OUT_JSONL) as f:
            for line in f:
                try:
                    row = json.loads(line)

                    for tx in row.get(
                        "candidate_transactions", []
                    ):
                        sig = tx.get("signature")
                        if sig:
                            unique_candidate_signatures.add(sig)

                except Exception:
                    pass

    summary = {
        "rpc_url": RPC_URL,
        "before_seconds": BEFORE_SECONDS,
        "after_seconds": AFTER_SECONDS,
        "slot_safety_seconds": SLOT_SAFETY_SECONDS,
        "slots_per_second_estimate":
            SLOTS_PER_SECOND_ESTIMATE,
        "pool_mappings": len(pool_map),
        "entries": len(entries),
        "raw_ranges": len(ranges),
        "merged_ranges": len(merged),
        "raw_candidate_slots": raw_slot_count,
        "merged_candidate_slots": merged_slot_count,
        "completed_slot_records": len(completed),
        "blocks_fetched_this_run": total_blocks,
        "relevant_blocks_this_run": relevant_blocks,
        "candidate_transactions_this_run": candidate_txs,
        "unique_candidate_signatures_total":
            len(unique_candidate_signatures),
        "skipped_completed_this_run": skipped_completed,
        "null_blocks_this_run": null_blocks,
        "errors": errors,
    }

    os.makedirs(os.path.dirname(SUMMARY_JSON), exist_ok=True)

    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)

    print()
    print("DONE")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
