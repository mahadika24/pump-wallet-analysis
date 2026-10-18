import os,csv,json,time,urllib.request,urllib.parse

API="https://pro-api.solscan.io/playground/account/transactions/enhanced"
KEY=os.getenv("SOLSCAN_API_KEY")
PUMP="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BUY=bytes.fromhex("c62e1552b4d9e870")
SELL=bytes.fromhex("33e685a4017f83ad")

MAX_RT=10
MAX_PAGES=5
OUT="data/market_context_4d4c.jsonl"

def b58(s):
    abc="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n=0
    for c in s:n=n*58+abc.index(c)
    x=n.to_bytes((n.bit_length()+7)//8,"big") if n else b""
    return b"\0"*(len(s)-len(s.lstrip("1")))+x

def load(p):
    with open(p,newline="") as f:return list(csv.DictReader(f))

if not KEY:raise SystemExit("SOLSCAN_API_KEY tidak ditemukan")

rt=load("output/roundtrips.csv")
pm=load("output/pumpswap_pools.csv")
poolmap={x["signature"]:x["pool"] for x in pm}

seen=set()
summary=[]

for num,r in enumerate(rt[:MAX_RT],1):
    pool=poolmap.get(r["buy_signature"])
    if not pool:
        print(f"[{num}] pool missing")
        continue

    buy_sig=r["buy_signature"]
    sell_sig=r["sell_signature"]
    bs=int(r["buy_slot"])
    ss=int(r["sell_slot"])

    start=bs-85
    end=ss
    cursor=None
    pages=swaps=0
    retries=0

    print(f"[{num}/{MAX_RT}] {start}->{end}")

    while pages<MAX_PAGES:
        q={
            "address":pool,
            "program[]":PUMP,
            "from_slot":start,
            "to_slot":end,
            "limit":40
        }
        if cursor:q["cursor"]=cursor

        url=API+"?"+urllib.parse.urlencode(q,doseq=True)

        try:
            req=urllib.request.Request(
                url,
                headers={"accept":"application/json","token":KEY}
            )
            with urllib.request.urlopen(req,timeout=30) as z:
                data=json.loads(z.read())
            retries=0

        except Exception as e:
            retries+=1
            print(f"  429/error {retries}/3:",str(e)[:45])

            if retries>=3:
                print("  SKIP roundtrip")
                break

            time.sleep(30*retries)
            continue

        if not data.get("success"):
            print("  API error")
            break

        txs=data.get("data",{}).get("transactions",[])
        if not txs:break

        with open(OUT,"a") as f:
            for tx in txs:
                sigs=tx.get("transaction",{}).get("signatures",[])
                if not sigs:continue

                sig=sigs[0]
                if sig in (buy_sig,sell_sig):continue

                msg=tx.get("transaction",{}).get("message",{})
                signer=""

                for a in msg.get("accountKeys",[]):
                    if a.get("signer"):
                        signer=a.get("pubkey","")
                        break

                for ix in msg.get("instructions",[]):
                    if ix.get("programId")!=PUMP:continue

                    raw=b58(ix.get("data",""))

                    if raw[:8]==BUY:
                        side="BUY"
                    elif raw[:8]==SELL:
                        side="SELL"
                    else:
                        continue

                    key=(num,sig,side)
                    if key in seen:continue
                    seen.add(key)

                    rec={
                        "roundtrip":num,
                        "pool":pool,
                        "slot":int(tx.get("slot",0)),
                        "signature":sig,
                        "signer":signer,
                        "side":side,
                        "distance_from_buy":int(tx.get("slot",0))-bs
                    }

                    if side=="BUY" and len(raw)>=24:
                        rec["quote_in"]=int.from_bytes(raw[8:16],"little")
                        rec["min_base_out"]=int.from_bytes(raw[16:24],"little")

                    if side=="SELL" and len(raw)>=24:
                        rec["base_in"]=int.from_bytes(raw[8:16],"little")
                        rec["min_quote_out"]=int.from_bytes(raw[16:24],"little")

                    f.write(json.dumps(rec)+"\n")
                    swaps+=1

        pages+=1
        cursor=data.get("data",{}).get("next")

        if not cursor:break
        time.sleep(5)

    print(f"  pages={pages} swaps={swaps}")

    summary.append({
        "roundtrip":num,
        "pool":pool,
        "pages":pages,
        "market_swaps":swaps
    })

    time.sleep(20)

with open("output/market_context_4d4c_summary.json","w") as f:
    json.dump({"phase":"4D.4c","results":summary},f,indent=2)

print("DONE")
