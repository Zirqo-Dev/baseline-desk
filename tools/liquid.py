"""Liquid Base token survey (BL-39 part 1, also BL-38's universe): counts and coverage only, evidence, by hand.

    python -m tools.liquid --cut 2026-11-08 --write     fetch the universe + daily candles, write backtest/liquid/
    python -m tools.liquid --cut 2026-11-08             the same fetch, print the survey, write nothing
    python -m tools.liquid --cut 2026-11-08 --survey    print the survey from the committed files (no request)

Never on the tick path; core/ never imports tools/ and never reads backtest/. No return of any kind is computed here
(no P&L, no price change, no forward return by any bucket, no backtest): the only price read is the last daily close
before CUT, as asset_kind()'s price input (last_close()). Volume is the only point-in-time liquidity number.

CUT (the owner, Nov 9): nothing at or after CUT is kept or read. Daily candles are asked with before_timestamp=CUT and
every candle whose day does not END by CUT (t + 1 day > CUT) is dropped on arrival (GeckoTerminal returns the candle
starting at CUT too). The list answers carry today's prices and volumes: they are dropped unread; only pool identity,
pool_created_at and reserve_in_usd (at fetch, the owner's field list) are kept.

Survey set:
  1. Pools: /networks/base/pools sorted by h24_volume_usd_desc and by h24_tx_count_desc (10 pages x 20 each), then
     /networks/base/dexes/{dex}/pools (h24_volume_usd_desc, 10 pages) for the TOP_DEXES dexes that appear most often in
     those two network lists. Tokens = both sides of every listed pool.
  2. Launch-universe tokens dropped: every address in data/daily/2026-10-23 .. 2026-11-07 tasks.csv (address column
     only; LAUNCH_DAYS); reported as a count only.
  3. /networks/base/tokens/multi (30 per call, include=top_pools): each token's deepest pool (highest reserve among its
     top pools and the listed pools) and its base / quote pair counts.
  4. Asset exclusion: core/verify.py asset_kind() imported (never copied): symbol + base/quote counts first, then again
     with the last daily close before CUT as the price (wrapped >= 100 USD).
  5. Established = the deepest pool created before ESTABLISHED_BEFORE (2026-10-09, >= 30 days at CUT). Younger pools:
     one count, no per-token detail, no candle call.
  6. Daily candles: /networks/base/pools/{pool}/ohlcv/day?aggregate=1&limit=1000&before_timestamp=CUT&currency=usd
     &token=<token> (one call per established pool; the public API gives ~183 days, see history_check()).

Files (backtest/liquid/, never data/): universe.json and daily.jsonl.gz (line 1 {"kind": "meta"}, then one
{"kind": "token", token, pool, symbol, candles [[t, o, h, l, c, v_usd], ...]} per token, compact() as tools/candles.py).
"""
import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.book import ROOT
from core.verify import asset_kind
from tools import candles

DAY_S = 86400
DIR = "backtest/liquid"
UNIVERSE = f"{DIR}/universe.json"
DAILY = f"{DIR}/daily.jsonl.gz"
ESTABLISHED_BEFORE = "2026-10-09T00:00:00Z"
LAUNCH_DAYS = ("2026-10-23", "2026-11-07")
LIST_PAGES = 10
TOP_DEXES = 8
SORTS = ("h24_volume_usd_desc", "h24_tx_count_desc")
MAX_CALLS = 1200
THRESHOLDS = (100_000, 1_000_000, 10_000_000)
HISTORY = (30, 90, 180, 365)
WEEKS = 26
GROUP_LOOKBACK_D = 31  # BL-39 draft MAX lookback: 31 daily closes = the trailing 30 days (the owner, Nov 9)
GROUP_SHARE = 0.10
BUCKET_LINES = (10_000, 100_000)
V = 5  # candle index of the USD volume


class BudgetError(Exception):
    pass


class Budget:
    """candles.Client (paced ~8/min, retried) with a hard call cap."""

    def __init__(self, client, max_calls=MAX_CALLS):
        self.client, self.max_calls = client, max_calls

    @property
    def calls(self):
        return self.client.calls

    @property
    def refused(self):
        return self.client.refused

    def get(self, path):
        if self.client.calls >= self.max_calls:
            raise BudgetError(f"max calls {self.max_calls}")
        return self.client.get(path)


def cut_ts(day):
    return candles._ts(f"{day}T00:00:00Z")


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _addr(rel, key):
    i = ((rel.get(key) or {}).get("data") or {}).get("id", "")
    net, _, addr = i.partition("_")
    return addr.lower() if net == "base" else ""


def pool_info(p):
    """Identity, pool_created_at and reserve only; today's prices and volumes in the answer are never read."""
    a, rel = p.get("attributes") or {}, p.get("relationships") or {}
    created = a.get("pool_created_at")
    try:
        reserve = float(a.get("reserve_in_usd"))
    except (TypeError, ValueError):
        reserve = None
    return {"pool": (a.get("address") or "").lower(), "dex": ((rel.get("dex") or {}).get("data") or {}).get("id", ""),
            "base": _addr(rel, "base_token"), "quote": _addr(rel, "quote_token"),
            "created": created.replace(".000", "") if isinstance(created, str) else None, "reserve": reserve}


# ---- 1. pools ----
def list_pools(api, errors):
    """{pool: info} from the network lists, then the busiest dexes' lists."""
    pools = {}

    def take(path):
        for page in range(1, LIST_PAGES + 1):
            try:
                body = api.get(f"{path}&page={page}") or {}
            except candles.FetchError as e:
                errors.append(f"{path} page {page}: {e}")
                continue
            data = body.get("data") or []
            for p in data:
                x = pool_info(p)
                if x["pool"]:
                    pools.setdefault(x["pool"], x)
            if len(data) < 20:
                break

    for s in SORTS:
        take(f"/networks/base/pools?sort={s}")
    counts = {}
    for x in pools.values():
        counts[x["dex"]] = counts.get(x["dex"], 0) + 1
    dexes = [d for d in sorted(counts, key=lambda d: (-counts[d], d)) if d][:TOP_DEXES]
    for d in dexes:
        take(f"/networks/base/dexes/{d}/pools?sort=h24_volume_usd_desc")
    return pools, dexes


# ---- 2. launch-universe addresses ----
def launch_addresses(root, cut_day, days=LAUNCH_DAYS):
    """Every address in data/daily/<d>/tasks.csv for d in days (inclusive), address column only. A day at or after
    CUT is refused (never opened)."""
    root, out = Path(root), set()
    first, last = days
    d, end = datetime.strptime(first, "%Y-%m-%d"), datetime.strptime(last, "%Y-%m-%d")
    while d <= end:
        name = d.strftime("%Y-%m-%d")
        if name >= cut_day:
            raise ValueError(f"tasks.csv {name} is at or after CUT")
        path = root / f"data/daily/{name}/tasks.csv"
        if path.exists():
            with open(path, newline="") as fh:
                rows = csv.reader(fh)
                header = next(rows, [])
                if "address" in header:
                    k = header.index("address")
                    out.update(r[k].lower() for r in rows if len(r) > k and r[k].lower().startswith("0x"))
        d += timedelta(days=1)
    return out


# ---- 3. tokens ----
def resolve_tokens(api, tokens, pools, errors):
    """{token: {symbol, pool, dex, created, reserve, base_pairs, quote_pairs}} from /tokens/multi top pools plus the
    listed pools; the deepest pool by reserve."""
    out = {}
    toks = sorted(tokens)
    for i in range(0, len(toks), candles.BATCH):
        part = toks[i:i + candles.BATCH]
        try:
            body = api.get(f"/networks/base/tokens/multi/{','.join(part)}?include=top_pools") or {}
        except candles.FetchError as e:
            errors.append(f"tokens/multi {i}: {e}")
            body = {}
        inc = {p.get("id"): pool_info(p) for p in body.get("included") or [] if p.get("type") == "pool"}
        for t in body.get("data") or []:
            a = t.get("attributes") or {}
            addr = (a.get("address") or "").lower()
            if addr not in tokens:
                continue
            ids = [x.get("id") for x in ((t.get("relationships") or {}).get("top_pools") or {}).get("data") or []]
            cands = {inc[x]["pool"]: inc[x] for x in ids if x in inc}
            out[addr] = {"symbol": a.get("symbol") or "", "cands": cands}
    for addr in tokens:
        rec = out.setdefault(addr, {"symbol": "", "cands": {}})
        for p in tokens[addr]:
            rec["cands"].setdefault(p, pools[p])
    final = {}
    for addr, rec in out.items():
        cands = [p for p in rec["cands"].values() if p["pool"]]
        if not cands:
            continue
        best = max(cands, key=lambda p: (p["reserve"] or 0, p["pool"]))
        final[addr] = {"symbol": rec["symbol"], "pool": best["pool"], "dex": best["dex"], "created": best["created"],
                       "reserve": best["reserve"], "base_pairs": sum(p["base"] == addr for p in cands),
                       "quote_pairs": sum(p["quote"] == addr for p in cands)}
    return final


# ---- 6. daily candles ----
def keep(c, cut):
    """A daily candle is kept only when its whole day ends by CUT."""
    return isinstance(c, list) and len(c) >= 6 and int(c[0]) + DAY_S <= cut


def daily(api, pool, token, cut):
    """Daily candles of `pool` priced in `token`, every day ended before CUT, oldest first."""
    body = api.get(f"/networks/base/pools/{pool}/ohlcv/day?aggregate=1&limit=1000&before_timestamp={cut}"
                   f"&currency=usd&token={token}") or {}
    rows = ((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    got = {int(c[0]): candles.compact(c) for c in rows if keep(c, cut)}
    return [got[t] for t in sorted(got)]


def history_check(api, pool, token, cut, back_d=200):
    """The ~183-day cap (the owner: max 2 calls): one request for candles ending back_d days before CUT, on the oldest
    established pool. Returns "api_limit" (refused with a 4xx or empty), "deeper" (older candles came back) or
    "error:<reason>". The answer is never stored."""
    tries, candles.RETRIES = candles.RETRIES, 2  # at most 2 attempts, a 429 retry included
    try:
        got = daily(api, pool, token, cut - back_d * DAY_S)
        return "deeper" if got else "api_limit"
    except candles.FetchError as e:
        return "api_limit" if str(e).startswith("http 4") else f"error:{e}"
    finally:
        candles.RETRIES = tries


def last_close(rows):
    """The last daily close before CUT: asset_kind()'s price input, the one price this module reads."""
    return float(rows[-1][4]) if rows else None


# ---- fetch ----
def fetch(api, root, cut, now=None):
    root, now = Path(root), now or candles._now()
    errors = []
    pools, dexes = list_pools(api, errors)
    tokens = {}
    for p in pools.values():
        for side in ("base", "quote"):
            if p[side]:
                tokens.setdefault(p[side], set()).add(p["pool"])
    launch = launch_addresses(root, _day(cut))
    dropped = sorted(set(tokens) & launch)
    for a in dropped:
        tokens.pop(a)
    res = resolve_tokens(api, tokens, pools, errors)
    est_before = candles._ts(ESTABLISHED_BEFORE)
    rows, young, no_created, kinds = [], 0, 0, {}
    for a in sorted(res):
        r = res[a]
        kind = asset_kind(r["symbol"], None, a, r["base_pairs"], r["quote_pairs"])
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
            continue
        if not r["created"]:
            no_created += 1
            continue
        if candles._ts(r["created"]) >= est_before:
            young += 1
            continue
        rows.append(dict(r, token=a))
    need = api.calls + len(rows) + 2
    if need > getattr(api, "max_calls", need):
        raise BudgetError(f"{len(rows)} established pools need ~{need} calls in all, over the cap {api.max_calls}")
    print(f"pools={len(pools)} tokens={len(res)} established={len(rows)} young={young} calls so far={api.calls}",
          file=sys.stderr, flush=True)
    lines, uni = [], []
    for r in rows:
        try:
            got = daily(api, r["pool"], r["token"], cut)
            err = None
        except candles.FetchError as e:
            got, err = [], str(e)
            errors.append(f"daily {r['pool']}: {e}")
        kind = asset_kind(r["symbol"], last_close(got), r["token"], r["base_pairs"], r["quote_pairs"])
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
            continue
        uni.append({"pool": r["pool"], "token": r["token"], "symbol": r["symbol"], "dex": r["dex"],
                    "pool_created_at": r["created"], "reserve_usd": r["reserve"], "quote_side_usd": None,
                    "asset_kind": "", "candle_error": err})
        lines.append({"token": r["token"], "pool": r["pool"], "symbol": r["symbol"], "candles": got})
    old = next((u for u in sorted(uni, key=lambda u: u["pool_created_at"]) if u["pool_created_at"]), None)
    cap = history_check(api, old["pool"], old["token"], cut) if old else "error"
    meta = {"fetched_at": candles._iso(now), "cut": candles._iso(cut), "card": "BL-39",
            "established_before": ESTABLISHED_BEFORE, "source": "geckoterminal",
            "lists": [f"/networks/base/pools?sort={s}" for s in SORTS] + [f"/dexes/{d}/pools" for d in dexes],
            "counts": {"pools_listed": len(pools), "tokens_listed": len(tokens) + len(dropped),
                       "launch_dropped": len(dropped), "resolved": len(res), "asset_excluded": kinds,
                       "no_created": no_created, "young": young, "established": len(uni)},
            "history_check": {"pool": old["pool"] if old else None, "back_d": 200, "result": cap},
            "quote_side_note": "GeckoTerminal pool answers carry no per-side reserve: quote_side_usd is null",
            "calls": api.calls, "refused": api.refused, "errors": errors[:50], "n_errors": len(errors)}
    return meta, uni, lines


def write(root, meta, uni, lines):
    root = Path(root)
    cut = candles._ts(meta["cut"])
    for x in lines:
        for c in x["candles"]:
            if not c[0] + DAY_S <= cut:
                raise AssertionError(f"candle {c[0]} not before CUT")
    (root / DIR).mkdir(parents=True, exist_ok=True)
    p = root / UNIVERSE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(dict(meta, tokens=uni), indent=1) + "\n")
    tmp.replace(p)
    candles.write(root / DAILY, meta, lines)  # line 1 = meta, then {"kind": "token", ...}


def load(root):
    root = Path(root)
    u = json.loads((root / UNIVERSE).read_text())
    _, lines = candles.read(root / DAILY)
    return u, lines


# ---- survey (counts only) ----
def _vols(lines):
    """{token: {day start t: volume_usd}}: the volume column only."""
    return {x["token"]: {int(c[0]): float(c[V]) for c in x["candles"]} for x in lines}


def _ge(values, ths):
    return {t: sum(v >= t for v in values) for t in ths}


def survey(u, lines):
    cut = candles._ts(u["cut"])
    vols = _vols(lines)
    uni = {x["token"]: x for x in u["tokens"]}
    n = len(uni)
    out = {"n": n, "counts": u["counts"], "history_check": u["history_check"]}
    out["reserve"] = {"n": n, "known": sum(x["reserve_usd"] is not None for x in uni.values()),
                      "ge": _ge([x["reserve_usd"] or 0 for x in uni.values()], THRESHOLDS)}
    last30 = [cut - DAY_S * k for k in range(30, 0, -1)]
    med30 = {a: statistics.median([vols.get(a, {}).get(t, 0.0) for t in last30]) for a in uni}
    out["median_vol_30d"] = {"n": n, "ge": _ge(med30.values(), THRESHOLDS)}
    first = {a: min(v) for a, v in vols.items() if v}
    hist = {a: (cut - t) // DAY_S for a, t in first.items()}
    held = {a: len(v) for a, v in vols.items() if v}
    out["history"] = {"n": n, "with_candles": len(first), "no_candles": n - len(first),
                      "span_ge": {d: sum(h >= d for h in hist.values()) for d in HISTORY},
                      "candles_ge": {d: sum(h >= d for h in held.values()) for d in HISTORY},
                      "pool_age_ge_365": sum(1 for x in uni.values() if x["pool_created_at"]
                                             and cut - candles._ts(x["pool_created_at"]) >= 365 * DAY_S),
                      "max_span_days": max(hist.values()) if hist else 0,
                      "max_candles": max(held.values()) if held else 0,
                      "earliest_candle": _day(min(first.values())) if first else None}
    # gaps: days with no candle between the first candle and CUT, zero-volume candles
    miss, zero = {}, {}
    for a, v in vols.items():
        if not v:
            continue
        span = range(min(v), cut, DAY_S)
        miss[a] = sum(t not in v for t in span)
        zero[a] = sum(1 for x in v.values() if x == 0)
    mv = list(miss.values())
    out["gaps"] = {"n": len(miss), "missing_days_total": sum(mv), "zero_volume_days_total": sum(zero.values()),
                   "tokens_with_zero_volume_day": sum(1 for z in zero.values() if z),
                   "missing_buckets": {"0": sum(m == 0 for m in mv), "1-5": sum(1 <= m <= 5 for m in mv),
                                       "6-30": sum(6 <= m <= 30 for m in mv), ">30": sum(m > 30 for m in mv)}}
    start = window_start(vols, uni, miss, cut)
    out["history"]["window_start"] = _day(start)
    out["weeks"] = weekly(vols, cut, start)
    out["survivorship"] = survivorship(vols, uni, out["weeks"])
    return out


def window_start(vols, uni, miss, cut):
    """The first day the candle window covers for every pool: the latest first candle among pools created more than
    200 days before CUT that have no missing day (they trade daily, so their first candle is the API's edge, ~184
    candles per answer). Thin pools reach further back with gaps and do not set it. Fallback: the earliest candle."""
    edges = [min(vols[a]) for a in vols if vols[a] and miss.get(a) == 0 and uni.get(a, {}).get("pool_created_at")
             and candles._ts(uni[a]["pool_created_at"]) < cut - 200 * DAY_S]
    if edges:
        return max(edges)
    return min((min(v) for v in vols.values() if v), default=cut)


def mondays(cut, weeks=WEEKS):
    """The last `weeks` Mondays 00:00 UTC at or before CUT, newest first (each week's as-of point)."""
    d = datetime.fromtimestamp(cut, timezone.utc)
    m = int((d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0).timestamp())
    return [m - 7 * DAY_S * k for k in range(weeks)]


def weekly(vols, cut, window_start):
    """Per as-of Monday m: the trailing 7 days [m - 7 d, m) as mean USD/day. Only full weeks inside the candle window
    (m - 7 d >= window_start) are kept."""
    rows = []
    for m in sorted(mondays(cut)):
        days7 = [m - DAY_S * k for k in range(7, 0, -1)]
        if days7[0] < window_start:
            continue
        look = [m - DAY_S * k for k in range(GROUP_LOOKBACK_D, 0, -1)]
        avg7 = {a: sum(v.get(t, 0.0) for t in days7) / 7 for a, v in vols.items()}
        traded = {a for a, v in vols.items() if any(v.get(t, 0) > 0 for t in days7)}
        full31 = {a for a in traded if all(t in vols[a] for t in look)}
        ge100k = {a for a in traded if avg7[a] >= THRESHOLDS[0]}
        bottom = sorted(traded, key=lambda a: (avg7[a], a))[:math.ceil(GROUP_SHARE * len(traded))]
        bv = [avg7[a] for a in bottom]
        rows.append({"monday": _day(m), "universe": len(traded), "ge": _ge([avg7[a] for a in traded], THRESHOLDS),
                     "lookback_in_window": look[0] >= window_start,
                     **({"max_eligible": len(full31), "max_group": math.ceil(GROUP_SHARE * len(full31)),
                         "max_eligible_100k": len(full31 & ge100k),
                         "max_group_100k": math.ceil(GROUP_SHARE * len(full31 & ge100k))}
                        if look[0] >= window_start else
                        {"max_eligible": None, "max_group": None, "max_eligible_100k": None, "max_group_100k": None}),
                     "bottom": {"n": len(bottom), "median_vol_day": statistics.median(bv) if bv else None,
                                "ge": _ge(bv, BUCKET_LINES)},
                     "_avg7": avg7, "_traded": traded})
    return rows


def survivorship(vols, uni, weeks):
    """A floor only: tokens still listed today that left the volume universe between the first and last full week.
    Dead tokens are not listed, so they are invisible here."""
    if not weeks:
        return {}
    w0, w1 = weeks[0], weeks[-1]
    out = {"first_week": w0["monday"], "last_week": w1["monday"], "by_threshold": {}}
    for t in THRESHOLDS:
        start = {a for a in w0["_traded"] if w0["_avg7"][a] >= t}
        still = {a for a in start if a in w1["_traded"] and w1["_avg7"][a] >= t}
        quiet = {a for a in start if a not in w1["_traded"]}
        out["by_threshold"][t] = {"n_start": len(start), "still": len(still), "below": len(start - still - quiet),
                                  "no_trade_last_week": len(quiet)}
    ever = {a for w in weeks for a in w["_traded"] if w["_avg7"][a] >= THRESHOLDS[0]}
    out["ever_ge_100k"] = len(ever)
    out["ever_ge_100k_reserve_lt_100k"] = sum(1 for a in ever if (uni[a]["reserve_usd"] or 0) < THRESHOLDS[0])
    last = max((max(v) for v in vols.values() if v), default=0)
    out["stopped_7d_before_cut"] = sum(1 for v in vols.values() if v and max(v) < last - 6 * DAY_S)
    return out


def public(s):
    """The survey without the per-token working sets."""
    s = dict(s)
    s["weeks"] = [{k: v for k, v in w.items() if not k.startswith("_")} for w in s["weeks"]]
    return s


def max_ts(lines):
    return max((c[0] for x in lines for c in x["candles"]), default=None)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.liquid", description=__doc__.split("\n")[0])
    ap.add_argument("--cut", required=True, help="YYYY-MM-DD, 00:00 UTC")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--survey", action="store_true", help="from the committed files, no request")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    cut = cut_ts(a.cut)
    if a.survey:
        u, lines = load(a.root)
        if u["cut"] != candles._iso(cut):
            raise SystemExit(f"files are for cut {u['cut']}")
    else:
        api = Budget(candles.Client(), a.max_calls)
        meta, uni, lines = fetch(api, a.root, cut)
        u = dict(meta, tokens=uni)
        if a.write:
            write(a.root, meta, uni, lines)
    s = public(survey(u, lines))
    mt = max_ts(lines)
    print(json.dumps(s, indent=1, default=str))
    print(f"calls={u['calls']} refused={u['refused']} errors={u['n_errors']} max_candle_start="
          f"{candles._iso(mt) if mt else None} (+1 d <= cut {u['cut']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
