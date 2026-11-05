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

State key "labels" = {"pending": {address: {symbol, first_seen, first_price, done: [hours], sight_pair, sight_qliq}}}
(sight_*: BL-30). An entry leaves after its +24 h label or at first_seen + KEEP_H, whichever comes first, so
state.json stays bounded.
Rows: data/daily/<day>/outcomes.csv (FIELDS). In the tick it runs after the desk and baseline steps in its own try.
BL-28: last column qliq, the USD value of the deepest pair's other side (ETH, WETH, USDC...; base.quote_liq()), "na" when
unknown. A file is never mixed: rows go out with the header the day's file already has, so a file started before BL-28
keeps the old columns to the end of its UTC day and qliq starts with the next day's file.
BL-30 (the owner, 2026-11-05), logging only, no rule reads it: five more columns after qliq, from the first file
started after BL-30 (same header rule): sight_pair (the pair facts() chose at first sight), label_pair (the pair the
other label columns describe), sight_qliq (qliq at first sight), best_qliq (the highest qliq over every Base pair of the
token in the batch answer, quote_liq() each; qliq stays the deepest pair's, BL-28) and dead (see dead(), on best_qliq;
its two numbers live in DEAD, nowhere else). The batch answer holds ONE pair per token (probe 2026-11-05: BRETT and DEGEN
30 Base pairs on /token-pairs/v1, 1 each in the batch), so today best_qliq = qliq unless another token's pair in the same
answer also holds this token. sight_pair and
sight_qliq are taken only when the check IS the first sight, like first_price, so never from a later re-check; "na"
otherwise (and for entries already pending at deploy). Why: the batch answer gives the deepest pair, so a leftover dust
pair can look alive with gone=0.
"""
import csv
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
          "pair_b1", "pair_s1", "pair_b24", "pair_s24", "dex", "gone", "qliq",  # qliq: BL-28
          "sight_pair", "label_pair", "sight_qliq", "best_qliq", "dead"]  # BL-30
NA = "na"
# BL-30 (the owner, 2026-11-05): the dead flag's two numbers, the one place they live (the replay harness imports dead()).
DEAD = {"floor_usd": 100, "share": 0.10}


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf") else None


def _q(x):
    """A qliq as a number, or None when unknown ("na", missing, odd). 0 is a real value (BL-28), never missing."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v >= 0 and v != float("inf") else None


def _addr(x):
    return x.lower() if isinstance(x, str) and x else NA


def dead(gone, label_qliq, sight_qliq):
    """BL-30 dead flag (the owner, 2026-11-05), logging only: 1, 0 or "na".

    1 when gone = 1, or when the label qliq is known and below DEAD floor_usd (whatever the sight qliq; 0 is known).
    "na" only when the result depends on a missing number: label qliq "na", or label qliq >= floor_usd and sight qliq
    "na". Else 1 when label qliq < max(floor_usd, share x sight qliq), 0 otherwise (the exact edge is alive).
    """
    if str(gone) == "1":
        return 1
    q = _q(label_qliq)
    if q is None:
        return NA
    if q < DEAD["floor_usd"]:
        return 1
    sq = _q(sight_qliq)
    if sq is None:
        return NA
    return 1 if q < max(DEAD["floor_usd"], DEAD["share"] * sq) else 0


def best_qliq(pairs, address):
    """BL-30 (the owner, 2026-11-05): the best real depth a holder could sell into: the highest quote_liq() over every
    Base pair of the token in the answer already fetched, "na" when none is known (a real 0 counts). No request."""
    qs = [q for p in pairs or [] if isinstance(p, dict) and p.get("chainId") == "base"
          for q in [base.quote_liq(p, address)] if q != NA]
    return max(qs) if qs else NA


def sight(address, facts, first_seen, now=None):
    """A check past the gate, as the tick hands it over (plain data, no request). now: the check time; a check
    that is not the first sight has no first price, sight pair or sight qliq (BL-30: never a later re-check)."""
    first = now is None or first_seen == ts(now)
    price = _num(facts.get("price")) if first else None
    trade = facts.get("trade") if isinstance(facts.get("trade"), dict) else {}
    pair = _addr(facts.get("pair")) if first else NA
    q = trade.get("qliq", NA) if first else NA
    return {"address": address.lower(), "symbol": facts.get("symbol") or "", "first_seen": first_seen,
            "first_price": price if price is not None else NA, "sight_pair": pair,
            "sight_qliq": q if _q(q) is not None else NA}


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
                                          "first_price": x["first_price"], "done": [],
                                          "sight_pair": x.get("sight_pair", NA), "sight_qliq": x.get("sight_qliq", NA)}


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
def header(path):
    """The columns of an existing outcomes.csv (a file never mixes column counts, BL-28), else FIELDS."""
    if not path.exists() or path.stat().st_size == 0:
        return FIELDS
    with path.open(newline="") as fh:
        return next(csv.reader(fh), None) or FIELDS


def row(now, address, p, h, pairs):
    """One outcomes.csv row from the batch answer's pairs for this token ([] = gone)."""
    out = {"ts": ts(now), "symbol": p["symbol"], "address": address, "first_seen": p["first_seen"],
           "horizon": f"{h}h", "age_min": round((now - _t(p["first_seen"])).total_seconds() / 60),
           "first_price": p["first_price"]}
    sq = p.get("sight_qliq", NA)  # BL-30; an entry pending since before BL-30 has none
    out.update(sight_pair=p.get("sight_pair", NA), sight_qliq=sq)
    pair, side = base.pick_pair(pairs, address)
    if not pair:
        rest = {k: NA for k in FIELDS[7:] if k not in out and k not in ("gone", "dead")}  # label_pair, best_qliq too
        return dict(out, **rest, gone=1, dead=dead(1, NA, sq))
    best = best_qliq(pairs, address)
    price = _num(base.dex_facts(pair, side, now).get("price"))
    t = base.trade_facts([pair], pair, address)  # this one pair, quote side mirrored
    first = _num(p["first_price"])
    chg = round((price / first - 1) * 100, 2) if price is not None and first else NA
    return dict(out, price=price if price is not None else NA, chg_pct=chg, liq=t["liq"],
                pair_b1=t["b1"], pair_s1=t["s1"], pair_b24=t["b24"], pair_s24=t["s24"], dex=t["dex"], gone=0,
                qliq=t["qliq"], label_pair=_addr(pair.get("pairAddress")), best_qliq=best, dead=dead(0, best, sq))


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
                    path = root / OUTCOMES.format(day=day(now))
                    _append(path, header(path), row(now, a, p, h, pairs))
                    p["done"].append(h)
                    n["done"] += 1
    prune(s, now)
    save(root, s)
    return n
