"""Weekly point-in-time snapshot of liquid Base pools (BL-46): identity, reserve and 24 h volume only, by workflow.

    python -m tools.snapshot --write         fetch and write backtest/liquid/snapshots/<UTC day>.json.gz
    python -m tools.snapshot --write --out DIR   the same into DIR (smoke runs; never committed)
    python -m tools.snapshot                 fetch and print the header, write nothing

A forward record for a later BL-38/39 test that needs point-in-time lists (the survey's survivorship note, docs/NOTES.md
"BL-39 survey"). Never on the tick path; core/ never imports tools/ and never reads backtest/. Same lists and method as
the BL-39 survey (tools/liquid.py, imported): /networks/base/pools sorted by h24_volume_usd_desc and h24_tx_count_desc,
then /networks/base/dexes/{dex}/pools (h24_volume_usd_desc) for the liquid.TOP_DEXES dexes most frequent in those two
lists, liquid.LIST_PAGES pages each; tokens = both sides of every listed pool; /networks/base/tokens/multi
(include=top_pools) gives each token's deepest pool (highest reserve among its top pools and the listed pools) and
its base / quote pair counts. The list calls add include=base_token,quote_token, for the symbols only.

Stored per pool (the owner, BL-46 option A): pool, dex, base, quote, base_symbol, quote_symbol, pool_created_at,
reserve_usd, volume_h24_usd, lists. Records are built field by field from those keys only: no price, price change,
FDV, market cap, candle or return is ever copied (test). No age filter: young pools are kept, pool_created_at is
stored so a later spec filters by age as of the snapshot. Per token: symbol, deepest pool, base / quote pair counts
and core/verify.py asset_kind() (imported), whose price input is the /tokens/multi price_usd read in memory only and
never stored (the survey used the last daily close).

Seal (BL-34, the owner's option A, and seal fix A of 2026-11-13; the same address-column read as BL-45,
tools/costs.py launch_addresses()): every pool with a base or quote whose address is in any data/daily/*/tasks.csv
address column, and every such token, is kept out of the main file and written instead to
backtest/liquid/snapshots/sealed/<UTC day>.json.gz (same fields, header sealed: true, the rule, counts, complete).
Sealed tokens are resolved with /tokens/multi like the others, for the sealed file only. Nothing reads the sealed
files (read() refuses them; a test checks no other module under tools/ or core/ names the folder); the run prints
their counts and bytes only. A later story, after BL-34's holdout run, decides how they are opened.

complete: false when the call cap was hit or a call failed after every retry (a partial week beats a lost week); a
429 that a retry got through is counted in "refused" but loses nothing. A run where no pool came back writes nothing.
One main and one sealed file per UTC day, never overwritten: the run refuses before its first call if either exists.
"""
import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.book import ROOT
from core.verify import asset_kind
from tools import candles, liquid
from tools.costs import launch_addresses

DIR = "backtest/liquid/snapshots"
SEALED = "sealed"  # backtest/liquid/snapshots/sealed/: written here, read nowhere (seal fix A)
MAX_CALLS = 300
SOURCE = "geckoterminal api v2 (keyless public API)"
POOL_KEYS = ("pool", "dex", "base", "quote", "base_symbol", "quote_symbol", "pool_created_at", "reserve_usd",
             "volume_h24_usd", "lists")
TOKEN_KEYS = ("token", "symbol", "pool", "base_pairs", "quote_pairs", "asset_kind")
TOP_POOLS = "tokens/multi:top_pools"
EXIT_PARTIAL = 3


class CapError(Exception):
    pass


class Budget:
    """candles.Client (paced, retried) under a HARD cap: a get() starts only while every retry it may need still fits
    (calls + candles.RETRIES <= max_calls), so attempts never pass max_calls."""

    def __init__(self, client, max_calls=MAX_CALLS):
        self.client, self.max_calls = client, max_calls
        self.capped = False

    @property
    def calls(self):
        return self.client.calls

    @property
    def refused(self):
        return self.client.refused

    def get(self, path):
        if self.client.calls + candles.RETRIES > self.max_calls:
            self.capped = True
            raise CapError(f"max calls {self.max_calls}")
        return self.client.get(path)


def day_of(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def path_for(out_dir, t):
    return Path(out_dir) / f"{day_of(t)}.json.gz"


def sealed_path(out_dir, t):
    return Path(out_dir) / SEALED / f"{day_of(t)}.json.gz"


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _addr(rel, key):
    i = ((rel.get(key) or {}).get("data") or {}).get("id", "")
    net, _, addr = i.partition("_")
    return addr.lower() if net == "base" else ""


def pool_record(p, symbols, tag):
    """One pool from an API pool object: the owner's fields only, each read by name; nothing else is touched."""
    a, rel = p.get("attributes") or {}, p.get("relationships") or {}
    created = a.get("pool_created_at")
    base, quote = _addr(rel, "base_token"), _addr(rel, "quote_token")
    return {"pool": (a.get("address") or "").lower(),
            "dex": ((rel.get("dex") or {}).get("data") or {}).get("id", ""),
            "base": base, "quote": quote,
            "base_symbol": symbols.get(base, ""), "quote_symbol": symbols.get(quote, ""),
            "pool_created_at": created.replace(".000", "") if isinstance(created, str) else None,
            "reserve_usd": _num(a.get("reserve_in_usd")),
            "volume_h24_usd": _num((a.get("volume_usd") or {}).get("h24")),
            "lists": [tag]}


def _symbols(body):
    """{address: symbol} from an answer's included token objects (symbol only)."""
    out = {}
    for t in body.get("included") or []:
        if t.get("type") == "token":
            a = t.get("attributes") or {}
            addr = (a.get("address") or "").lower()
            if addr:
                out[addr] = a.get("symbol") or ""
    return out


def _add(pools, rec):
    if not rec["pool"]:
        return
    old = pools.get(rec["pool"])
    if old is None:
        pools[rec["pool"]] = rec
        return
    for t in rec["lists"]:
        if t not in old["lists"]:
            old["lists"].append(t)
    for k in ("base_symbol", "quote_symbol"):
        old[k] = old[k] or rec[k]


FAILED = object()


class Run:
    def __init__(self, api):
        self.api, self.errors, self.failed, self.symbols = api, [], 0, {}

    def get(self, path):
        """The body, None on 404, FAILED for a call that failed after every retry (counted); CapError propagates."""
        try:
            return self.api.get(path)
        except candles.FetchError as e:
            self.failed += 1
            self.errors.append(f"{path.split('?')[0]}: {e}")
            return FAILED

    # ---- 1. pools (the survey's lists) ----
    def list_pools(self, pools):
        def take(path, tag):
            for page in range(1, liquid.LIST_PAGES + 1):
                body = self.get(f"{path}&page={page}&include=base_token,quote_token")
                if body is FAILED:
                    continue  # a failed page: counted, the next page is still asked
                if body is None:
                    break  # 404: the list ended
                data = body.get("data") or []
                syms = _symbols(body)
                self.symbols.update(syms)
                for p in data:
                    _add(pools, pool_record(p, syms, tag))
                if len(data) < 20:
                    break

        for s in liquid.SORTS:
            take(f"/networks/base/pools?sort={s}", f"pools:{s}")
        counts = {}
        for x in pools.values():
            counts[x["dex"]] = counts.get(x["dex"], 0) + 1
        dexes = [d for d in sorted(counts, key=lambda d: (-counts[d], d)) if d][:liquid.TOP_DEXES]
        for d in dexes:
            take(f"/networks/base/dexes/{d}/pools?sort=h24_volume_usd_desc", f"dex:{d}")
        return dexes

    # ---- 3. tokens ----
    def resolve(self, tokens, pools):
        """{token: {symbol, cands, price}} from /tokens/multi; price is asset_kind()'s input, in memory only."""
        out = {}
        toks = sorted(tokens)
        for i in range(0, len(toks), candles.BATCH):
            part = toks[i:i + candles.BATCH]
            body = self.get(f"/networks/base/tokens/multi/{','.join(part)}?include=top_pools")
            if body is FAILED or body is None:
                continue
            for t in body.get("data") or []:
                a = t.get("attributes") or {}
                addr = (a.get("address") or "").lower()
                if addr:
                    self.symbols[addr] = a.get("symbol") or ""
            syms = self.symbols
            inc = {}
            for p in body.get("included") or []:
                if p.get("type") == "pool":
                    inc[p.get("id")] = pool_record(p, syms, TOP_POOLS)
            for t in body.get("data") or []:
                a = t.get("attributes") or {}
                addr = (a.get("address") or "").lower()
                if addr not in tokens:
                    continue
                ids = [x.get("id") for x in ((t.get("relationships") or {}).get("top_pools") or {}).get("data") or []]
                cands = {}
                for x in ids:
                    if x in inc:
                        _add(pools, inc[x])
                        cands[inc[x]["pool"]] = pools[inc[x]["pool"]]
                out[addr] = {"symbol": a.get("symbol") or "", "cands": cands, "price": _num(a.get("price_usd"))}
        return out


def _tokens(addrs, tokens, res, pools, kinds):
    """Token records for `addrs`, each with its deepest pool among `pools` (the file's own pools)."""
    out, unresolved = [], 0
    for a in sorted(addrs):
        r = res.get(a, {"symbol": "", "cands": {}, "price": None})
        cands = {k: pools[k] for k in set(r["cands"]) | tokens[a] if k in pools}
        if not cands:
            unresolved += 1
            continue
        best = max(cands.values(), key=lambda p: (p["reserve_usd"] or 0, p["pool"]))
        bp = sum(p["base"] == a for p in cands.values())
        qp = sum(p["quote"] == a for p in cands.values())
        kind = asset_kind(r["symbol"], r["price"], a, bp, qp)  # the price stays in memory
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        out.append({"token": a, "symbol": r["symbol"], "pool": best["pool"], "base_pairs": bp, "quote_pairs": qp,
                    "asset_kind": kind})
    return out, unresolved


def _pools(sel):
    out = [{k: sel[p][k] for k in POOL_KEYS} for p in sorted(sel)]
    for p in out:
        p["lists"] = sorted(p["lists"])
    return out


def snapshot(api, root, now=None):
    """(doc, sealed_doc, complete). doc = the main file {"meta", "pools", "tokens"}: no sealed address at all.
    sealed_doc = the same fields for every pool and token the seal removed (seal fix A); only write() touches it."""
    now = now or candles._now()
    run = Run(api)
    sealed = launch_addresses(root)
    pools, dexes, res, tokens = {}, [], {}, {}
    try:
        dexes = run.list_pools(pools)
        for p in list(pools.values()):
            for side in ("base", "quote"):
                if p[side]:
                    tokens.setdefault(p[side], set()).add(p["pool"])
        res = run.resolve(tokens, pools)  # sealed tokens too (seal fix A): they go to the sealed file only
    except CapError as e:
        run.errors.append(str(e))
    listed_n = sum(1 for p in pools.values() if p["lists"] != [TOP_POOLS])
    for p in pools.values():  # a symbol learned from another answer fills a blank
        for side in ("base", "quote"):
            if not p[f"{side}_symbol"]:
                p[f"{side}_symbol"] = run.symbols.get(p[side], "")
    keep = {k: p for k, p in pools.items() if p["base"] not in sealed and p["quote"] not in sealed}
    held = {k: p for k, p in pools.items() if k not in keep}
    open_toks = [a for a in tokens if a not in sealed]
    held_toks = [a for a in tokens if a in sealed]
    kinds, held_kinds = {}, {}
    toks, unresolved = _tokens(open_toks, tokens, res, keep, kinds)
    s_toks, s_unresolved = _tokens(held_toks, tokens, res, held, held_kinds)
    complete = not api.capped and run.failed == 0
    out_pools, s_pools = _pools(keep), _pools(held)
    run_info = {"calls": api.calls, "refused": api.refused, "failed": run.failed, "max_calls": api.max_calls,
                "capped": api.capped}
    meta = {"card": "BL-46", "as_of": candles._iso(now), "source": SOURCE, "complete": complete,
            "params": {"sorts": list(liquid.SORTS), "pages": liquid.LIST_PAGES, "per_page": 20,
                       "top_dexes": liquid.TOP_DEXES, "dexes": dexes,
                       "lists": [f"/networks/base/pools?sort={s}" for s in liquid.SORTS]
                       + [f"/networks/base/dexes/{d}/pools?sort=h24_volume_usd_desc" for d in dexes],
                       "include": "base_token,quote_token (symbols only)",
                       "tokens": "/networks/base/tokens/multi?include=top_pools, 30 per call",
                       "deepest_pool": "highest reserve_in_usd among the token's top pools and listed pools",
                       "asset_kind": "core/verify.py asset_kind(); price input = /tokens/multi price_usd, in memory "
                                     "only, never stored"},
            **run_info,
            "seal": {"rule": "address in any data/daily/*/tasks.csv address column (BL-34 seal A)",
                     "tokens_dropped": len(held_toks), "pools_dropped": len(held)},
            "counts": {"pools_listed": listed_n, "pools_from_top_pools": len(pools) - listed_n,
                       "pools_written": len(out_pools), "tokens_listed": len(tokens),
                       "tokens_resolved": len(toks), "tokens_unresolved": unresolved, "asset_kinds": kinds},
            "errors": run.errors[:50], "n_errors": len(run.errors)}
    s_meta = {"card": "BL-46", "as_of": meta["as_of"], "sealed": True, "source": SOURCE, "complete": complete,
              "rule": "every pool with a base or quote, and every token, whose address is in any "
                      "data/daily/*/tasks.csv address column as of the run (BL-34 seal A); the main file's "
                      "params apply",
              **run_info,
              "counts": {"pools": len(s_pools), "tokens": len(s_toks), "tokens_unresolved": s_unresolved,
                         "asset_kinds": held_kinds}}
    return ({"meta": meta, "pools": out_pools, "tokens": toks},
            {"meta": s_meta, "pools": s_pools, "tokens": s_toks}, complete)


def write(out_dir, doc, now):
    """The day's file, created exclusively (never overwritten); gzip with mtime 0. A sealed doc (meta.sealed) goes
    to <out>/sealed/; nothing else in the repo reads that folder."""
    p = (sealed_path if doc["meta"].get("sealed") else path_for)(out_dir, now)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(doc, separators=(",", ":"), sort_keys=False) + "\n").encode()
    with open(p, "xb") as f:
        f.write(gzip.compress(raw, mtime=0))
    return p, len(raw)


def read(p):
    """A main snapshot file. Sealed files are never read here (a later story, after BL-34's holdout run)."""
    if SEALED in Path(p).parts:
        raise PermissionError("sealed snapshot files are not read")
    return json.loads(gzip.decompress(Path(p).read_bytes()))


def main(argv=None, client=None, now=None):
    ap = argparse.ArgumentParser(prog="python -m tools.snapshot", description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default=None, help=f"output folder (default {DIR})")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    now = now or candles._now()
    out_dir = Path(a.out) if a.out else Path(a.root) / DIR
    if a.write:
        for target in (path_for(out_dir, now), sealed_path(out_dir, now)):
            if target.exists():
                print(f"refused: {day_of(now)} already written (never overwritten)", file=sys.stderr)
                return 2
    api = Budget(client or candles.Client(), a.max_calls)
    doc, sealed_doc, complete = snapshot(api, a.root, now)
    m = doc["meta"]
    print(json.dumps({k: m[k] for k in ("as_of", "complete", "calls", "refused", "failed", "capped", "seal",
                                        "counts", "n_errors")}))
    if not doc["pools"] and not sealed_doc["pools"]:
        print("no pool came back: nothing written", file=sys.stderr)
        return 1
    if a.write:
        p, n = write(out_dir, doc, now)
        print(f"wrote {p} raw_bytes={n} gz_bytes={p.stat().st_size}")
        sp, sn = write(out_dir, sealed_doc, now)
        c = sealed_doc["meta"]["counts"]
        print(f"sealed file: pools={c['pools']} tokens={c['tokens']} raw_bytes={sn} gz_bytes={sp.stat().st_size}")
    return 0 if complete else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
