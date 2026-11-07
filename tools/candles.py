"""Candles (BL-33): GeckoTerminal 5-minute pool OHLCV for the backtest. By hand only, never on the tick path.

    python -m tools.candles --list-days            complete logged days that still need a final file
    python -m tools.candles --day YYYY-MM-DD       fetch one sight day (every token of the logging set seen that day)
    python -m tools.candles --twin-validation      the twin's logged trades on every complete logged day
    python -m tools.candles --eth                  ETH (deepest WETH/USDC pool) for every complete day still missing
    python -m tools.candles --status               rewrite backtest/status.json only (no request)

Run from .github/workflows/backtest.yml (workflow_dispatch only) or by hand in a session. core/ never imports this
module and the tick never reads backtest/. Fetching is not testing: every logged day is fetched, holdout days too;
only tools/backtest.py refuses holdout days without the sealed lock (the owner, BL-33).

Tokens = the logging set (core/verify.py logged(), read back from tasks.csv as tools/replay.py past_gate()): every
desk VERIFY row in it on that UTC day, the first row per address that day = the sighting. Window = sight -> +7 days
(WINDOW_S), paging past 1,000 candles when a pool needs it.

Pool per token, with a match-quality class (MATCH):
  exact   the pool is logged: the position's "pair" in the state.json committed by that tick (traded tokens, git
          history), else outcomes.csv sight_pair (files from 2026-11-06)
  single  GeckoTerminal lists exactly one pool for the token and its dex matches the logged dex
  age     several pools, exactly one with the logged dex whose creation time matches the logged pair_age (+-10 min;
          pair_age is logged from 2026-11-05)
  dex     several pools, exactly one with the logged dex (ambiguous: GeckoTerminal lists top pools only)
  guess   several pools with the logged dex, the deepest taken
  other   no pool with the logged dex, the deepest taken
  none    no pool found
QUALIFIED = exact, single, age: the only classes a holdout run (BL-34) uses.

Pool lookup: GeckoTerminal /tokens/multi (30 tokens per call, include=top_pools), then /tokens/{a}/pools (up to 20
pools) only for a token whose top pools leave it ambiguous. OHLCV: /pools/{pool}/ohlcv/minute?aggregate=5, newest
first, limit 1,000 (the API refuses more: "must be positive integer less than or equal to 1000"), before_timestamp to
page back, currency=usd&token=<our token> so prices are our token's USD price. Only 5-minute windows with a trade are
returned (sparse). Rate limit measured from this sandbox 2026-11-06 (no rate-limit headers; a 429 says Retry-After 0;
docs unreachable): 1 call/s 2 of 14 refused, 20/min 12 of 30, 10/min 3 of 20, so PACE_S = 7.5 s (8/min) and up to
RETRIES tries per call with BACKOFF_S between them.

Files (backtest/candles/, never data/): <sight-day>.jsonl.gz, written once when every window of that day has closed
(last sight + 7 days); before that <sight-day>.open.jsonl.gz, replaced by each later fetch and deleted when the final
file is written. twin-validation.jsonl.gz, written once. backtest/status.json (BL-43, display only, for the site's
Backtest lab card): rewritten at the end of every run but --list-days, and by --status; see write_status(). Line 1 is {"kind": "meta", ...}; then one line per token:
{"kind": "token", "address", "symbol", "sight_ts", "pool", "match", "pool_created", "start", "end", "candles":
[[t, open, high, low, close, volume_usd], ...]} with t = the candle's start (unix s), oldest first, prices at 6 and
volume at 4 significant digits (compact()), and end = the last second the candles cover (min(window end, fetch time)).
"""
import argparse
import gzip
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.book import ROOT
from core.report import _kv
from tools import replay

GT = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "baseline-desk-backtest"}
PACE_S = 7.5
RETRIES = 5
BACKOFF_S = 15
TIMEOUT_S = 20
CANDLE_S = 300
MAX_PER_CALL = 1000
MAX_PAGES = 6  # 6 x 1,000 candles: more than a 7-day window holds (2,016)
WINDOW_S = 7 * 86400
BATCH = 30
AGE_TOL_S = 600
FIRST_DAY = "2026-10-31"  # the first complete UTC day with qliq on every logged row (BL-28)
DIR = "backtest/candles"
MATCH = ["exact", "single", "age", "dex", "guess", "other", "none"]
QUALIFIED = ("exact", "single", "age")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class FetchError(Exception):
    pass


def _ts(iso):
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now():
    return int(time.time())


# ---- client ----
class Client:
    """GeckoTerminal GET, paced (PACE_S between calls) with retries on 429/5xx/timeouts. get() returns the JSON body,
    or None on 404; raises FetchError when every try failed. calls / refused count every attempt."""

    def __init__(self, urlopen=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic, pace=PACE_S):
        self.urlopen, self.sleep, self.clock, self.pace = urlopen, sleep, clock, pace
        self.calls = self.refused = 0
        self._last = None

    def _wait(self):
        if self._last is not None:
            gap = self.pace - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()

    def get(self, path):
        for attempt in range(RETRIES):
            self._wait()
            self.calls += 1
            try:
                with self.urlopen(urllib.request.Request(GT + path, headers=HEADERS), timeout=TIMEOUT_S) as f:
                    return json.loads(f.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code != 429 and e.code < 500:
                    raise FetchError(f"http {e.code}")
                self.refused += 1
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                self.refused += 1
            if attempt < RETRIES - 1:
                self.sleep(BACKOFF_S)
        raise FetchError("retries")


# ---- tokens from the logs ----
def sightings(root, day):
    """{address: {symbol, sight_ts, dex, pair_age, created}} for the logging set's first row per address on `day`."""
    out = {}
    for r in replay._read(Path(root) / f"data/daily/{day}/tasks.csv"):
        if not replay.past_gate(r) or r["address"] in out:
            continue
        kv = _kv(r["detail"])
        age = replay._num(kv.get("pair_age"))
        out[r["address"]] = {"symbol": r["symbol"], "sight_ts": r["ts"], "dex": kv.get("dex", "na"),
                             "pair_age": age, "created": _ts(r["ts"]) - age * 60 if age is not None else None}
    return out


def sight_pairs(root, day):
    """{address: sight_pair} from that day's outcomes.csv (BL-30, files from 2026-11-06)."""
    out = {}
    for r in replay._read(Path(root) / f"data/daily/{day}/outcomes.csv"):
        p = (r.get("sight_pair") or "").lower()
        if p.startswith("0x") and r["address"] not in out:
            out[r["address"]] = p
    return out


def state_pairs(root, buys):
    """{(book, address, buy ts): pair} from the state.json the buying tick committed ("data: tick <minute>"), read
    from git history; {} when git or the commit is not there. buys = [(book, address, ts)]."""
    try:
        log = subprocess.run(["git", "-C", str(root), "log", "--format=%H %s", "--", "data/state.json"],
                             capture_output=True, text=True, timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    by_min = {}
    for line in log.splitlines():
        h, _, subject = line.partition(" ")
        m = re.fullmatch(r"data: tick (\d{4}-\d{2}-\d{2}T\d{2}:\d{2})Z", subject)
        if m:
            by_min.setdefault(m.group(1), h)
    out, cache = {}, {}
    for book, address, t in buys:
        h = by_min.get(t[:16])
        if not h:
            continue
        if h not in cache:
            try:
                cache[h] = json.loads(subprocess.run(["git", "-C", str(root), "show", f"{h}:data/state.json"],
                                                     capture_output=True, text=True, timeout=60, check=True).stdout)
            except (OSError, subprocess.SubprocessError, ValueError):
                cache[h] = {}
        p = ((cache[h].get(book) or {}).get("positions") or {}).get(address) or {}
        if p.get("entry_ts") == t and str(p.get("pair", "")).startswith("0x"):
            out[(book, address, t)] = p["pair"].lower()
    return out


# ---- pools ----
def dex_match(logged, gt_dex_id):
    """Logged dex "uniswap:v4" vs GeckoTerminal "uniswap-v4-base": family and (when logged) version."""
    if not logged or logged == "na" or not gt_dex_id:
        return False
    family, _, version = logged.partition(":")
    gid = gt_dex_id.lower()
    return family.lower() in gid and (not version or version.lower() in gid)


def _pool(p):
    a, rel = p.get("attributes") or {}, p.get("relationships") or {}
    created = a.get("pool_created_at")
    return {"pool": (a.get("address") or "").lower(), "dex": ((rel.get("dex") or {}).get("data") or {}).get("id", ""),
            "created": _ts(created.replace(".000", "")) if isinstance(created, str) and len(created) >= 20 else None,
            "reserve": replay._num(a.get("reserve_in_usd")) or 0.0}


def classify(tok, pools):
    """(pool, match, created) for one token from its candidate pools (see MATCH)."""
    if not pools:
        return None, "none", None
    dexm = [p for p in pools if dex_match(tok.get("dex"), p["dex"])]
    if tok.get("created") is not None:
        age = [p for p in dexm if p["created"] is not None and abs(p["created"] - tok["created"]) <= AGE_TOL_S]
        if len(age) == 1:
            return age[0]["pool"], "age", age[0]["created"]
    if len(pools) == 1 and dexm:
        return pools[0]["pool"], "single", pools[0]["created"]
    if len(dexm) == 1:
        return dexm[0]["pool"], "dex", dexm[0]["created"]
    pick = max(dexm or pools, key=lambda p: p["reserve"])
    return pick["pool"], ("guess" if dexm else "other"), pick["created"]


def resolve(client, toks, known=None):
    """{address: (pool, match, created)}. known = {address: pool} logged exactly; the rest by tokens/multi batches,
    then /tokens/{a}/pools for a token its top pools leave ambiguous (class dex, guess, other or none)."""
    known, out = known or {}, {}
    for a in toks:
        if known.get(a):
            out[a] = (known[a], "exact", None)
    rest = [a for a in toks if a not in out]
    every = list(toks)  # exact tokens ride in the same batches: their pool_created_at, no extra call (BL-33 part 2)
    for i in range(0, len(every), BATCH):
        part = every[i:i + BATCH]
        body = client.get(f"/networks/base/tokens/multi/{','.join(part)}?include=top_pools") or {}
        pools = {p.get("id"): _pool(p) for p in body.get("included") or [] if p.get("type") == "pool"}
        for t in body.get("data") or []:
            a = ((t.get("attributes") or {}).get("address") or "").lower()
            ids = [x.get("id") for x in ((t.get("relationships") or {}).get("top_pools") or {}).get("data") or []]
            if a not in toks:
                continue
            cands = [pools[x] for x in ids if x in pools]
            if a in out and out[a][1] == "exact":
                created = next((p["created"] for p in cands if p["pool"] == out[a][0]), None)
                out[a] = (out[a][0], "exact", created)
            else:
                out[a] = classify(toks[a], cands)
    for a in rest:
        if a not in out or out[a][1] not in QUALIFIED:
            body = client.get(f"/networks/base/tokens/{a}/pools?page=1") or {}
            got = classify(toks[a], [_pool(p) for p in body.get("data") or []])
            if a not in out or MATCH.index(got[1]) <= MATCH.index(out[a][1]):
                out[a] = got
    return out


def compact(c):
    """[t, o, h, l, c, v] with prices at 6 and volume at 4 significant digits (2.4x smaller gzipped, BL-33)."""
    return [int(c[0])] + [float(f"{float(x):.6g}") for x in c[1:5]] + [float(f"{float(c[5]):.4g}")]


def ohlcv(client, pool, token, start, end):
    """5-minute candles [[t, o, h, l, c, v], ...] of `pool` priced in `token` (USD), start <= t < end, oldest first,
    paging back from `end` by 1,000 until the window is covered."""
    got, before = {}, int(end)
    for _ in range(MAX_PAGES):
        body = client.get(f"/networks/base/pools/{pool}/ohlcv/minute?aggregate=5&limit={MAX_PER_CALL}"
                          f"&before_timestamp={before}&currency=usd&token={token}") or {}
        rows = ((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        for c in rows:
            if isinstance(c, list) and len(c) >= 6 and start <= int(c[0]) < end:
                got[int(c[0])] = compact(c)
        if len(rows) < MAX_PER_CALL:
            break
        oldest = min(int(c[0]) for c in rows)
        if oldest <= start:
            break
        before = oldest
    return [got[t] for t in sorted(got)]


# ---- files ----
def write(path, meta, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps(dict(meta, kind="meta"), separators=(",", ":")) + "\n")
        for x in lines:
            fh.write(json.dumps(dict(x, kind="token"), separators=(",", ":")) + "\n")
    tmp.replace(path)


def read(path):
    """(meta, [token lines]) of one candle file; ({}, []) when it is not there."""
    path = Path(path)
    if not path.exists():
        return {}, []
    meta, out = {}, []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            x = json.loads(line)
            if x.get("kind") == "meta":
                meta = x
            else:
                out.append(x)
    return meta, out


def logged_days(root, now=None):
    """Complete UTC days from FIRST_DAY that have a tasks.csv (today is not complete)."""
    today = datetime.fromtimestamp(now or _now(), timezone.utc).strftime("%Y-%m-%d")
    return [d for d in replay.days(root, FIRST_DAY, "") if d < today
            and (Path(root) / f"data/daily/{d}/tasks.csv").exists()]


def days_to_fetch(root, now=None):
    """Complete logged days without a final file (an .open file is fetched again)."""
    return [d for d in logged_days(root, now) if not (Path(root) / DIR / f"{d}.jsonl.gz").exists()]


def fetch(client, root, wanted, known, now):
    """Token lines for wanted = {address: {symbol, sight_ts, dex, pair_age, created}}, each sight -> +7 days."""
    pools = resolve(client, wanted, known)
    lines = []
    for a, tok in sorted(wanted.items(), key=lambda kv: kv[1]["sight_ts"]):
        pool, match, created = pools.get(a, (None, "none", None))
        start = _ts(tok["sight_ts"]) // CANDLE_S * CANDLE_S
        end = min(_ts(tok["sight_ts"]) + WINDOW_S, now)
        candles = ohlcv(client, pool, a, start, end) if pool else []
        lines.append({"address": a, "symbol": tok["symbol"], "sight_ts": tok["sight_ts"], "pool": pool,
                      "match": match, "pool_created": _iso(created) if created else None,
                      "start": start, "end": end, "candles": candles})
    return lines


def fetch_day(client, root, day, now=None):
    """Fetch one sight day; writes the final file when every window has closed, else the .open file. Returns
    (path, n tokens)."""
    if not DAY_RE.fullmatch(day or ""):
        raise ValueError(f"bad day {day!r}")
    root, now = Path(root), now or _now()
    toks = sightings(root, day)
    known = sight_pairs(root, day)
    traded = [(r["book"], r["address"], r["ts"]) for r in replay._read(root / f"data/daily/{day}/trades.csv")
              if r["side"] == "BUY" and r["address"] in toks]
    for (book, a, t), p in state_pairs(root, traded).items():
        known.setdefault(a, p)
    lines = fetch(client, root, toks, known, now)
    closed = all(x["end"] >= _ts(toks[x["address"]]["sight_ts"]) + WINDOW_S for x in lines)
    final, opened = root / DIR / f"{day}.jsonl.gz", root / DIR / f"{day}.open.jsonl.gz"
    meta = {"day": day, "fetched_at": _iso(now), "window_s": WINDOW_S, "complete": closed, "calls": client.calls,
            "refused": client.refused, "pace_s": client.pace, "source": "geckoterminal ohlcv minute aggregate=5"}
    write(final if closed else opened, meta, lines)
    if closed and opened.exists():
        opened.unlink()
    return (final if closed else opened), len(lines)


# ---- ETH (BL-33 part 2) ----
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ETH_FIRST_DAY = "2026-10-30"  # the day before FIRST_DAY: the 24 h ETH return of the first decisions


def eth_pool(client):
    """The deepest WETH/USDC pool on Base (GeckoTerminal /tokens/WETH/pools), or None."""
    body = client.get(f"/networks/base/tokens/{WETH}/pools?page=1") or {}
    best = None
    for p in body.get("data") or []:
        rel = p.get("relationships") or {}
        sides = {((rel.get(k) or {}).get("data") or {}).get("id", "").lower() for k in ("base_token", "quote_token")}
        if f"base_{USDC}" in sides and f"base_{WETH}" in sides:
            x = _pool(p)
            if best is None or x["reserve"] > best["reserve"]:
                best = x
    return best


def eth_days(root, now=None):
    """Complete UTC days from ETH_FIRST_DAY to yesterday without an eth-<day>.jsonl.gz."""
    now = now or _now()
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    out, d = [], ETH_FIRST_DAY
    while d < today:
        if not (Path(root) / DIR / f"eth-{d}.jsonl.gz").exists():
            out.append(d)
        d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return out


def fetch_eth(client, root, days, now=None):
    """backtest/candles/eth-<day>.jsonl.gz for each complete day (written once): the day's 288 candles of the deepest
    WETH/USDC pool, priced in USD (token=WETH). Returns the paths written."""
    now, root, out = now or _now(), Path(root), []
    pool = eth_pool(client) if days else None
    for d in days:
        if not DAY_RE.fullmatch(d):
            raise ValueError(f"bad day {d!r}")
        path = root / DIR / f"eth-{d}.jsonl.gz"
        start = _ts(f"{d}T00:00:00Z")
        if path.exists() or start + 86400 > now or not pool:
            continue
        candles = ohlcv(client, pool["pool"], WETH, start, start + 86400)
        write(path, {"day": d, "fetched_at": _iso(now), "calls": client.calls, "refused": client.refused,
                     "pace_s": client.pace, "source": "geckoterminal ohlcv minute aggregate=5, WETH/USDC"},
              [{"address": WETH, "symbol": "WETH", "sight_ts": f"{d}T00:00:00Z", "pool": pool["pool"],
                "match": "exact", "pool_created": _iso(pool["created"]) if pool["created"] else None,
                "start": start, "end": start + 86400, "candles": candles}])
        out.append(path)
    return out


# ---- status.json (BL-43, display only) ----
STATUS = "backtest/status.json"
RESULTS = "backtest/results"
FILE_RE = re.compile(r"(eth-)?(\d{4}-\d{2}-\d{2})(\.open)?\.jsonl\.gz")


def twin_validation_status(root):
    """Totals of the latest twin-validation results (BL-33 part 1's honesty check), from its trades.csv: the trades
    closed in both the live log and the simulation, logged vs simulated P&L at the logged friction (stop fill
    "worse", the default), plus "level" stops and core friction. None when there is no such run."""
    runs = sorted(p for p in (Path(root) / RESULTS).glob("*-twin-validation") if (p / "trades.csv").exists())
    if not runs:
        return None
    d = runs[-1]
    rows = replay._read(d / "trades.csv")
    summary = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    closed = [r for r in rows if r.get("status") == "closed" and r.get("logged_pnl") and r.get("pnl_logged_fric")]
    total = lambda k: round(sum(float(r[k]) for r in closed if r.get(k)), 2)
    count = lambda s: sum(r.get("status") == s for r in rows)
    dev = (summary.get("split") or {}).get("dev") or []
    return {"run": d.name, "path": f"{RESULTS}/{d.name}/trades.csv", "days": [dev[0], dev[-1]] if dev else None,
            "trades": len(rows), "closed_both": len(closed), "logged_usd": total("logged_pnl"),
            "simulated_usd": total("pnl_logged_fric"), "simulated_level_usd": total("pnl_logged_fric_level"),
            "simulated_core_usd": total("pnl_core"), "no_fill": count("no_fill"), "no_data": count("no_data"),
            "pending": count("pending"), "tried": len(rows) - count("no_data")}


def status(root):
    """What candle files exist (no directory listing on the site): token days (final or open, tokens, fetched_at),
    ETH days, and the twin-validation totals."""
    root, toks, eth = Path(root), {}, []
    for p in sorted((root / DIR).glob("*.jsonl.gz")):
        m = FILE_RE.fullmatch(p.name)
        if not m:
            continue
        if m.group(1):
            eth.append(m.group(2))
            continue
        final = not m.group(3)
        if m.group(2) in toks and not final:
            continue  # a final file wins over a leftover .open
        meta, lines = read(p)
        toks[m.group(2)] = {"day": m.group(2), "state": "final" if final else "open", "tokens": len(lines),
                            "with_candles": sum(1 for x in lines if x.get("candles")),
                            "fetched_at": meta.get("fetched_at")}
    eth_meta = [read(root / DIR / f"eth-{d}.jsonl.gz")[0].get("fetched_at") for d in eth]
    # as_of = the newest fetch among the files, so the file changes only when a candle file does (no empty commits)
    stamps = [x for x in [t["fetched_at"] for t in toks.values()] + eth_meta if x]
    return {"as_of": max(stamps) if stamps else None, "card": "BL-43", "token_days": [toks[d] for d in sorted(toks)],
            "eth_days": eth, "twin_validation": twin_validation_status(root)}


def write_status(root):
    """Write backtest/status.json (never under data/; core/ and the tick never read it). Returns the path."""
    p = Path(root) / STATUS
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(status(root), indent=1) + "\n")
    tmp.replace(p)
    return p


def twin_buys(root, days):
    """The random twin's BUY rows on `days`, from trades.csv."""
    return [r for d in days for r in replay._read(Path(root) / f"data/daily/{d}/trades.csv")
            if r["book"] == "baseline" and r["side"] == "BUY"]


def fetch_twin(client, root, days, now=None):
    """twin-validation.jsonl.gz (written once): one line per twin BUY on `days` (every complete logged day; the
    validation itself reads development days only), window BUY -> +7 days; the pool from
    the buying tick's state.json (exact), else resolved."""
    root, now = Path(root), now or _now()
    path = root / DIR / "twin-validation.jsonl.gz"
    if path.exists():
        return path, 0
    buys = twin_buys(root, days)
    exact = state_pairs(root, [("baseline", b["address"], b["ts"]) for b in buys])
    lines, cache = [], {}
    sight = {}
    for d in days:
        sight.update({a: s for a, s in sightings(root, d).items() if a not in sight})
    need = {b["address"]: dict(sight.get(b["address"]) or {"dex": "na", "created": None},
                               symbol=b["symbol"], sight_ts=b["ts"])
            for b in buys if ("baseline", b["address"], b["ts"]) not in exact}
    resolved = resolve(client, need) if need else {}
    for b in buys:
        key = ("baseline", b["address"], b["ts"])
        pool, match, created = (exact[key], "exact", None) if key in exact else resolved.get(b["address"], (None, "none", None))
        start = _ts(b["ts"]) // CANDLE_S * CANDLE_S
        end = min(_ts(b["ts"]) + WINDOW_S, now)
        ck = (pool, b["address"])
        if pool and ck not in cache:  # one fetch per pool over the union of its trades' windows
            ws = [_ts(x["ts"]) for x in buys if x["address"] == b["address"]]
            lo, hi = min(ws) // CANDLE_S * CANDLE_S, min(max(ws) + WINDOW_S, now)
            cache[ck] = ohlcv(client, pool, b["address"], lo, hi)
        candles = [c for c in cache.get(ck, []) if start <= c[0] < end]
        lines.append({"address": b["address"], "symbol": b["symbol"], "sight_ts": b["ts"], "pool": pool,
                      "match": match, "pool_created": _iso(created) if created else None,
                      "start": start, "end": end, "candles": candles})
    write(path, {"day": ",".join(days), "fetched_at": _iso(now), "window_s": WINDOW_S, "calls": client.calls,
                 "refused": client.refused, "pace_s": client.pace, "purpose": "twin validation (BL-33)"}, lines)
    return path, len(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.candles", description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list-days", action="store_true")
    g.add_argument("--day")
    g.add_argument("--twin-validation", action="store_true")
    g.add_argument("--eth", action="store_true", help="every complete day without an eth-<day> file")
    g.add_argument("--status", action="store_true", help="rewrite backtest/status.json only (BL-43)")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    if a.list_days:
        print(" ".join(days_to_fetch(a.root)))
        return 0
    if a.status:
        print("status:", write_status(a.root))
        return 0
    c = Client()
    try:
        if a.eth:
            paths = fetch_eth(c, a.root, eth_days(a.root))
            print(f"eth candles: {len(paths)} days written calls={c.calls} refused={c.refused}")
            return 0
        if a.day:
            path, n = fetch_day(c, a.root, a.day)
        else:
            path, n = fetch_twin(c, a.root, logged_days(a.root))  # every logged day: fetching is not testing
        print(f"candles: {path} tokens={n} calls={c.calls} refused={c.refused}")
        return 0
    finally:
        write_status(a.root)  # BL-43: the site's Backtest lab card, also after a failed fetch


if __name__ == "__main__":
    sys.exit(main())
