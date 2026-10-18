#!/data/data/com.termux/files/usr/bin/bash

WALLET="GRCkjXgEEWQnqveX4oWTNSZroMo4wMwx7EWZFqzZubeB"
RPC="https://api.mainnet-beta.solana.com"

LIMIT=500
DELAY=1.5
MAX_RETRY=8

SIGFILE="signatures.json"
SIGLIST="signature_list.txt"
OUTFILE="transactions.jsonl"
FAILED="failed.txt"

echo "===================================="
echo " Pump Wallet Transaction Downloader"
echo "===================================="
echo "Wallet : $WALLET"
echo "Target : $LIMIT transactions"
echo ""

echo "[1/3] Mengambil daftar signature..."

curl -s "$RPC" \
  -X POST \
  -H "Content-Type: application/json" \
  -d "{
    \"jsonrpc\":\"2.0\",
    \"id\":1,
    \"method\":\"getSignaturesForAddress\",
    \"params\":[
      \"$WALLET\",
      {\"limit\":$LIMIT}
    ]
  }" > "$SIGFILE"

if ! jq -e '.result' "$SIGFILE" >/dev/null 2>&1; then
    echo "ERROR: gagal mengambil signature."
    cat "$SIGFILE"
    exit 1
fi

jq -r '.result[].signature' "$SIGFILE" > "$SIGLIST"

TOTAL=$(wc -l < "$SIGLIST")

echo "Ditemukan $TOTAL signature."
echo ""

touch "$OUTFILE"
touch "$FAILED"

echo "[2/3] Mengambil detail transaksi..."
echo "RPC public sengaja diberi jeda agar tidak kena 429."
echo ""

COUNT=0

while IFS= read -r SIG; do

    COUNT=$((COUNT + 1))

    # Jangan download ulang transaksi yang sudah berhasil
    if grep -q "\"requestedSignature\":\"$SIG\"" "$OUTFILE" 2>/dev/null; then
        echo "[$COUNT/$TOTAL] SKIP (sudah ada)"
        continue
    fi

    RETRY=0
    SUCCESS=0

    while [ "$RETRY" -lt "$MAX_RETRY" ]; do

        RESPONSE=$(curl -s "$RPC" \
          -X POST \
          -H "Content-Type: application/json" \
          -d "{
            \"jsonrpc\":\"2.0\",
            \"id\":1,
            \"method\":\"getTransaction\",
            \"params\":[
              \"$SIG\",
              {
                \"encoding\":\"jsonParsed\",
                \"maxSupportedTransactionVersion\":0
              }
            ]
          }")

        # sukses kalau .result bukan null
        RESULT_TYPE=$(printf '%s' "$RESPONSE" | jq -r \
          'if (.result != null) then "success"
           elif (.error.code == 429) then "rate"
           elif (.error != null) then "error"
           else "null" end' 2>/dev/null)

        if [ "$RESULT_TYPE" = "success" ]; then

            printf '%s' "$RESPONSE" | jq -c \
              --arg sig "$SIG" \
              '. + {requestedSignature:$sig}' >> "$OUTFILE"

            echo "[$COUNT/$TOTAL] OK"

            SUCCESS=1
            break

        elif [ "$RESULT_TYPE" = "rate" ]; then

            RETRY=$((RETRY + 1))
            WAIT=$((RETRY * 5))

            echo "[$COUNT/$TOTAL] 429 → tunggu ${WAIT}s (retry $RETRY/$MAX_RETRY)"

            sleep "$WAIT"

        else

            RETRY=$((RETRY + 1))
            WAIT=$((RETRY * 3))

            echo "[$COUNT/$TOTAL] gagal → retry $RETRY/$MAX_RETRY"

            sleep "$WAIT"
        fi
    done

    if [ "$SUCCESS" -eq 0 ]; then
        echo "$SIG" >> "$FAILED"
        echo "[$COUNT/$TOTAL] FAILED → dicatat di failed.txt"
    fi

    sleep "$DELAY"

done < "$SIGLIST"

echo ""
echo "[3/3] SELESAI"
echo "===================================="

SUCCESS_TOTAL=$(wc -l < "$OUTFILE")
FAILED_TOTAL=$(wc -l < "$FAILED")

echo "Berhasil : $SUCCESS_TOTAL"
echo "Gagal    : $FAILED_TOTAL"
echo ""
echo "File transaksi:"
echo "$PWD/$OUTFILE"
echo "===================================="
