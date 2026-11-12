"""
DexHunter alert bot -> Telegram
- Big buy/sell fo9 MIN_ADA (w limites khassa l kol token)
- MCAP + Chart f kol alert
- 🔥 ila kanu 3 big buys wla kter f nafs token f 10 d9ay9
- Wallet: notif kol ±10% mn entry, m3a profit/loss
- Commands f Telegram: /price /wallet /min /entry /help
- Résumé kol lil
"""
import json
import math
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "Africa/Casablanca"))
except Exception:
    TZ = timezone.utc

API = "https://api-us.dexhunterv3.app"
PUB = "https://dhapi.io"
KOIOS = "https://api.koios.rest/api/v1"
KEY = os.environ.get("DEXHUNTER_KEY", "").strip()
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
WALLET = os.environ.get("WALLET_ADDR", "").strip()
MIN_ADA = float(os.environ.get("MIN_ADA", "10000"))
POLL_SEC = int(os.environ.get("POLL_SEC", "30"))
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "75"))
BIG_MOVE = float(os.environ.get("BIG_MOVE_PCT", "10"))
CLUSTER_N = int(os.environ.get("CLUSTER_N", "3"))
CLUSTER_MIN = int(os.environ.get("CLUSTER_MIN", "10"))
STREAK_N = int(os.environ.get("STREAK_N", "5"))
STREAK_GAP_MIN = int(os.environ.get("STREAK_GAP_MIN", "10"))
STREAK_COOLDOWN_MIN = int(os.environ.get("STREAK_COOLDOWN_MIN", "30"))
WALLET_STEP = float(os.environ.get("STEP_PCT", "10"))
WALLET_EVERY = int(os.environ.get("WALLET_EVERY_SEC", "120"))
WALLET_MIN_VALUE = float(os.environ.get("MIN_VALUE_ADA", "20"))
SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", "23"))
TEST = os.environ.get("TEST_MODE", "false").lower() == "true"
COMMANDS = os.environ.get("COMMANDS", "1") != "0"  # 0 = bla commands (ila bot mchtarak)
SEEN_FILE = "seen.json"
STATE_FILE = "state.json"

ADA_IDS = {"", "ada", "lovelace", "000000000000000000000000000000000000000000000000000000006c6f76656c616365"}
H = {"X-Partner-Id": KEY, "Content-Type": "application/json", "Accept": "application/json"}
TS_FIELDS = ("submission_time", "created_at", "createdAt", "update_time", "last_update")
S = requests.Session()


# =============== helpers ===============
def num(v):
    try:
        return float(v)
    except Exception:
        return None


def g(o, *names, default=None):
    for n in names:
        if isinstance(o, dict) and o.get(n) not in (None, ""):
            return o[n]
    return default


def is_ada(tid):
    return (tid or "").lower() in ADA_IDS


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
    v = num(v)
    if v is None:
        return 0.0
    return v / 1_000_000 if v > 1e11 else v


def kfmt(x):
    if x is None:
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{int(x / div * 100 + (1e-6 if x > 0 else -1e-6)) / 100:g}{suf}"
    return f"{x:,.0f}" if abs(x) >= 10 else f"{x:.2g}"


def pfmt(p):
    if p is None:
        return "?"
    if p >= 1:
        return f"{p:,.2f}"
    d = -math.floor(math.log10(p)) + 2 if p > 0 else 2
    return f"{p:.{d}f}"


def pct(x):
    return "?" if x is None else f"{x:+.1f}%"


def now_local():
    return datetime.now(TZ)


# =============== telegram ===============
def tg(text, chat=None):
    try:
        r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   json={"chat_id": chat or TG_CHAT, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}, timeout=20)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram exception:", e)


def tg_updates(offset, timeout):
    try:
        r = S.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                  params={"offset": offset, "timeout": int(timeout), "allowed_updates": '["message"]'},
                  timeout=timeout + 15)
        if r.ok:
            return r.json().get("result", [])
        print("getUpdates error:", r.status_code, r.text[:200])
        time.sleep(min(timeout, 5))
    except Exception as e:
        print("getUpdates exception:", e)
        time.sleep(min(timeout, 5))
    return []


# =============== token data ===============
_names = {}


def token_name(tid):
    if tid in _names:
        return _names[tid]
    name = tid[:10]
    try:
        n2 = bytes.fromhex(tid[56:]).decode("utf-8").strip("\x00")
        if n2 and n2.isprintable():
            name = n2
    except Exception:
        pass
    for url, hdr in ((f"{API}/swap/token/{tid}", H), (f"{PUB}/swap/token/{tid}", {})):
        try:
            r = S.get(url, headers=hdr, timeout=15)
            if r.ok:
                name = g(r.json(), "ticker", "token_ascii", default=name)
                break
        except Exception:
            pass
    _names[tid] = name or tid[:10]
    return _names[tid]


_dex = {}


def dexscreener(tids):
    """{tid: {"url","mcap_ada","price","h1","h24","liq_ada"}} (cache 60s)."""
    out, need = {}, []
    for t in tids:
        c = _dex.get(t)
        if c and time.time() - c[0] < 60:
            out[t] = c[1]
        else:
            need.append(t)
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        best = {}
        try:
            r = S.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=20)
            pairs = (r.json().get("pairs") or []) if r.ok else []
        except Exception as e:
            print("DexScreener error:", e)
            pairs = []
        for p in pairs:
            if p.get("chainId") != "cardano":
                continue
            t = (p.get("baseToken") or {}).get("address")
            if t not in chunk:
                continue
            liq = num((p.get("liquidity") or {}).get("usd")) or 0
            if t not in best or liq > best[t][0]:
                best[t] = (liq, p)
        for t in chunk:
            info = None
            if t in best:
                p = best[t][1]
                pn, pu = num(p.get("priceNative")), num(p.get("priceUsd"))
                mc = num(p.get("marketCap")) or num(p.get("fdv"))
                rate = (pn / pu) if pn and pu else None  # ADA l kol $
                pc = p.get("priceChange") or {}
                info = {"url": p.get("url"), "price": pn,
                        "mcap_ada": mc * rate if mc and rate else None,
                        "h1": num(pc.get("h1")), "h24": num(pc.get("h24")),
                        "liq_ada": num((p.get("liquidity") or {}).get("quote"))}
            _dex[t] = (time.time(), info)
            out[t] = info
    return out


def dex1(tid):
    return dexscreener([tid]).get(tid)


def find_token(q):
    """Qelleb 3la token b ticker (SNEK) wla b CA."""
    q = q.strip().lstrip("$")
    if len(q) >= 56 and all(c in "0123456789abcdefABCDEF" for c in q):
        return q.lower(), token_name(q.lower())
    try:
        r = S.get(f"{PUB}/swap/tokens", params={"query": q}, timeout=15)
        lst = r.json() if r.ok else []
    except Exception:
        lst = []
    if not isinstance(lst, list) or not lst:
        return None, None
    qu = q.upper()
    exact = [t for t in lst if str(t.get("ticker", "")).upper() == qu]
    pick = (sorted(exact, key=lambda t: not t.get("is_verified")) or lst)[0]
    tid = pick.get("token_id")
    _names[tid] = pick.get("ticker") or pick.get("token_ascii") or tid[:10]
    return tid, _names[tid]


_pools = {}


def pool_ada(tid):
    c = _pools.get(tid)
    if c and time.time() - c[0] < 30:
        return c[1]
    total = 0.0
    for url, hdr in ((f"{API}/stats/pools/ADA/{tid}", H), (f"{PUB}/stats/pools/ADA/{tid}", {})):
        try:
            r = S.get(url, headers=hdr, timeout=15)
            if r.ok and isinstance(r.json(), list):
                for p in r.json():
                    if p.get("pool_id"):
                        total += num(p.get("token_1_amount")) or 0
                break
        except Exception:
            pass
    _pools[tid] = (time.time(), total)
    return total or None


def price_move(kind, ada, tok, o):
    v = num(g(o, "price_impact", "priceImpact", "impact"))
    if v is not None:
        return abs(v * 100 if abs(v) < 1 else v)
    R = pool_ada(tok)
    if not R or not ada:
        return None
    if kind == "buy":
        before = max(R - ada, R * 0.05)
        return min(((R / before) ** 2 - 1) * 100, 999)
    before = R + ada
    return (1 - (R / before) ** 2) * 100


def price_ada(tid):
    for url, hdr in ((f"{API}/swap/averagePrice/ADA/{tid}", H), (f"{PUB}/swap/averagePrice/ADA/{tid}", {})):
        try:
            r = S.get(url, headers=hdr, timeout=20)
            if r.ok:
                d = r.json()
                p = num(d.get("price_ba")) if isinstance(d, dict) else None
                if p:
                    return p
        except Exception:
            pass
    return None


# =============== state ===============
def parse_pairs(txt):
    out = {}
    for part in (txt or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            v = num(v)
            if v:
                out[k.strip().upper().lstrip("$")] = v
    return out


ENV_MINS = parse_pairs(os.environ.get("TOKEN_MIN", ""))
ENV_ENTRY = parse_pairs(os.environ.get("ENTRY_MCAP", ""))

state = {"min_over": {}, "default_min": None, "entry_over": {}, "wallet": {},
         "tg_offset": 0, "day": {}, "last_summary": ""}


def load_state():
    try:
        state.update(json.load(open(STATE_FILE)))
    except Exception:
        pass


def save_state():
    try:
        json.dump(state, open(STATE_FILE, "w"))
    except Exception as e:
        print("Save state error:", e)


def default_min():
    return state.get("default_min") or MIN_ADA


def mins():
    m = dict(ENV_MINS)
    for k, v in state["min_over"].items():
        if v:
            m[k] = v
        else:
            m.pop(k, None)
    return m


def entries():
    e = dict(ENV_ENTRY)
    e.update(state["entry_over"])
    return e


def day_rec():
    today = now_local().strftime("%Y-%m-%d")
    d = state.get("day") or {}
    if d.get("date") != today:
        d = {"date": today, "trades": {}, "wallet_start": None}
        state["day"] = d
    return d


# =============== big buys / sells ===============
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
            r = S.post(API + "/swap/globalOrders", headers=H, json=payload(page), timeout=30)
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


clusters = {}     # tid -> deque[(ts, ada)]
cluster_sent = {}  # tid -> ts


def check_cluster(tid, name, ts, ada, info):
    dq = clusters.setdefault(tid, deque())
    dq.append((ts, ada))
    win = CLUSTER_MIN * 60
    while dq and ts - dq[0][0] > win:
        dq.popleft()
    if len(dq) >= CLUSTER_N and time.time() - cluster_sent.get(tid, 0) > 30 * 60:
        cluster_sent[tid] = time.time()
        total = sum(a for _, a in dq)
        lines = [f"\U0001F525 <b>{name}: {len(dq)} big buys f {CLUSTER_MIN} min</b>",
                 f"\U0001F4B5 Total: {kfmt(total)} ADA"]
        if info and info.get("mcap_ada"):
            lines.append(f"\U0001F4CA MCAP: {kfmt(info['mcap_ada'])} ADA")
        lines.append("\U0001F517 " + links(tid, info))
        tg("\n".join(lines))


streak_seen = set()
streak_sent = {}  # tid -> ts


def check_streak(tid, kind, ts, ada, quiet):
    """5 buys (wla 5 sells) mtab3in f nafs token, ay 7jm, bla l3aks binathom."""
    sk = state.setdefault("streaks", {})
    st = sk.get(tid)
    if not st or st["kind"] != kind or ts - st["last"] > STREAK_GAP_MIN * 60:
        st = sk[tid] = {"kind": kind, "n": 0, "ada": 0.0, "first": ts, "last": ts}
    st["n"] += 1
    st["ada"] += ada
    st["last"] = ts
    if st["n"] == STREAK_N and not quiet and time.time() - streak_sent.get(tid, 0) > STREAK_COOLDOWN_MIN * 60:
        streak_sent[tid] = time.time()
        name = token_name(tid)
        info = dex1(tid)
        secs = int(st["last"] - st["first"])
        dur = f"{secs // 60} min" if secs >= 60 else f"{max(secs, 1)} s"
        if kind == "buy":
            head = f"\U0001F680\U0001F7E2 <b>{name}: {st['n']} buys mtab3in</b>"
        else:
            head = f"\U0001F6A8\U0001F534 <b>{name}: {st['n']} sells mtab3in</b>"
        lines = [head, f"\U0001F4B5 Total: {kfmt(st['ada'])} ADA f {dur}"]
        if info and info.get("mcap_ada"):
            lines.append(f"\U0001F4CA MCAP: {kfmt(info['mcap_ada'])} ADA")
        lines.append("\U0001F517 " + links(tid, info))
        tg("\n".join(lines))


def links(tid, info, tx=None):
    out = []
    if info and info.get("url"):
        out.append(f'<a href="{info["url"]}">Chart</a>')
    out.append(f'<a href="https://app.dexhunter.io/swap?tokenIdSell=&tokenIdBuy={tid}">DexHunter</a>')
    if tx and len(str(tx)) == 64:
        out.append(f'<a href="https://cardanoscan.io/transaction/{tx}">Transaction</a>')
    return " · ".join(out)


def process(orders, since, seen, quiet=False):
    alerts = 0
    lim = mins()
    dmin = default_min()
    low = min([dmin] + list(lim.values()))
    events = []
    all_tr = []
    for o in orders:
        tin = g(o, "token_id_in", "tokenIdIn", "token_in", default="")
        tout = g(o, "token_id_out", "tokenIdOut", "token_out", default="")
        if is_ada(tout) and not is_ada(tin):
            kind = "sell"
            ada = ada_amount(g(o, "actual_out_amount", "amount_out", "expected_out_amount", default=0))
        elif is_ada(tin) and not is_ada(tout):
            kind = "buy"
            ada = ada_amount(g(o, "amount_in", "amountIn", default=0))
        else:
            continue
        ts = to_ts(g(o, *TS_FIELDS))
        if ts and ts < since:
            continue
        tx = g(o, "tx_hash", "txHash", "_id", "id", default="")
        uid = str(tx or json.dumps(o, sort_keys=True))
        if uid not in streak_seen:
            streak_seen.add(uid)
            all_tr.append((ts or time.time(), tin if kind == "sell" else tout, kind, ada))
        if ada < low:
            continue
        if uid in seen:
            continue
        tok = tin if kind == "sell" else tout
        name = token_name(tok)
        need = lim.get(str(name).upper().lstrip("$"), dmin)
        if ada < need:
            continue
        seen.add(uid)
        qty = num(g(o, "amount_in", "amountIn") if kind == "sell" else
                  g(o, "actual_out_amount", "amount_out", "expected_out_amount"))
        price = (ada / qty) if qty else None
        impact = price_move(kind, ada, tok, o)
        big = impact is not None and impact >= BIG_MOVE
        info = dex1(tok)
        if kind == "sell":
            icon = "\U0001F7E8" if big else "\U0001F534"
            lines = [f"{icon} <b>BIG SELL {name} {kfmt(ada)} ADA</b>"]
        else:
            icon = "\U0001F7E8" if big else "\U0001F7E2"
            lines = [f"{icon} <b>BIG BUY {name} {kfmt(ada)} ADA</b>"]
        lines.append(f"\U0001F4B0 Price: {pfmt(price)} ADA" if price else "\U0001F4B0 Price: ?")
        lines.append(f"\U0001F4E6 {kfmt(qty)} {name}" if qty else f"\U0001F4E6 ? {name}")
        if info and info.get("mcap_ada"):
            lines.append(f"\U0001F4CA MCAP: {kfmt(info['mcap_ada'])} ADA")
        if impact is not None:
            sign = "-" if kind == "sell" else "+"
            arrow = "\U0001F4C9" if kind == "sell" else "\U0001F4C8"
            lines.append(f"{arrow} Impact: {sign}{abs(impact):.2f}%")
        lines.append("\U0001F517 " + links(tok, info, tx))
        tg("\n".join(lines))
        alerts += 1
        # l'résumé dyal nhar
        d = day_rec()["trades"].setdefault(tok, {"n": name, "buy": 0, "sell": 0, "nb": 0, "ns": 0})
        d["buy" if kind == "buy" else "sell"] += ada
        d["nb" if kind == "buy" else "ns"] += 1
        events.append((ts or time.time(), tok, name, kind, ada, info))
    # b tartib dyal l'wa9t (l9dim 9bel)
    for ts, tok, name, kind, ada, info in sorted(events, key=lambda e: e[0]):
        if kind == "buy":
            check_cluster(tok, name, ts, ada, info)
    # ay buy/sell (ay 7jm) l'streaks
    for ts, tok, kind, ada in sorted(all_tr, key=lambda e: e[0]):
        check_streak(tok, kind, ts, ada, quiet)
    if len(streak_seen) > 50000:
        streak_seen.clear()
    return alerts


# =============== wallet ===============
_stake = [None]
_supply = {}


def koios(path, body):
    try:
        r = S.post(f"{KOIOS}/{path}", json=body, timeout=30)
        return r.json() if r.ok else []
    except Exception as e:
        print("Koios error:", e)
        return []


def holdings():
    if not WALLET:
        return {}
    if not _stake[0]:
        ai = koios("address_info", {"_addresses": [WALLET]})
        _stake[0] = (ai[0].get("stake_address") if ai else None) or ""
    rows = (koios("account_assets", {"_stake_addresses": [_stake[0]]}) if _stake[0]
            else koios("address_assets", {"_addresses": [WALLET]}))
    out = {}
    for a in rows or []:
        tid = a.get("policy_id", "") + a.get("asset_name", "")
        dec = int(a.get("decimals") or 0)
        q = (num(a.get("quantity")) or 0) / (10 ** dec)
        try:
            name = bytes.fromhex(a.get("asset_name", "")).decode("utf-8")
            if not name or not name.isprintable():
                raise ValueError
        except Exception:
            name = tid[:10]
        out[tid] = {"name": name, "qty": q}
    return out


def supply(tid):
    if tid in _supply:
        return _supply[tid]
    rows = koios("asset_info", {"_asset_list": [[tid[:56], tid[56:]]]})
    s = None
    if rows:
        a = rows[0]
        dec = int((a.get("token_registry_metadata") or {}).get("decimals") or 0)
        s = (num(a.get("total_supply")) or 0) / (10 ** dec)
    _supply[tid] = s
    return s


def wallet_snapshot():
    """[{tid,name,qty,price,value,mcap,entry,pnl,gain}]"""
    hold = holdings()
    ent = entries()
    ws = state["wallet"]
    out = []
    for tid, h in hold.items():
        p = price_ada(tid)
        if not p:
            continue
        value = p * h["qty"]
        if value < WALLET_MIN_VALUE:
            continue
        sup = supply(tid)
        if not sup:
            continue
        mcap = p * sup
        st = ws.get(tid)
        man = ent.get(h["name"].upper())
        if st is None:
            st = ws[tid] = {"entry": man or mcap, "level": 0, "src": "manual" if man else "daba"}
        elif man and abs(st["entry"] - man) > 1e-9:
            st.update(entry=man, level=0, src="manual")
        gain = (mcap / st["entry"] - 1) * 100
        cost = h["qty"] * st["entry"] / sup
        out.append({"tid": tid, "name": h["name"], "qty": h["qty"], "price": p, "value": value,
                    "mcap": mcap, "entry": st["entry"], "pnl": value - cost, "gain": gain})
    for tid in list(ws):
        if tid not in hold:
            del ws[tid]
    return out


def check_wallet(first):
    if not WALLET:
        return
    snap = wallet_snapshot()
    d = day_rec()
    if d.get("wallet_start") is None:
        d["wallet_start"] = sum(x["value"] for x in snap)
    for x in snap:
        st = state["wallet"][x["tid"]]
        old = level = st["level"]
        while x["gain"] >= (level + 1) * WALLET_STEP:
            level += 1
        while x["gain"] <= (level - 1) * WALLET_STEP:
            level -= 1
        st["level"] = level
        if level != old and level != 0 and not first:
            head = (f"\U0001F680 <b>{x['name']} +{x['gain']:.0f}%</b>" if x["gain"] > 0
                    else f"\U0001F53B <b>{x['name']} {x['gain']:.0f}%</b>")
            pl = (f"\U0001F7E2 Profit: +{kfmt(x['pnl'])} ADA" if x["pnl"] >= 0
                  else f"\U0001F534 Loss: -{kfmt(-x['pnl'])} ADA")
            tg(f"{head}\n\U0001F4CA MCAP: {kfmt(x['entry'])} ➜ {kfmt(x['mcap'])} ADA\n"
               f"\U0001F4BC 3ndk: {kfmt(x['qty'])} {x['name']} ≈ {kfmt(x['value'])} ADA\n{pl}\n"
               f"\U0001F517 {links(x['tid'], dex1(x['tid']))}")


def wallet_text():
    if not WALLET:
        return "Ma kaynach adresse dyal wallet (WALLET_ADDR)."
    snap = sorted(wallet_snapshot(), key=lambda x: -x["value"])
    if not snap:
        return "Ma l9it 7tta token f wallet."
    lines = ["\U0001F4BC <b>Wallet dyalk</b>"]
    tv = tp = 0
    for x in snap:
        tv += x["value"]
        tp += x["pnl"]
        icon = "\U0001F7E2" if x["pnl"] >= 0 else "\U0001F534"
        lines.append(f"{icon} <b>{x['name']}</b>: {kfmt(x['value'])} ADA ({pct(x['gain'])}) "
                     f"{'+' if x['pnl'] >= 0 else '-'}{kfmt(abs(x['pnl']))} ADA")
    lines.append(f"\n\U0001F4B0 Total: <b>{kfmt(tv)} ADA</b>")
    lines.append(f"{'🟢 Profit' if tp >= 0 else '🔴 Loss'}: {'+' if tp >= 0 else '-'}{kfmt(abs(tp))} ADA")
    lines.append("<i>PnL mn l'entry (ila ma 3titinich entry, mn nhar bda l'bot kaychoufo)</i>")
    return "\n".join(lines)


# =============== commands ===============
HELP = ("\U0001F916 <b>Commands</b>\n"
        "/price SNEK — prix, MCAP, 1h/24h, chart\n"
        "/wallet — ch7al 3ndk w profit/loss\n"
        "/min — chouf l'limites\n"
        "/min 15000 — limite 3adiya l ga3 tokens\n"
        "/min SNEK 30000 — limite khassa l token\n"
        "/min SNEK off — 7ayd limite khassa\n"
        "/entry SOLADA 2070000 — MCAP fach chriti (l wallet)\n"
        "/summary — résumé dyal nhar daba")


def cmd_price(arg):
    if not arg:
        return "Ktb b7al: /price SNEK"
    tid, name = find_token(arg)
    if not tid:
        return f"Ma l9itch token smito {arg}."
    info = dex1(tid) or {}
    p = info.get("price") or price_ada(tid)
    lines = [f"\U0001F4B0 <b>{name}</b>",
             f"Price: {pfmt(p)} ADA",
             f"\U0001F4CA MCAP: {kfmt(info.get('mcap_ada'))} ADA",
             f"⏱ 1h: {pct(info.get('h1'))} · 24h: {pct(info.get('h24'))}"]
    if info.get("liq_ada"):
        lines.append(f"\U0001F4A7 Liquidity: {kfmt(info['liq_ada'])} ADA")
    lines.append(f"\U0001F4C4 <code>{tid}</code>")
    lines.append("\U0001F517 " + links(tid, info))
    return "\n".join(lines)


def cmd_min(args):
    if not args:
        m = mins()
        lines = [f"⚙️ <b>Limites</b>", f"Ga3 tokens: {kfmt(default_min())} ADA"]
        for k, v in sorted(m.items(), key=lambda kv: kv[1]):
            lines.append(f"• {k}: {kfmt(v)} ADA")
        return "\n".join(lines)
    if len(args) == 1:
        v = num(args[0].replace("k", "000").replace("K", "000"))
        if not v:
            return "Ktb b7al: /min 15000 wla /min SNEK 30000"
        state["default_min"] = v
        save_state()
        return f"✅ Limite dyal ga3 tokens daba: {kfmt(v)} ADA"
    tok = args[0].upper().lstrip("$")
    if args[1].lower() in ("off", "0", "remove", "7ayd"):
        state["min_over"][tok] = 0
        save_state()
        return f"✅ {tok} rja3 l limite 3adiya ({kfmt(default_min())} ADA)"
    v = num(args[1].replace("k", "000").replace("K", "000"))
    if not v:
        return "Ktb b7al: /min SNEK 30000"
    state["min_over"][tok] = v
    save_state()
    return f"✅ {tok}: alert ghir fo9 {kfmt(v)} ADA"


def cmd_entry(args):
    if len(args) < 2:
        return "Ktb b7al: /entry SOLADA 2070000 (MCAP b ADA fach chriti)"
    v = num(args[1].replace("m", "e6").replace("M", "e6").replace("k", "e3").replace("K", "e3"))
    if not v:
        return "Ktb b7al: /entry SOLADA 2070000"
    tok = args[0].upper().lstrip("$")
    state["entry_over"][tok] = v
    save_state()
    return f"✅ Entry dyal {tok}: {kfmt(v)} MCAP"


def handle(text):
    parts = text.strip().split()
    if not parts:
        return None
    c = parts[0].split("@")[0].lower()
    args = parts[1:]
    if c == "/price":
        return cmd_price(" ".join(args))
    if c == "/wallet":
        return wallet_text()
    if c == "/min":
        return cmd_min(args)
    if c == "/entry":
        return cmd_entry(args)
    if c == "/summary":
        return summary_text()
    if c in ("/help", "/start"):
        return HELP
    return None


def poll_commands(wait):
    end = time.time() + wait
    while True:
        left = end - time.time()
        if left <= 1:
            return
        ups = tg_updates(state.get("tg_offset", 0), min(left, 25))
        for u in ups:
            state["tg_offset"] = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != TG_CHAT:
                continue  # ghir chat dyalk
            txt = m.get("text") or ""
            if not txt.startswith("/"):
                continue
            try:
                ans = handle(txt)
            except Exception as e:
                ans = f"Wa9e3 chi mochkil: {e}"
            if ans:
                tg(ans)
        if ups:
            save_state()


# =============== résumé dyal nhar ===============
def summary_text():
    d = day_rec()
    tr = d.get("trades", {})
    lines = [f"\U0001F4CA <b>Résumé dyal nhar {now_local():%d/%m}</b>"]
    nb = sum(x["nb"] for x in tr.values())
    ns = sum(x["ns"] for x in tr.values())
    vb = sum(x["buy"] for x in tr.values())
    vs = sum(x["sell"] for x in tr.values())
    lines.append(f"\U0001F433 Big trades: \U0001F7E2 {nb} buys ({kfmt(vb)} ADA) · "
                 f"\U0001F534 {ns} sells ({kfmt(vs)} ADA)")
    if tr:
        top = sorted(tr.items(), key=lambda kv: -(kv[1]["buy"] - kv[1]["sell"]))
        lines.append("\n\U0001F525 <b>Akbar chra (net)</b>")
        for tid, x in top[:3]:
            net = x["buy"] - x["sell"]
            if net <= 0:
                break
            lines.append(f"• {x['n']}: +{kfmt(net)} ADA ({x['nb']} buys)")
        dump = [kv for kv in reversed(top) if kv[1]["sell"] - kv[1]["buy"] > 0][:3]
        if dump:
            lines.append("\n\U0001F9CA <b>Akbar bi3 (net)</b>")
            for tid, x in dump:
                lines.append(f"• {x['n']}: -{kfmt(x['sell'] - x['buy'])} ADA ({x['ns']} sells)")
    # tokens li tl3o / hbto (24h)
    cand = set(tr)
    try:
        r = S.post("https://charts.dhapi.io/trending", json={}, timeout=20)
        for t in (r.json() if r.ok else []):
            if t.get("token_id"):
                cand.add(t["token_id"])
    except Exception:
        pass
    for tid in list(state["wallet"]):
        cand.add(tid)
    info = dexscreener(list(cand))
    ch = [(tid, i["h24"]) for tid, i in info.items() if i and i.get("h24") is not None]
    if ch:
        ch.sort(key=lambda x: -x[1])
        lines.append("\n\U0001F4C8 <b>Tl3o (24h)</b>")
        for tid, c in ch[:3]:
            if c <= 0:
                break
            lines.append(f"• {token_name(tid)}: {pct(c)} · MCAP {kfmt(info[tid].get('mcap_ada'))}")
        lines.append("\U0001F4C9 <b>Hbto (24h)</b>")
        for tid, c in list(reversed(ch))[:3]:
            if c >= 0:
                break
            lines.append(f"• {token_name(tid)}: {pct(c)} · MCAP {kfmt(info[tid].get('mcap_ada'))}")
    if WALLET:
        snap = wallet_snapshot()
        tv = sum(x["value"] for x in snap)
        tp = sum(x["pnl"] for x in snap)
        ws = d.get("wallet_start")
        lines.append("\n\U0001F4BC <b>Wallet</b>")
        lines.append(f"Total: {kfmt(tv)} ADA" + (f" ({'+' if tv >= ws else '-'}{kfmt(abs(tv - ws))} ADA lyoum)" if ws else ""))
        lines.append(f"{'🟢 Profit' if tp >= 0 else '🔴 Loss'} mn l'entry: {'+' if tp >= 0 else '-'}{kfmt(abs(tp))} ADA")
        for x in sorted(snap, key=lambda x: -abs(x["pnl"]))[:5]:
            lines.append(f"• {x['name']}: {pct(x['gain'])} ({'+' if x['pnl'] >= 0 else '-'}{kfmt(abs(x['pnl']))} ADA)")
    return "\n".join(lines)


def maybe_summary():
    n = now_local()
    key = n.strftime("%Y-%m-%d")
    if n.hour >= SUMMARY_HOUR and state.get("last_summary") != key:
        state["last_summary"] = key
        try:
            tg(summary_text())
        except Exception as e:
            print("Summary error:", e)
        save_state()


# =============== main ===============
def main():
    if not (KEY and TG_TOKEN and TG_CHAT):
        print("Khass DEXHUNTER_KEY, TG_TOKEN w TG_CHAT_ID f GitHub Secrets.")
        sys.exit(1)
    load_state()
    state["streaks"] = {}  # kat3awd tbna mn l'lookback
    seen = set()
    try:
        seen = set(json.load(open(SEEN_FILE)))
    except Exception:
        pass

    start = time.time()
    since = start - LOOKBACK_MIN * 60
    end = start + RUN_MINUTES * 60
    total = checks = 0
    last_wallet = 0
    while True:
        loop_start = time.time()
        try:
            orders = fetch_since(since)
            a = process(orders, since, seen, quiet=(checks == 0))
        except Exception as e:
            print("Process error:", e)
            orders, a = [], 0
        total += a
        checks += 1
        if time.time() - last_wallet >= WALLET_EVERY:
            try:
                check_wallet(first=(last_wallet == 0 and checks == 1 and not state["wallet"]))
            except Exception as e:
                print("Wallet error:", e)
            last_wallet = time.time()
        maybe_summary()
        if checks == 1:
            print(f"Check 1: {len(orders)} ordres, {a} alerte(s), wallet {len(state['wallet'])} tokens")
            if TEST:
                tg(f"✅ Bot khddam: alerts fo9 {kfmt(default_min())} ADA, \U0001F525 clusters, "
                   f"wallet ({len(state['wallet'])} tokens), résumé f {SUMMARY_HOUR}:00."
                   + ("\nKtb /help bach tchouf l'commands." if COMMANDS else ""))
        try:
            json.dump(list(seen)[-5000:], open(SEEN_FILE, "w"))
        except Exception:
            pass
        save_state()
        since = loop_start - 600
        if RUN_MINUTES <= 0 or time.time() + POLL_SEC > end:
            break
        wait = max(1, POLL_SEC - (time.time() - loop_start))
        if COMMANDS:
            poll_commands(wait)
        else:
            time.sleep(wait)
    save_state()
    print(f"Sala: {checks} checks, {total} alerte(s)")


if __name__ == "__main__":
    main()
