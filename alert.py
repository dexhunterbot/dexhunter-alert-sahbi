"""
DexHunter big buy/sell alert -> Telegram (real-time)
Kaychouf DexHunter kol POLL_SEC tanya, w ila chi wa7ed ba3 token b > MIN_ADA ADA, kaysift message f Telegram.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

API = "https://api-us.dexhunterv3.app"
KEY = os.environ.get("DEXHUNTER_KEY", "").strip()
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
MIN_ADA = float(os.environ.get("MIN_ADA", "5000"))
POLL_SEC = int(os.environ.get("POLL_SEC", "30"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))  # 0 = check wa7ed w ykhrej
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "75"))
TEST = os.environ.get("TEST_MODE", "false").lower() == "true"
SEEN_FILE = "seen.json"

ADA_IDS = {"", "ada", "lovelace", "000000000000000000000000000000000000000000000000000000006c6f76656c616365"}
H = {"X-Partner-Id": KEY, "Content-Type": "application/json", "Accept": "application/json"}
TS_FIELDS = ("submission_time", "created_at", "createdAt", "update_time", "last_update")


def tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)


def is_ada(tid):
    return (tid or "").lower() in ADA_IDS


def g(o, *names, default=None):
    for n in names:
        if isinstance(o, dict) and o.get(n) not in (None, ""):
            return o[n]
    return default


def to_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def ada_amount(v):
    try:
        v = float(v)
    except Exception:
        return 0.0
    return v / 1_000_000 if v > 1e11 else v


def payload(page):
    return {"page": page, "perPage": 100,
            "filters": [{"filterType": "STATUS", "values": ["COMPLETE"]}],
            "orderSorts": "STARTTIME", "sortDirection": "DESC"}


def extract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("orders", "data", "items", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def fetch_since(since_ts, max_pages=15):
    out = []
    for page in range(max_pages):
        try:
            r = requests.post(API + "/swap/globalOrders", headers=H, json=payload(page), timeout=30)
        except Exception as e:
            print("Request error:", e)
            break
        if not r.ok:
            print(f"globalOrders page {page}: HTTP {r.status_code} {r.text[:300]}")
            break
        lst = extract_list(r.json())
        if not lst:
            break
        out.extend(lst)
        tss = [t for t in (to_ts(g(x, *TS_FIELDS)) for x in lst) if t]
        if not tss or min(tss) < since_ts:
            break
        time.sleep(0.3)
    return out


DEBUG_DONE = [False]


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def kfmt(x):
    if x is None:
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            v = int(x / div * 10) / 10
            return f"{v:g}{suf}"
    return f"{x:,.0f}" if x >= 10 else f"{x:g}"


def pfmt(p):
    if p >= 1:
        return f"{p:,.2f}"
    d = -math.floor(math.log10(p)) + 2 if p > 0 else 2
    return f"{p:.{d}f}"


_names = {}


def token_name(tid):
    if tid in _names:
        return _names[tid]
    name = tid[:10]
    try:
        name = bytes.fromhex(tid[56:]).decode("utf-8", "ignore").strip("\x00") or name
    except Exception:
        pass
    try:
        r = requests.get(f"{API}/swap/token/{tid}", headers=H, timeout=15)
        if r.ok:
            name = g(r.json(), "ticker", "token_ascii", default=name)
    except Exception:
        pass
    name = name or tid[:10]
    _names[tid] = name
    return name


def process(orders, since, seen):
    alerts = 0
    for o in orders:
        tin = g(o, "token_id_in", "tokenIdIn", "token_in", default="")
        tout = g(o, "token_id_out", "tokenIdOut", "token_out", default="")
        if is_ada(tout) and not is_ada(tin):
            kind = "sell"  # token -> ADA
            ada = ada_amount(g(o, "actual_out_amount", "amount_out", "expected_out_amount", default=0))
        elif is_ada(tin) and not is_ada(tout):
            kind = "buy"   # ADA -> token
            ada = ada_amount(g(o, "amount_in", "amountIn", default=0))
        else:
            continue
        ts = to_ts(g(o, *TS_FIELDS))
        if ts and ts < since:
            continue
        if ada < MIN_ADA:
            continue
        tx = g(o, "tx_hash", "txHash", "_id", "id", default="")
        uid = str(tx or json.dumps(o, sort_keys=True))
        if uid in seen:
            continue
        seen.add(uid)
        tok = tin if kind == "sell" else tout
        qty = num(g(o, "amount_in", "amountIn") if kind == "sell" else
                  g(o, "actual_out_amount", "amount_out", "expected_out_amount"))
        name = token_name(tok)
        price = (ada / qty) if qty else None
        impact = num(g(o, "price_impact", "priceImpact", "impact"))
        if impact is not None and abs(impact) < 1:
            impact *= 100  # ila ja b 0.023 -> 2.3%
        if kind == "sell":
            lines = [f"\U0001F534 <b>BIG SELL {name} {kfmt(ada)} ADA</b>"]
        else:
            lines = [f"\U0001F7E2 <b>BIG BUY {name} {kfmt(ada)} ADA</b>"]
        lines.append(f"\U0001F4B0 Price: {pfmt(price)} ADA" if price else "\U0001F4B0 Price: ?")
        lines.append(f"\U0001F4E6 {kfmt(qty)} {name}" if qty else f"\U0001F4E6 ? {name}")
        if impact is not None:
            sign = "-" if kind == "sell" else "+"
            arrow = "\U0001F4C9" if kind == "sell" else "\U0001F4C8"
            lines.append(f"{arrow} Impact: {sign}{abs(impact):.2f}%")
        links = [f'<a href="https://app.dexhunter.io/swap?tokenIdSell=&tokenIdBuy={tok}">DexHunter</a>']
        if len(str(tx)) == 64:
            links.append(f'<a href="https://cardanoscan.io/transaction/{tx}">Transaction</a>')
        lines.append("\U0001F517 " + " · ".join(links))
        if not DEBUG_DONE[0]:
            DEBUG_DONE[0] = True
            print("DEBUG ORDER:", json.dumps(o)[:1500])
        tg("\n".join(lines))
        alerts += 1
    return alerts


def save(seen):
    try:
        json.dump(list(seen)[-5000:], open(SEEN_FILE, "w"))
    except Exception as e:
        print("Save error:", e)


def main():
    if not (KEY and TG_TOKEN and TG_CHAT):
        print("Khass DEXHUNTER_KEY, TG_TOKEN w TG_CHAT_ID f GitHub Secrets.")
        sys.exit(1)

    seen = set()
    if os.path.exists(SEEN_FILE):
        try:
            seen = set(json.load(open(SEEN_FILE)))
        except Exception:
            pass

    start = time.time()
    since = start - LOOKBACK_MIN * 60
    end = start + RUN_MINUTES * 60
    total_alerts = 0
    checks = 0

    while True:
        loop_start = time.time()
        orders = fetch_since(since)
        a = process(orders, since, seen)
        total_alerts += a
        checks += 1
        if checks == 1:
            print(f"Check 1: {len(orders)} ordres, {a} alerte(s)")
            if TEST:
                tg(f"✅ Test: robot khddam (real-time, kol {POLL_SEC}s). "
                   f"Jbed {len(orders)} ordres, l9a {a} buy/sell > {MIN_ADA:,.0f} ADA.")
        if a:
            print(f"{datetime.now(timezone.utc):%H:%M:%S} sifet {a} alerte(s)")
            save(seen)
        # check jay: ghir mn 10 dqayq lor (bach ma ntl3ouch bzaf dyal pages)
        since = loop_start - 600
        if RUN_MINUTES <= 0 or time.time() + POLL_SEC > end:
            break
        time.sleep(max(1, POLL_SEC - (time.time() - loop_start)))

    save(seen)
    print(f"Sala: {checks} checks, {total_alerts} alerte(s)")


if __name__ == "__main__":
    main()
