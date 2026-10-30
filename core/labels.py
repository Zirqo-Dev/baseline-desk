"""Outcome labels (BL-26), logging only: what happened to a token +1 h, +6 h and +24 h after first sight.

Which tokens: every desk lead whose check got past dex_skip() (WATCH, PASS, non-gate SKIP, GoPlus-side RETRY
and fetch_error_x3: the rows BL-25 gives trade keys). Gate kills, baseline rows and trades get no labels.
No rule, config value, verdict or exit reads them (DECISIONS 2026-10-30, BL-26).

Sight: the tick passes each such check (address, symbol, first_seen, first_price). first_seen is the watchlist's
first_seen, else the check time; first_price is the facts() price (pick_pair: the deepest Base pair) only when this
check IS the first sight (first_seen = now), else "na" (a token already watched at deploy, or re-added later), and
"na" when missing. A token already pending keeps its first sight; one seen again after it left (a new watch cycle
after its 24 h skip) starts a new set, told apart by first_seen.

Labels: due at first_seen + 1 h / 6 h / 24 h, taken on the first tick at or after that (age_min = the real age).
DexScreener /tokens/v1/base/<a,b,...> answers up to BATCH addresses in one request but only ONE pair per token
(the deepest; probe 2026-10-30). So price and liq come from pick_pair() on that answer, the same rule as first_price,
and pair_b1 pair_s1 pair_b24 pair_s24 are that one pair's counts (BL-25's trade_facts() on that pair, quote side
mirrored), not BL-25's sums over every Base pair. gone=1 when the answer has no Base pair for the token.
At most MAX_CALLS requests per tick, oldest due first; the rest wait. A failed request leaves its labels due.

State key "labels" = {"pending": {address: {symbol, first_seen, first_price, done: [hours]}}}. An entry leaves after
its +24 h label or at first_seen + KEEP_H, whichever comes first, so state.json stays bounded.
Rows: data/daily/<day>/outcomes.csv (FIELDS). In the tick it runs after the desk and baseline steps in its own try.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adapters import base
from core import config
from core.book import _append, day, load_state, save_key, ts

HORIZONS = [1, 6, 24]  # hours after first sight
KEEP_H = 26            # an entry never stays longer than this
BATCH = 30             # addresses per DexScreener request
MAX_CALLS = 2          # label requests per tick
OUTCOMES = "data/daily/{day}/outcomes.csv"
FIELDS = ["ts", "symbol", "address", "first_seen", "horizon", "age_min", "first_price", "price", "chg_pct", "liq",
          "pair_b1", "pair_s1", "pair_b24", "pair_s24", "dex", "gone"]
NA = "na"


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf") else None


def sight(address, facts, first_seen, now=None):
    """A check past the gate, as the tick hands it over (plain data, no request). now: the check time; a check
    that is not the first sight has no first price."""
    price = _num(facts.get("price")) if now is None or first_seen == ts(now) else None
    return {"address": address.lower(), "symbol": facts.get("symbol") or "", "first_seen": first_seen,
            "first_price": price if price is not None else NA}


# ---- state ----
def load(root):
    s = load_state(Path(root) / config.STATE).get("labels") or {}
    s.setdefault("pending", {})
    return s


def save(root, s):
    save_key(Path(root) / config.STATE, "labels", s)


def add(s, sights):
    for x in sights:
        if x["address"] not in s["pending"]:
            s["pending"][x["address"]] = {"symbol": x["symbol"], "first_seen": x["first_seen"],
                                          "first_price": x["first_price"], "done": []}


def prune(s, now):
    """Drop entries with every horizon done or older than KEEP_H."""
    s["pending"] = {a: p for a, p in s["pending"].items()
                    if len(p["done"]) < len(HORIZONS) and now < _t(p["first_seen"]) + timedelta(hours=KEEP_H)}


def due(s, now):
    """[(due time, address, hours)] for every label due now, oldest first."""
    out = []
    for a, p in s["pending"].items():
        for h in HORIZONS:
            at = _t(p["first_seen"]) + timedelta(hours=h)
            if h not in p["done"] and now >= at:
                out.append((at, a, h))
    return sorted(out)


# ---- one label ----
def row(now, address, p, h, pairs):
    """One outcomes.csv row from the batch answer's pairs for this token ([] = gone)."""
    out = {"ts": ts(now), "symbol": p["symbol"], "address": address, "first_seen": p["first_seen"],
           "horizon": f"{h}h", "age_min": round((now - _t(p["first_seen"])).total_seconds() / 60),
           "first_price": p["first_price"]}
    pair, side = base.pick_pair(pairs, address)
    if not pair:
        return dict(out, **dict.fromkeys(FIELDS[7:-1], NA), gone=1)
    price = _num(base.dex_facts(pair, side, now).get("price"))
    t = base.trade_facts([pair], pair, address)  # this one pair, quote side mirrored
    first = _num(p["first_price"])
    chg = round((price / first - 1) * 100, 2) if price is not None and first else NA
    return dict(out, price=price if price is not None else NA, chg_pct=chg, liq=t["liq"],
                pair_b1=t["b1"], pair_s1=t["s1"], pair_b24=t["b24"], pair_s24=t["s24"], dex=t["dex"], gone=0)


def run(root, now, sights, client):
    """Add the tick's sights, take the labels due (<= MAX_CALLS requests), write rows, save. Returns the counts."""
    root, s = Path(root), load(root)
    add(s, sights)
    prune(s, now)
    todo = due(s, now)
    order = list(dict.fromkeys(a for _, a, _ in todo))[:BATCH * MAX_CALLS]
    n = {"due": len(todo), "done": 0, "calls": 0}
    for i in range(0, len(order), BATCH):
        chunk = order[i:i + BATCH]
        n["calls"] += 1
        body = base.token_batch(chunk, client)
        if not isinstance(body, list):  # failed request: these stay due for the next tick
            continue
        for a in chunk:
            p = s["pending"][a]
            pairs = [x for x in body if isinstance(x, dict) and a in (
                base._lower(base._dict(x.get("baseToken")).get("address")),
                base._lower(base._dict(x.get("quoteToken")).get("address")))]
            for _, a2, h in todo:
                if a2 == a:
                    _append(root / OUTCOMES.format(day=day(now)), FIELDS, row(now, a, p, h, pairs))
                    p["done"].append(h)
                    n["done"] += 1
    prune(s, now)
    save(root, s)
    return n
