import csv
import json
from pathlib import Path
from collections import defaultdict, deque
from statistics import median


ROOT = Path(__file__).resolve().parent.parent

INPUT = ROOT / "output" / "exact_trade_flow.csv"
OUT_CSV = ROOT / "output" / "roundtrips.csv"
OUT_JSON = ROOT / "output" / "roundtrips_summary.json"


def as_int(value):
    if value is None or value == "":
        return None

    return int(value)


def percentile(values, p):
    if not values:
        return None

    values = sorted(values)

    k = (len(values) - 1) * p

    lower = int(k)
    upper = min(lower + 1, len(values) - 1)

    if lower == upper:
        return values[lower]

    return (
        values[lower]
        + (values[upper] - values[lower])
        * (k - lower)
    )


# ============================================================
# LOAD EXACT TRADE FLOW
# ============================================================

with INPUT.open(
    encoding="utf-8"
) as file:

    rows = list(
        csv.DictReader(file)
    )


for row in rows:

    row["slot"] = as_int(
        row["slot"]
    )

    row["block_time"] = as_int(
        row["block_time"]
    )

    row["input_raw"] = as_int(
        row["input_raw"]
    )

    row["limit_raw"] = as_int(
        row["limit_raw"]
    )

    row["wallet_sol_delta_lamports"] = as_int(
        row["wallet_sol_delta_lamports"]
    )

    row["network_fee_lamports"] = as_int(
        row["network_fee_lamports"]
    )

    row["trade_token_delta_raw"] = as_int(
        row["trade_token_delta_raw"]
    )

    row["success"] = (
        row["success"] == "True"
    )


# Blockchain records were originally newest -> oldest.
# For FIFO matching we need chronological order.

rows.sort(
    key=lambda row: (
        row["block_time"] or 0,
        row["slot"] or 0
    )
)


# ============================================================
# QUANTITY-AWARE FIFO MATCHING
# ============================================================

open_buys = defaultdict(deque)

pairs = []

unmatched_sells = []


for row in rows:

    if not row["success"]:
        continue

    mint = row["trade_token_mint"]

    if not mint:
        continue

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if row["action"] == "BUY":

        quantity = row[
            "trade_token_delta_raw"
        ]

        if (
            quantity is None
            or quantity <= 0
        ):
            continue

        open_buys[mint].append({
            "row": row,
            "remaining": quantity
        })

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    elif row["action"] == "SELL":

        # Important:
        # Keep an explicit reference to the current SELL row.
        sell = row

        sell_quantity = abs(
            sell[
                "trade_token_delta_raw"
            ] or 0
        )

        if sell_quantity <= 0:
            continue

        remaining_sell = (
            sell_quantity
        )

        while (
            remaining_sell > 0
            and open_buys[mint]
        ):

            buy_lot = (
                open_buys[mint][0]
            )

            buy = buy_lot["row"]

            matched_quantity = min(
                remaining_sell,
                buy_lot["remaining"]
            )

            buy_total_quantity = (
                buy[
                    "trade_token_delta_raw"
                ]
            )

            sell_total_quantity = (
                sell_quantity
            )

            buy_fraction = (
                matched_quantity
                / buy_total_quantity
            )

            sell_fraction = (
                matched_quantity
                / sell_total_quantity
            )

            # =================================================
            # BUY COST
            # =================================================
            #
            # input_raw on BUY =
            # PumpSwap spendable_quote_in.
            #
            # This is the swap budget itself.
            # Account rent is intentionally NOT treated
            # as trading cost here.

            buy_swap_cost = (
                buy["input_raw"]
                * buy_fraction
            )

            buy_network_fee = (
                (
                    buy[
                        "network_fee_lamports"
                    ]
                    or 0
                )
                * buy_fraction
            )

            economic_cost = (
                buy_swap_cost
                + buy_network_fee
            )

            # =================================================
            # SELL PROCEEDS
            # =================================================
            #
            # wallet_sol_delta_lamports on a successful SELL
            # already represents:
            #
            # native SOL received
            # minus transaction fee
            #
            # Therefore this value can be used directly
            # as economic proceeds after the SELL network fee.

            sell_wallet_delta = (
                sell[
                    "wallet_sol_delta_lamports"
                ]
                or 0
            )

            sell_network_fee_total = (
                sell[
                    "network_fee_lamports"
                ]
                or 0
            )

            economic_proceeds = (
                sell_wallet_delta
                * sell_fraction
            )

            # For diagnostics only:
            # reconstruct native SOL received before
            # the SELL transaction network fee.

            sell_native_before_fee = (
                sell_wallet_delta
                + sell_network_fee_total
            ) * sell_fraction

            sell_network_fee = (
                sell_network_fee_total
                * sell_fraction
            )

            # =================================================
            # PNL
            # =================================================

            pnl_lamports = (
                economic_proceeds
                - economic_cost
            )

            if economic_cost > 0:

                return_pct = (
                    pnl_lamports
                    / economic_cost
                    * 100
                )

            else:

                return_pct = None

            # =================================================
            # HOLD TIME
            # =================================================

            if (
                buy["block_time"]
                is not None
                and sell["block_time"]
                is not None
            ):

                hold_seconds = (
                    sell["block_time"]
                    - buy["block_time"]
                )

            else:

                hold_seconds = None

            # =================================================
            # SAVE PAIR
            # =================================================

            pairs.append({

                "mint":
                    mint,

                "buy_signature":
                    buy["signature"],

                "sell_signature":
                    sell["signature"],

                "buy_slot":
                    buy["slot"],

                "sell_slot":
                    sell["slot"],

                "buy_block_time":
                    buy["block_time"],

                "sell_block_time":
                    sell["block_time"],

                "hold_seconds":
                    hold_seconds,

                "matched_token_raw":
                    matched_quantity,

                "token_decimals":
                    buy[
                        "trade_token_decimals"
                    ],

                "buy_instruction_budget_lamports":
                    round(
                        buy_swap_cost
                    ),

                "buy_network_fee_lamports":
                    round(
                        buy_network_fee
                    ),

                "economic_cost_lamports":
                    round(
                        economic_cost
                    ),

                "sell_native_before_network_fee_lamports":
                    round(
                        sell_native_before_fee
                    ),

                "sell_network_fee_lamports":
                    round(
                        sell_network_fee
                    ),

                "economic_proceeds_lamports":
                    round(
                        economic_proceeds
                    ),

                "pnl_lamports":
                    round(
                        pnl_lamports
                    ),

                "pnl_sol":
                    (
                        pnl_lamports
                        / 1_000_000_000
                    ),

                "return_pct":
                    return_pct,

                "buy_full_position":
                    (
                        matched_quantity
                        == buy_total_quantity
                    ),

                "sell_full_position":
                    (
                        matched_quantity
                        == sell_total_quantity
                    )
            })

            # =================================================
            # UPDATE REMAINING QUANTITIES
            # =================================================

            buy_lot["remaining"] -= (
                matched_quantity
            )

            remaining_sell -= (
                matched_quantity
            )

            if (
                buy_lot["remaining"]
                == 0
            ):

                open_buys[
                    mint
                ].popleft()

        # If a SELL cannot be completely matched,
        # preserve it rather than forcing a fake pair.

        if remaining_sell > 0:

            unmatched_sells.append({

                "signature":
                    sell["signature"],

                "mint":
                    mint,

                "unmatched_raw":
                    remaining_sell,

                "block_time":
                    sell["block_time"]
            })


# ============================================================
# UNMATCHED BUYS
# ============================================================

unmatched_buys = []


for mint, queue in open_buys.items():

    for lot in queue:

        unmatched_buys.append({

            "signature":
                lot["row"][
                    "signature"
                ],

            "mint":
                mint,

            "remaining_raw":
                lot["remaining"],

            "block_time":
                lot["row"][
                    "block_time"
                ]
        })


# ============================================================
# WRITE ROUNDTRIPS CSV
# ============================================================

if pairs:

    with OUT_CSV.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=list(
                pairs[0].keys()
            )
        )

        writer.writeheader()

        writer.writerows(
            pairs
        )


# ============================================================
# STATISTICS
# ============================================================

returns = [

    pair["return_pct"]

    for pair in pairs

    if pair["return_pct"]
    is not None
]


holds = [

    pair["hold_seconds"]

    for pair in pairs

    if pair["hold_seconds"]
    is not None
]


pnls = [

    pair["pnl_sol"]

    for pair in pairs
]


wins = sum(
    1
    for value in returns
    if value > 0
)


losses = sum(
    1
    for value in returns
    if value < 0
)


flat = sum(
    1
    for value in returns
    if value == 0
)


full_pairs = sum(

    1

    for pair in pairs

    if (
        pair[
            "buy_full_position"
        ]
        and
        pair[
            "sell_full_position"
        ]
    )
)


# ============================================================
# HOLD-TIME BUCKETS
# ============================================================

hold_buckets = {

    "0_5": {
        "count": 0,
        "wins": 0,
        "returns": []
    },

    "6_15": {
        "count": 0,
        "wins": 0,
        "returns": []
    },

    "16_30": {
        "count": 0,
        "wins": 0,
        "returns": []
    },

    "31_60": {
        "count": 0,
        "wins": 0,
        "returns": []
    },

    "61_120": {
        "count": 0,
        "wins": 0,
        "returns": []
    },

    "121_plus": {
        "count": 0,
        "wins": 0,
        "returns": []
    }
}


for pair in pairs:

    hold = pair["hold_seconds"]
    ret = pair["return_pct"]

    if (
        hold is None
        or ret is None
    ):
        continue

    if hold <= 5:
        key = "0_5"

    elif hold <= 15:
        key = "6_15"

    elif hold <= 30:
        key = "16_30"

    elif hold <= 60:
        key = "31_60"

    elif hold <= 120:
        key = "61_120"

    else:
        key = "121_plus"

    bucket = (
        hold_buckets[key]
    )

    bucket["count"] += 1

    if ret > 0:
        bucket["wins"] += 1

    bucket[
        "returns"
    ].append(ret)


hold_bucket_summary = {}


for key, bucket in (
    hold_buckets.items()
):

    bucket_returns = (
        bucket["returns"]
    )

    hold_bucket_summary[key] = {

        "count":
            bucket["count"],

        "wins":
            bucket["wins"],

        "win_rate_pct":
            (
                bucket["wins"]
                / bucket["count"]
                * 100
                if bucket["count"]
                else None
            ),

        "average_return_pct":
            (
                sum(bucket_returns)
                / len(bucket_returns)
                if bucket_returns
                else None
            ),

        "median_return_pct":
            (
                median(
                    bucket_returns
                )
                if bucket_returns
                else None
            )
    }


# ============================================================
# SUMMARY
# ============================================================

summary = {

    "pair_fragments":
        len(pairs),

    "full_buy_full_sell_pairs":
        full_pairs,

    "unmatched_sell_count":
        len(unmatched_sells),

    "unmatched_buy_count":
        len(unmatched_buys),

    "hold_seconds": {

        "min":
            min(holds)
            if holds
            else None,

        "median":
            median(holds)
            if holds
            else None,

        "p75":
            percentile(
                holds,
                0.75
            ),

        "p90":
            percentile(
                holds,
                0.90
            ),

        "max":
            max(holds)
            if holds
            else None
    },

    "return_pct": {

        "min":
            min(returns)
            if returns
            else None,

        "p10":
            percentile(
                returns,
                0.10
            ),

        "p25":
            percentile(
                returns,
                0.25
            ),

        "median":
            median(returns)
            if returns
            else None,

        "p75":
            percentile(
                returns,
                0.75
            ),

        "p90":
            percentile(
                returns,
                0.90
            ),

        "max":
            max(returns)
            if returns
            else None
    },

    "wins":
        wins,

    "losses":
        losses,

    "flat":
        flat,

    "win_rate_pct":
        (
            wins
            / len(returns)
            * 100
            if returns
            else None
        ),

    "sum_pnl_sol":
        sum(pnls),

    "median_pnl_sol":
        (
            median(pnls)
            if pnls
            else None
        ),

    "hold_time_buckets":
        hold_bucket_summary,

    "unmatched_sells":
        unmatched_sells,

    "unmatched_buys":
        unmatched_buys,

    "methodology": [

        (
            "Successful trades only."
        ),

        (
            "Quantity-aware FIFO matching "
            "is performed independently "
            "for each token mint."
        ),

        (
            "BUY cost uses decoded "
            "PumpSwap spendable_quote_in."
        ),

        (
            "BUY network transaction fee "
            "is added separately."
        ),

        (
            "SELL proceeds use the target "
            "wallet native SOL balance delta."
        ),

        (
            "SELL native amount before "
            "network fee is reconstructed "
            "only as a diagnostic field."
        ),

        (
            "Rent and token-account creation "
            "effects are excluded from BUY "
            "trading cost."
        ),

        (
            "Unmatched positions are "
            "preserved and never force-paired."
        )
    ]
}


OUT_JSON.write_text(

    json.dumps(
        summary,
        indent=2
    ),

    encoding="utf-8"
)


# ============================================================
# TERMINAL OUTPUT
# ============================================================

print(
    "=" * 60
)

print(
    "PHASE 3A — ROUND-TRIP RECONSTRUCTION"
)

print(
    "=" * 60
)

print(
    "Pair fragments :",
    len(pairs)
)

print(
    "Full pairs     :",
    full_pairs
)

print(
    "Unmatched SELL :",
    len(unmatched_sells)
)

print(
    "Unmatched BUY  :",
    len(unmatched_buys)
)

print()


if returns:

    print(
        "Wins           :",
        wins
    )

    print(
        "Losses         :",
        losses
    )

    print(
        "Win rate       :",
        f"{summary['win_rate_pct']:.2f}%"
    )

    print(
        "Median return  :",
        f"{summary['return_pct']['median']:.4f}%"
    )

    print(
        "Total PnL      :",
        f"{summary['sum_pnl_sol']:.9f} SOL"
    )


if holds:

    print(
        "Median hold    :",
        f"{summary['hold_seconds']['median']} sec"
    )

    print(
        "P90 hold       :",
        f"{summary['hold_seconds']['p90']:.1f} sec"
    )


print()

print(
    "Output:"
)

print(
    " ",
    OUT_CSV
)

print(
    " ",
    OUT_JSON
)
