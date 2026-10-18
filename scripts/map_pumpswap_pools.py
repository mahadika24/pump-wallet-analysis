import csv
import json
from pathlib import Path
from collections import Counter, defaultdict


ROOT = Path(__file__).resolve().parent.parent

INPUT = ROOT / "data" / "transactions.jsonl"

OUT_CSV = ROOT / "output" / "pumpswap_pools.csv"
OUT_JSON = ROOT / "output" / "pumpswap_pools_summary.json"


WALLET = "GRCkjXgEEWQnqveX4oWTNSZroMo4wMwx7EWZFqzZubeB"

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


ALPHABET = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    "abcdefghijkmnopqrstuvwxyz"
)


BUY_EXACT_QUOTE_IN = bytes([
    198, 46, 21, 82,
    180, 217, 232, 112
])


SELL = bytes([
    51, 230, 133, 164,
    1, 127, 131, 173
])


def b58decode(value):

    n = 0

    for char in value:
        n = (
            n * 58
            + ALPHABET.index(char)
        )

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

    return (
        b"\x00" * padding
        + raw
    )


def pubkey_value(value):

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return value.get("pubkey")

    return None


def get_keys(result):

    tx = (
        result.get("transaction")
        or {}
    )

    message = (
        tx.get("message")
        or {}
    )

    meta = (
        result.get("meta")
        or {}
    )

    keys = []

    for item in (
        message.get("accountKeys")
        or []
    ):

        key = pubkey_value(item)

        if key:
            keys.append(key)

    loaded = (
        meta.get("loadedAddresses")
        or {}
    )

    keys.extend(
        loaded.get("writable")
        or []
    )

    keys.extend(
        loaded.get("readonly")
        or []
    )

    return keys


def resolve_program(ix, keys):

    program_id = ix.get(
        "programId"
    )

    if program_id:
        return program_id

    index = ix.get(
        "programIdIndex"
    )

    if (
        isinstance(index, int)
        and index < len(keys)
    ):

        return keys[index]

    return None


def resolve_accounts(ix, keys):

    result = []

    for value in (
        ix.get("accounts")
        or []
    ):

        if isinstance(value, int):

            if value < len(keys):
                result.append(
                    keys[value]
                )

            else:
                result.append(None)

        elif isinstance(value, str):

            result.append(value)

        else:

            result.append(None)

    return result


def get_signature(wrapper, result):

    requested = wrapper.get(
        "requestedSignature"
    )

    if requested:
        return requested

    signatures = (
        (
            result.get("transaction")
            or {}
        ).get("signatures")
        or []
    )

    if signatures:
        return signatures[0]

    return None


def identify_action(raw):

    if len(raw) < 8:
        return None

    discriminator = raw[:8]

    if (
        discriminator
        == BUY_EXACT_QUOTE_IN
    ):
        return "BUY"

    if discriminator == SELL:
        return "SELL"

    return None


def find_pumpswap(result, keys):

    message = (
        (
            result.get("transaction")
            or {}
        ).get("message")
        or {}
    )

    instructions = (
        message.get("instructions")
        or []
    )

    for outer_index, ix in enumerate(
        instructions
    ):

        if (
            resolve_program(ix, keys)
            != PUMPSWAP
        ):
            continue

        data = ix.get("data")

        if not data:
            continue

        try:

            raw = b58decode(data)

        except Exception:

            continue

        action = identify_action(raw)

        if action is None:
            continue

        accounts = resolve_accounts(
            ix,
            keys
        )

        return {
            "action": action,
            "outer_index": outer_index,
            "accounts": accounts,
            "raw": raw
        }

    return None


def account_at(accounts, index):

    if index < len(accounts):
        return accounts[index]

    return None


rows = []

pool_counts = Counter()
pool_buy_counts = Counter()
pool_sell_counts = Counter()

mint_to_pools = defaultdict(set)
pool_to_mints = defaultdict(set)

account_lengths = Counter()

wallet_mismatch = 0
mint_mismatch = 0


with INPUT.open(
    encoding="utf-8"
) as file:

    for line in file:

        if not line.strip():
            continue

        wrapper = json.loads(line)

        result = wrapper.get(
            "result"
        )

        if not result:
            continue

        keys = get_keys(result)

        swap = find_pumpswap(
            result,
            keys
        )

        if not swap:
            continue

        accounts = swap[
            "accounts"
        ]

        action = swap[
            "action"
        ]

        meta = (
            result.get("meta")
            or {}
        )

        success = (
            meta.get("err")
            is None
        )

        # Official PumpSwap layout:
        #
        # 0 pool
        # 1 user
        # 2 global_config
        # 3 base_mint
        # 4 quote_mint
        # 5 user_base_token_account
        # 6 user_quote_token_account
        # 7 pool_base_token_account
        # 8 pool_quote_token_account
        # 9 protocol_fee_recipient
        # 10 protocol_fee_recipient_token_account
        #
        # Later accounts may include token programs,
        # system/ATA program, event authority,
        # creator fee accounts, accumulators, etc.

        pool = account_at(
            accounts, 0
        )

        user = account_at(
            accounts, 1
        )

        global_config = account_at(
            accounts, 2
        )

        base_mint = account_at(
            accounts, 3
        )

        quote_mint = account_at(
            accounts, 4
        )

        user_base = account_at(
            accounts, 5
        )

        user_quote = account_at(
            accounts, 6
        )

        pool_base = account_at(
            accounts, 7
        )

        pool_quote = account_at(
            accounts, 8
        )

        protocol_fee_recipient = (
            account_at(
                accounts, 9
            )
        )

        protocol_fee_account = (
            account_at(
                accounts, 10
            )
        )

        if user != WALLET:
            wallet_mismatch += 1

        # Validate base mint using wallet token
        # balances whenever possible.

        wallet_token_mints = set()

        for item in (
            meta.get(
                "preTokenBalances"
            )
            or []
        ):

            if (
                item.get("owner")
                == WALLET
            ):

                mint = item.get("mint")

                if mint:
                    wallet_token_mints.add(
                        mint
                    )

        for item in (
            meta.get(
                "postTokenBalances"
            )
            or []
        ):

            if (
                item.get("owner")
                == WALLET
            ):

                mint = item.get("mint")

                if mint:
                    wallet_token_mints.add(
                        mint
                    )

        if (
            base_mint
            and wallet_token_mints
            and base_mint
            not in wallet_token_mints
        ):

            mint_mismatch += 1

        if pool:

            pool_counts[
                pool
            ] += 1

            if action == "BUY":

                pool_buy_counts[
                    pool
                ] += 1

            elif action == "SELL":

                pool_sell_counts[
                    pool
                ] += 1

        if (
            base_mint
            and pool
        ):

            mint_to_pools[
                base_mint
            ].add(pool)

            pool_to_mints[
                pool
            ].add(base_mint)

        account_lengths[
            len(accounts)
        ] += 1

        rows.append({

            "signature":
                get_signature(
                    wrapper,
                    result
                ),

            "slot":
                result.get(
                    "slot"
                ),

            "block_time":
                result.get(
                    "blockTime"
                ),

            "action":
                action,

            "success":
                success,

            "outer_index":
                swap[
                    "outer_index"
                ],

            "account_count":
                len(accounts),

            "pool":
                pool,

            "user":
                user,

            "global_config":
                global_config,

            "base_mint":
                base_mint,

            "quote_mint":
                quote_mint,

            "user_base_token_account":
                user_base,

            "user_quote_token_account":
                user_quote,

            "pool_base_token_account":
                pool_base,

            "pool_quote_token_account":
                pool_quote,

            "protocol_fee_recipient":
                protocol_fee_recipient,

            "protocol_fee_recipient_token_account":
                protocol_fee_account,

            "user_matches_target":
                user == WALLET,

            "base_mint_seen_in_wallet_balances":
                (
                    base_mint
                    in wallet_token_mints
                )
                if wallet_token_mints
                else None,

            "all_accounts":
                json.dumps(
                    accounts,
                    separators=(
                        ",",
                        ":"
                    )
                )
        })


if not rows:

    raise RuntimeError(
        "No PumpSwap BUY/SELL "
        "instructions found."
    )


# ============================================================
# CSV
# ============================================================

with OUT_CSV.open(
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

    writer.writerows(
        rows
    )


# ============================================================
# SUMMARY
# ============================================================

unique_pools = sorted(
    pool_counts.keys()
)

unique_base_mints = sorted(
    mint_to_pools.keys()
)


pool_details = []


for pool in unique_pools:

    mints = sorted(
        pool_to_mints[
            pool
        ]
    )

    pool_details.append({

        "pool":
            pool,

        "transactions":
            pool_counts[
                pool
            ],

        "buys":
            pool_buy_counts[
                pool
            ],

        "sells":
            pool_sell_counts[
                pool
            ],

        "base_mints":
            mints
    })


multi_pool_mints = {

    mint:
        sorted(pools)

    for mint, pools
    in mint_to_pools.items()

    if len(pools) > 1
}


multi_mint_pools = {

    pool:
        sorted(mints)

    for pool, mints
    in pool_to_mints.items()

    if len(mints) > 1
}


summary = {

    "mapped_swaps":
        len(rows),

    "unique_pools":
        len(unique_pools),

    "unique_base_mints":
        len(unique_base_mints),

    "account_count_distribution":
        dict(
            sorted(
                account_lengths.items()
            )
        ),

    "user_account_mismatches":
        wallet_mismatch,

    "base_mint_validation_mismatches":
        mint_mismatch,

    "mints_using_multiple_pools":
        multi_pool_mints,

    "pools_mapped_to_multiple_base_mints":
        multi_mint_pools,

    "pool_details":
        pool_details,

    "mapping_assumption": {

        "account_0":
            "pool",

        "account_1":
            "user",

        "account_2":
            "global_config",

        "account_3":
            "base_mint",

        "account_4":
            "quote_mint",

        "account_5":
            "user_base_token_account",

        "account_6":
            "user_quote_token_account",

        "account_7":
            "pool_base_token_account",

        "account_8":
            "pool_quote_token_account",

        "account_9":
            "protocol_fee_recipient",

        "account_10":
            (
                "protocol_fee_recipient_"
                "token_account"
            )
    }
}


OUT_JSON.write_text(

    json.dumps(
        summary,
        indent=2
    ),

    encoding="utf-8"
)


# ============================================================
# TERMINAL
# ============================================================

print("=" * 60)

print(
    "PHASE 4A.1 — PUMPSWAP POOL MAPPER"
)

print("=" * 60)

print(
    "Mapped swaps      :",
    len(rows)
)

print(
    "Unique pools      :",
    len(unique_pools)
)

print(
    "Unique base mints :",
    len(unique_base_mints)
)

print(
    "User mismatches   :",
    wallet_mismatch
)

print(
    "Mint mismatches   :",
    mint_mismatch
)

print()

print(
    "Account lengths:"
)

for length, count in sorted(
    account_lengths.items()
):

    print(
        f"  {length}: {count}"
    )

print()

print(
    "Mints with >1 pool:",
    len(multi_pool_mints)
)

print(
    "Pools with >1 mint:",
    len(multi_mint_pools)
)

print()

print("Top pools:")

for pool, count in (
    pool_counts.most_common(10)
):

    print(
        f"  {pool}: "
        f"{count} tx "
        f"({pool_buy_counts[pool]} BUY, "
        f"{pool_sell_counts[pool]} SELL)"
    )

print()

print("Output:")
print(" ", OUT_CSV)
print(" ", OUT_JSON)
