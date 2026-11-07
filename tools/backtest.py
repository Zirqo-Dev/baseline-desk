"""Backtest (BL-33): replay a fixed spec on our own logs and 5-minute candles. Offline, by hand only, never on the tick
path; core/ never imports it. Stdlib. Costs come from core/friction.py (imported, never copied).

    python -m tools.backtest --split                       the split the next run would use
    python -m tools.backtest --validate-twin [--write]     the honesty check: the twin's logged trades on the candles
    python -m tools.backtest --spec FILE [--write]         a development run (development days only)
    python -m tools.backtest --spec FILE --holdout --write the sealed holdout run (BL-34, once)

Split (the owner, BL-33): logged days = complete UTC days from 2026-10-31 (tools/candles.py logged_days()); the
holdout = the last third, rounded up (split()); development runs and the validation load development days only and cut
every log row and candle at the start of the first holdout day (a position still open there is "pending"). Every run
prints the split it used. The first holdout run writes backtest/holdout.lock.json (spec name, spec sha256, time, the
frozen split, the result); with a lock, split() returns the frozen split, and a second holdout run is refused unless
docs/DECISIONS.md has an owner line "holdout-rerun <spec name> <first 12 of its sha256>" marked "(the owner)".

Point in time (Logs / View): a run reads tasks.csv (desk VERIFY rows of the logging set, with their trade keys) and
outcomes.csv (labels) only; every row keeps its ts and View(t) returns rows with ts <= t only. tokens_24h.json is not
read (it keeps the latest day only). Fields a spec may read: View.facts() (FIELDS).

Candle rules (pessimistic, simulate()): buy at the OPEN of the first candle starting at or after the decision time and
less than FILL_WAIT_S after it, else no_fill; candles carry only 5-minute windows with a trade, so no candle = no trade
= the pool price stays at the last close. Inside a candle the stops come first (hard stop, then the trail once a ladder
rung has sold): an open at or below the stop exits at the open (gap), a low at or below it exits at the worse of the
stop and the candle's close (STOP_FILL "worse"; "level" = at the stop); a ladder
rung fills at its price when the high reaches it and no stop hit that candle; one exit event per candle (one rule per
tick, as core/exits.py). The trail uses the peak of EARLIER candles only. The time stop (no rung sold, age >=
time_stop_min, gain < time_stop_min_pct) is checked at its due time on the last close, then at each later candle open;
a time exit with no trade fills at the last close. liq_pull reads the logged total liquidity as of each candle (logs
only: candles carry no liquidity). No pool or no candles = no_data, never a win; data ending with the position open =
pending. Physical time on candles, never swap order. Exits are fixed before entry from the spec.

Costs: core/friction.py per side (base + launchpad fee + impact 100 x side USD / qliq): the buy at the qliq as of the
decision, each sell at the last logged qliq after entry and up to the sale (else the entry qliq, else thin_pct).
"""
import argparse
import bisect
import csv
import fractions
import hashlib
import json
import math
import operator
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import config, friction
from core.book import ROOT
from core.report import _kv
from tools import candles as cdl
from tools import replay

LOCK = "backtest/holdout.lock.json"
RESULTS = "backtest/results"
DECISIONS = "docs/DECISIONS.md"
FILL_WAIT_S = 300
HORIZONS = (1, 6, 24)
LABEL_KEYS = ("price", "chg_pct", "liq", "qliq", "best_qliq", "dead", "gone", "pair_b1", "pair_s1", "pair_b24",
              "pair_s24")
VERIFY_KEYS = ("qliq", "liq", "b1", "s1", "b24", "s24")
FIELDS = (["qliq", "liq", "b1", "s1", "b24", "s24", "dex", "symbol", "pool_age_h", "token_age_h", "since_sight_h",
           "sight_qliq", "first_price", "last_qliq", "last_liq", "verdict"]
          + [f"l{h}_{k}" for h in HORIZONS for k in LABEL_KEYS]
          + ["l24_depth", "match", "pool_age_gt_h", "ret_6h_pct", "eth_ret_6h_pct", "eth_ret_24h_pct"])  # BL-33 p2
OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq,
       "!=": operator.ne, "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b}
OPTIONAL_OFF = ("time_stop_min", "time_stop_min_pct", "liq_drop_pct")  # null = that exit is off (BL-33 part 2)
EXIT_KEYS = ("hard_stop_pct", "ladder", "trail_pct", "time_stop_min", "time_stop_min_pct", "liq_drop_pct")


def _ts(iso):
    return cdl._ts(iso)


def _iso(t):
    return cdl._iso(t)


def _day_start(day):
    return _ts(f"{day}T00:00:00Z")


# ---- split and lock ----
def load_lock(root):
    p = Path(root) / LOCK
    return json.loads(p.read_text()) if p.exists() else None


def spec_days(spec):
    """(dev_last_day, [holdout days]) of a spec's own split (BL-33 part 2), or None."""
    sp = (spec or {}).get("split")
    if not sp:
        return None
    first, last = sp["holdout_days"]
    days, d = [], first
    while d <= last:
        days.append(d)
        d = _iso(_day_start(d) + 86400)[:10]
    return sp["dev_last_day"], days


def split(root, now=None, spec=None):
    """(development days, holdout days). With a lock: the split it froze. With a spec that carries its own split
    (BL-34): development = complete logged days up to its dev_last_day, holdout = its holdout days (all of them, logged
    or not yet). Else the last third, rounded up, of the complete logged days is the holdout."""
    lock = load_lock(root)
    if lock:
        return list(lock["split"]["dev"]), list(lock["split"]["holdout"])
    days = cdl.logged_days(root, now)
    own = spec_days(spec)
    if own:
        last, hold = own
        return [d for d in days if d <= last and d < hold[0]], hold
    h = math.ceil(len(days) / 3)
    return days[:len(days) - h], days[len(days) - h:]


def split_line(dev, hold, frozen, own=False):
    how = (" FROZEN by " + LOCK if frozen else " (the spec's own split)" if own
           else " (last third rounded up, not frozen)")
    return (f"split: development {dev[0] if dev else '-'} .. {dev[-1] if dev else '-'} ({len(dev)} days), "
            f"holdout {hold[0] if hold else '-'} .. {hold[-1] if hold else '-'} ({len(hold)} days){how}")


def override(root, spec):
    """True when docs/DECISIONS.md has an owner line allowing a second holdout run of this exact spec."""
    p = Path(root) / DECISIONS
    if not p.exists():
        return False
    tag = f"holdout-rerun {spec['name']} {spec['sha256'][:12]}"
    return any(tag in line and "(the owner)" in line for line in p.read_text().splitlines())


def holdout_refusal(root, spec, now=None):
    """"" when a holdout run of `spec` may run now, else the reason it is refused."""
    if load_lock(root) and not override(root, spec):
        return (f"refused: {LOCK} exists (one holdout run); a second needs a docs/DECISIONS.md owner line "
                f"'holdout-rerun {spec['name']} {spec['sha256'][:12]} ... (the owner)'")
    own = spec_days(spec)
    if own:
        logged = set(cdl.logged_days(root, now))
        missing = [d for d in own[1] if d not in logged]
        if missing:
            return f"refused: holdout days not complete yet: {' '.join(missing)}"
    return ""


def write_lock(root, spec, dev, hold, now, result, passed=None):
    """The first holdout run creates the lock (spec, sha256, time, frozen split, result); an allowed rerun (an owner
    line in DECISIONS) is appended under "reruns", never replacing the first. BL-43 (display only): "pass" = the
    pass_tests() answer (tests and verdict) of a decision spec, so the site's BL-34 card reads them from the lock;
    null for a spec without pass tests. It changes no number of the run."""
    p = Path(root) / LOCK
    lock = load_lock(root)
    if lock is None:
        lock = {"spec": spec["name"], "sha256": spec["sha256"], "ran_at": _iso(now),
                "split": {"dev": dev, "holdout": hold}, "result": result, "pass": passed, "reruns": []}
    else:
        lock.setdefault("reruns", []).append({"spec": spec["name"], "sha256": spec["sha256"], "ran_at": _iso(now),
                                              "result": result, "pass": passed})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(lock, indent=1) + "\n")
    return p


# ---- point-in-time logs ----
class Logs:
    """Rows of the given days, each with its ts; nothing at or after `cutoff` (unix s) is loaded.
    rows[address] = sorted [(t, kind, data)], kind "verify" (desk VERIFY row of the logging set) or "label"."""

    def __init__(self, root, days, cutoff=None):
        root = Path(root)
        self.rows, self.cutoff = {}, cutoff
        for d in days:
            for r in replay._read(root / f"data/daily/{d}/tasks.csv"):
                if replay.past_gate(r):
                    kv = _kv(r["detail"])
                    self._add(r["address"], r["ts"], "verify",
                              dict(kv, symbol=r["symbol"], verdict=r["reason"] or r["action"],
                                   first_seen=kv.get("first_seen", r["ts"])))
            for r in replay._read(root / f"data/daily/{d}/outcomes.csv"):
                self._add(r["address"], r["ts"], "label", r)
        for v in self.rows.values():
            v.sort(key=lambda x: x[0])
        self.times = {a: [x[0] for x in v] for a, v in self.rows.items()}

    def _add(self, address, iso, kind, data):
        t = _ts(iso)
        if self.cutoff is not None and t >= self.cutoff:
            return
        self.rows.setdefault(address, []).append((t, kind, data))

    def asof(self, t):
        return View(self, t)


class View:
    """The logs as known at time t: rows with ts <= t only."""

    def __init__(self, logs, t):
        self.logs, self.t = logs, t

    def rows(self, address):
        i = bisect.bisect_right(self.logs.times.get(address, []), self.t)
        out = self.logs.rows.get(address, [])[:i]
        assert all(x[0] <= self.t for x in out)  # the leak guard
        return out

    def tokens(self):
        return sorted(a for a, ts in self.logs.times.items() if ts and ts[0] <= self.t)

    def facts(self, address):
        """The spec's fields as of t (FIELDS); a field with no logged number is absent (a missing number is a skip)."""
        rows = self.rows(address)
        ver = [d for _, k, d in rows if k == "verify"]
        if not ver:
            return {}
        last, first = ver[-1], ver[0]
        f = {"symbol": last.get("symbol", ""), "dex": last.get("dex", "na"), "verdict": last.get("verdict", "")}
        for k in VERIFY_KEYS:
            v = replay._num(last.get(k))
            if v is not None:
                f[k] = v
        vt = [t for t, k, _ in rows if k == "verify"]
        for key, out in (("pair_age", "pool_age_h"), ("token_age", "token_age_h")):
            aged = [(t, replay._num(d.get(key))) for t, d in zip(vt, ver) if replay._num(d.get(key)) is not None]
            if aged:
                t0, age = aged[-1]
                f[out] = (age * 60 + (self.t - t0)) / 3600
        sight = _ts(last.get("first_seen") or _iso(vt[-1]))
        f["since_sight_h"] = (self.t - sight) / 3600
        sq = replay._num(next((d.get("qliq") for t, d in zip(vt, ver) if t >= sight), None))
        if sq is not None:
            f["sight_qliq"] = sq
        labels = [(t, d) for t, k, d in rows if k == "label" and d.get("first_seen") == last.get("first_seen")]
        for _, d in labels:
            h = str(d.get("horizon", "")).rstrip("h")
            for k in LABEL_KEYS:
                v = replay._num(d.get(k))
                if v is not None:
                    f[f"l{h}_{k}"] = v
            fp = replay._num(d.get("first_price"))
            if fp is not None:
                f["first_price"] = fp
        depth = f.get("l24_best_qliq", f.get("l24_qliq"))  # BL-34: the +24 h real depth, best_qliq else qliq
        if depth is not None:
            f["l24_depth"] = depth
        q = self.last(address, "qliq")
        if q is not None:
            f["last_qliq"] = q
        lq = self.last(address, "liq")
        if lq is not None:
            f["last_liq"] = lq
        return f

    def last(self, address, key, after=None):
        """The latest logged `key` (qliq: also a label's best_qliq) with after < ts <= t, else None."""
        for t, kind, d in reversed(self.rows(address)):
            if after is not None and t <= after:
                return None
            for k in ((key, "best_qliq") if key == "qliq" and kind == "label" else (key,)):
                v = replay._num(d.get(k))
                if v is not None:
                    return v
        return None


# ---- specs ----
def load_spec(path):
    """A spec file (JSON): name, text, ticket_usd, universe [[field, op, value]], entry [[...]], exits (EXIT_KEYS,
    optional max_hold_h), match (pool classes allowed). sha256 = of the file's bytes."""
    raw = Path(path).read_bytes()
    spec = json.loads(raw)
    spec["sha256"] = hashlib.sha256(raw).hexdigest()
    return check_spec(spec)


def check_spec(spec):
    for k in ("name", "text", "ticket_usd", "universe", "entry", "exits"):
        if k not in spec:
            raise ValueError(f"spec: missing {k}")
    for cond in spec["universe"] + spec["entry"]:
        if len(cond) != 3 or cond[0] not in FIELDS or cond[1] not in OPS:
            raise ValueError(f"spec: bad condition {cond}")
        if isinstance(cond[2], dict) and cond[2].get("field") not in FIELDS:
            raise ValueError(f"spec: bad field reference {cond}")
    for k in EXIT_KEYS:
        if k not in spec["exits"]:
            raise ValueError(f"spec: exits missing {k}")
        if spec["exits"][k] is None and k not in OPTIONAL_OFF:
            raise ValueError(f"spec: exit {k} cannot be off")
    spec["exits"] = dict(spec["exits"], ladder=[[g, ladder_share(x)] for g, x in spec["exits"]["ladder"]])
    if spec.get("decision") not in (None, "l24"):
        raise ValueError(f"spec: unknown decision {spec.get('decision')}")
    spec.setdefault("match", list(cdl.QUALIFIED))
    spec.setdefault("sha256", hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest())
    return spec


def ladder_share(x):
    """A rung's share: a number, or an exact fraction string such as "1/3" (BL-34 sells 1/3, not 0.33)."""
    return float(fractions.Fraction(x)) if isinstance(x, str) else float(x)


def holds(conds, f):
    """Every [field, op, value] holds on facts f; a missing field fails (a missing number is a skip). A value
    {"field": name} compares with another field of f (missing = fails)."""
    for k, op, v in conds:
        if isinstance(v, dict):
            v = f.get(v["field"])
            if v is None:
                return False
        if f.get(k) is None or not OPS[op](f[k], v):
            return False
    return True


# ---- candles ----
class Candles:
    """Candle lines by address from candle files; get(address, t) = the line whose window covers t, cut at `cutoff`."""

    def __init__(self, paths, cutoff=None, match=None):
        self.lines, self.cutoff, self.match = {}, cutoff, set(match or cdl.MATCH)
        self.classes = {}
        for p in paths:
            _, lines = cdl.read(p)
            for x in lines:
                self.lines.setdefault(x["address"], []).append(x)

    def line(self, address, t):
        """The candle line whose 7-day window covers t (pool identity and match only; reads no candle), or None.
        Coverage is by the window (start .. start + 7 days), never by how far the candles reach."""
        for x in self.lines.get(address, []):
            if x["start"] <= t < x["start"] + cdl.WINDOW_S or x["start"] <= t < x["end"] + cdl.CANDLE_S:
                return x
        return None

    def get(self, address, t):
        x = self.line(address, t)
        if x is None:
            return None, "none", None
        if x["match"] not in self.match or not x.get("pool"):
            return None, x["match"], None
        end = min(x["end"], self.cutoff) if self.cutoff else x["end"]
        return [c for c in x["candles"] if c[0] < end], x["match"], end


DAY_FILE = __import__("re").compile(r"(\d{4}-\d{2}-\d{2})(\.open)?\.jsonl\.gz")


def candle_paths(root, days=None):
    """Token day files (a final file wins over its .open file); never eth-*.jsonl.gz. With `days`: those sight days
    only. twin-validation.jsonl.gz is read only by the validation."""
    d = Path(root) / cdl.DIR
    out = {}
    for p in sorted(d.glob("*.jsonl.gz")) if d.exists() else []:
        m = DAY_FILE.fullmatch(p.name)
        if m and (days is None or m.group(1) in days):
            if not m.group(2) or m.group(1) not in out:
                out[m.group(1)] = p
    return [out[k] for k in sorted(out)]


def candle_return(candles, t, hours):
    """100 x (price now / price then - 1) on candles [[start, o, h, l, c, v]]: price now = close of the last candle that
    ended at or before t, price then = close of the last candle that ended at or before t - hours. None when either is
    missing. Reads no candle that ends after t (the leak rule)."""
    now = then = None
    for c in candles:
        end = c[0] + cdl.CANDLE_S
        if end > t:
            break
        now = c[4]
        if end <= t - hours * 3600:
            then = c[4]
    if not now or not then:
        return None
    return (now / then - 1) * 100


class EthSeries:
    """ETH 5-minute candles from backtest/candles/eth-<day>.jsonl.gz (tools/candles.py --eth), cut at `cutoff`."""

    def __init__(self, root, days=None, cutoff=None):
        got = {}
        d = Path(root) / cdl.DIR
        for p in sorted(d.glob("eth-*.jsonl.gz")) if d.exists() else []:
            day = p.name[4:14]
            if days is not None and day not in days:
                continue
            for x in cdl.read(p)[1]:
                for c in x["candles"]:
                    if cutoff is None or c[0] + cdl.CANDLE_S <= cutoff:
                        got[c[0]] = c
        self.candles = [got[k] for k in sorted(got)]
        self.starts = [c[0] for c in self.candles]

    def ret(self, t, hours):
        i = bisect.bisect_right(self.starts, t - cdl.CANDLE_S)
        return candle_return(self.candles[:i], t, hours)

    def price(self, t):
        """ETH at time t: the close of the last candle that ended at or before t (hold-ETH uses it at both ends)."""
        i = bisect.bisect_right(self.starts, t - cdl.CANDLE_S)
        return self.candles[i - 1][4] if i else None

    def coverage(self):
        """{day: candles} of the series."""
        out = {}
        for c in self.candles:
            out[_iso(c[0])[:10]] = out.get(_iso(c[0])[:10], 0) + 1
        return out


# ---- fees ----
class CoreFees:
    """core/friction.py: base + launchpad fee + impact (BL-32)."""

    def __init__(self, launchpad="other"):
        self.lp = launchpad

    def buy(self, price, usd, qliq):
        return friction.buy_fill(price, usd, qliq, self.lp)

    def sell(self, price, qty, qliq):
        return friction.sell_fill(price, qty, qliq, self.lp)


class SpecFees:
    """A spec's friction (BL-34): core/friction.py with launchpad "other" (base + impact at the spec's fixed depth, no
    launchpad fee) + fee_pct on every side; never both a launchpad fee and fee_pct (ValueError)."""

    def __init__(self, fee_pct, depth, launchpad="other"):
        if fee_pct and friction.config.FRICTION["pool_fee_pct"].get((launchpad or "").lower(), 0.0):
            raise ValueError("fee_pct and a launchpad pool fee would stack")
        self.fee, self.depth, self.lp = float(fee_pct), depth, launchpad

    def buy(self, price, usd, qliq):
        pct = friction.side_pct(usd, self.depth, self.lp) + self.fee
        return price * (1 + pct / 100), pct

    def sell(self, price, qty, qliq):
        pct = min(friction.CAP_PCT, friction.side_pct(qty * price, self.depth, self.lp) + self.fee)
        return price * (1 - pct / 100), pct


class FixedFees:
    """Fixed percentages per side (the validation's "logged friction" P&L)."""

    def __init__(self, buy_pct, sell_pct):
        self.b, self.s = buy_pct, sell_pct

    def buy(self, price, usd, qliq):
        return price * (1 + self.b / 100), self.b

    def sell(self, price, qty, qliq):
        return price * (1 - self.s / 100), self.s


# ---- the simulator ----
STOP_FILL = "worse"  # a stop hit inside a candle fills at the worse of the stop level and the candle's close


def simulate(candles, data_end, t, exits, ticket, fees, qliq=None, qliq_at=None, liq=None, liq_at=None,
             stop_fill=STOP_FILL):
    """One position on 5-minute candles [[start, o, h, l, c, v]] (oldest first), decided at t (unix s); data_end = the
    last second the candles cover. qliq_at(t0, t1) / liq_at(t1) give the logged values (None when not known).
    stop_fill: "worse" (default) = a stop hit inside a candle (open above it, low at or below it) fills at
    min(stop level, the candle's close): a liquidity pull drops through the stop in one swap and recovers nothing, so the
    level alone flatters (the twin validation: USOE, NAFF, ADP, I O F); "level" = at the stop level.
    Returns {status: closed | pending | no_fill | no_data, entry_t, entry_price, entry_fill, sells, pnl, ...}."""
    if not candles:
        return {"status": "no_data"}
    first = next((c for c in candles if c[0] >= t), None)
    if first is None or first[0] - t >= FILL_WAIT_S:
        return {"status": "no_fill", "wait_s": None if first is None else first[0] - t}
    e = exits
    t_in = first[0]
    fill, pct_in = fees.buy(first[1], ticket, qliq)
    qty = orig = ticket / fill
    pos = {"qty": qty, "cost": ticket, "ladders": [], "peak": None, "last": first[1]}
    sells = []
    hard = fill * (1 + e["hard_stop_pct"] / 100)
    ts_on = e.get("time_stop_min") is not None  # null = no time stop (BL-34)
    ts_due = t_in + e["time_stop_min"] * 60 if ts_on else None
    max_hold = t_in + e["max_hold_h"] * 3600 if e.get("max_hold_h") else None
    ts_checked = False

    def sell(rule, when, price, q=None):
        q = pos["qty"] if q is None else min(q, pos["qty"])
        depth, src = friction.exit_depth(qliq_at(t_in, when) if qliq_at else None, qliq)
        out, pct = fees.sell(price, q, depth)
        share = q / pos["qty"]
        cost = pos["cost"] * share
        sells.append({"rule": rule, "t": when, "price": price, "qty": q, "fill": out, "pct": pct,
                      "pnl": q * out - cost, "qliq_src": src})
        pos["qty"] -= q
        pos["cost"] -= cost
        return pos["qty"] <= 1e-12 * orig

    def gain(price):
        return (price / fill - 1) * 100

    def timed(upto):
        """Time-based exits due before `upto` with no trade in between: priced at the last close."""
        nonlocal ts_checked
        if max_hold is not None and max_hold < upto:
            return sell("max_hold", max_hold, pos["last"])
        if ts_on and not pos["ladders"] and not ts_checked and ts_due < upto:
            ts_checked = True
            if gain(pos["last"]) < e["time_stop_min_pct"]:
                return sell("time_stop", ts_due, pos["last"])
        return False

    for c in candles:
        start, o, h, low, close = c[0], c[1], c[2], c[3], c[4]
        if start < t_in:
            continue
        if start >= data_end:
            break
        if start > t_in and timed(start):
            break
        if start > t_in:  # checks at this candle's open
            lq = liq_at(start) if liq_at else None
            if (e.get("liq_drop_pct") is not None and liq is not None and lq is not None
                    and lq <= liq * (1 - e["liq_drop_pct"] / 100)):
                sell("liq_pull", start, o)
                break
            if max_hold is not None and start >= max_hold:
                sell("max_hold", start, o)
                break
            if ts_on and not pos["ladders"] and start >= ts_due and gain(o) < e["time_stop_min_pct"]:
                ts_checked = True
                if sell("time_stop", start, o):
                    break
        stops = [("hard_stop", hard)]
        if pos["ladders"] and pos["peak"] is not None:
            stops.append(("trail", pos["peak"] * (1 - e["trail_pct"] / 100)))
        hit = None
        for rule, level in stops:  # stops first, the gap at the open
            if o <= level:
                hit = (rule, o)
                break
            if low <= level:
                hit = (rule, min(level, close) if stop_fill == "worse" else level)
                break
        if hit:
            sell(hit[0], start, hit[1])
            break
        for i, (g, share) in enumerate(e["ladder"]):
            if i not in pos["ladders"]:
                level = fill * (1 + g / 100)
                if h >= level:
                    pos["ladders"].append(i)
                    if sell(f"ladder_{i + 1}", start, level, orig * share):
                        break
                break
        if pos["qty"] <= 1e-12 * orig:
            break
        pos["peak"] = h if pos["peak"] is None else max(pos["peak"], h)  # used from the NEXT candle on
        pos["last"] = close
    if pos["qty"] > 1e-12 * orig:
        timed(data_end)
    closed = pos["qty"] <= 1e-12 * orig
    pnl = sum(s["pnl"] for s in sells)
    return {"status": "closed" if closed else "pending", "entry_t": t_in, "entry_price": first[1],
            "entry_fill": fill, "entry_pct": pct_in, "wait_s": t_in - t, "sells": sells, "pnl": pnl if closed else None,
            "pnl_so_far": pnl, "exit_t": sells[-1]["t"] if closed else None,
            "exit_rule": sells[-1]["rule"] if closed else None, "exit_price": sells[-1]["price"] if closed else None}


# ---- runs ----
def summarize(trades):
    """Per book: n, status counts, no_fill rate, closed net and net per trade, wins."""
    out = {}
    for book in sorted({t["book"] for t in trades}):
        mine = [t for t in trades if t["book"] == book]
        closed = [t for t in mine if t["status"] == "closed"]
        st = {s: sum(t["status"] == s for t in mine) for s in ("closed", "pending", "no_fill", "no_data")}
        net = sum(t["pnl"] for t in closed)
        tried = st["closed"] + st["pending"] + st["no_fill"]
        out[book] = dict(st, n=len(mine), no_fill_pct=round(100 * st["no_fill"] / tried, 1) if tried else None,
                         net_usd=round(net, 4), net_per_trade_usd=round(net / len(closed), 4) if closed else None,
                         wins=sum(t["pnl"] > 0 for t in closed))
    return out


def run_spec(root, spec, holdout=False, now=None):
    """The spec and its seeded random twin. Development: development days only, cut at the first holdout day.
    A spec with "decision" (BL-34) runs run_decision_spec()."""
    if spec.get("decision"):
        return run_decision_spec(root, spec, holdout, now)
    now = now or cdl._now()
    dev, hold = split(root, now)
    frozen = load_lock(root) is not None
    if holdout:
        why = holdout_refusal(root, spec)
        if why:
            raise PermissionError(why)
        days, cutoff, decide = dev + hold, None, set(hold)
    else:
        days, cutoff, decide = dev, (_day_start(hold[0]) if hold else None), set(dev)
    logs = Logs(root, days, cutoff)
    cs = Candles(candle_paths(root), cutoff, spec.get("match"))
    trades = []
    for a in sorted(logs.rows):
        for t in sorted({x[0] for x in logs.rows[a]}):
            if _iso(t)[:10] not in decide:
                continue
            f = logs.asof(t).facts(a)
            if f and holds(spec["universe"], f) and holds(spec["entry"], f):
                trades.append(sim_trade(spec["name"], a, t, f, spec, logs, cs))
                break
    trades.sort(key=lambda x: x["decision_t"])
    seed = int(spec["sha256"][:12], 16)
    twin = []
    for i, tr in enumerate(list(trades)):
        v = logs.asof(tr["decision_t"])
        pool = [a for a in v.tokens() if holds(spec["universe"], v.facts(a)) and cs.get(a, tr["decision_t"])[0]]
        if not pool:
            twin.append({"book": "twin", "address": "", "symbol": "", "decision_t": tr["decision_t"],
                         "status": "no_data", "match": "none", "pnl": None, "eligible": 0})
            continue
        a = random.Random(seed + i).choice(sorted(pool))
        x = sim_trade("twin", a, tr["decision_t"], v.facts(a), spec, logs, cs)
        x["eligible"] = len(pool)
        twin.append(x)
    allt = trades + twin
    return {"spec": spec["name"], "sha256": spec["sha256"], "mode": "holdout" if holdout else "development",
            "split": {"dev": dev, "holdout": hold, "frozen": frozen}, "ran_at": _iso(now),
            "summary": summarize(allt), "match": match_counts(allt), "trades": allt}


def sim_trade(book, a, t, f, spec, logs, cs):
    candles, match, end = cs.get(a, t)
    base = {"book": book, "address": a, "symbol": f.get("symbol", ""), "decision_t": t, "match": match}
    if not candles:
        return dict(base, status="no_data", pnl=None)
    r = simulate(candles, end, t, spec["exits"], spec["ticket_usd"], CoreFees(f.get("launchpad", "other")),
                 qliq=f.get("qliq"), qliq_at=lambda t0, t1: logs.asof(t1).last(a, "qliq", after=t0),
                 liq=f.get("liq"), liq_at=lambda t1: logs.asof(t1).last(a, "liq", after=t))
    return dict(base, **r)


def match_counts(trades):
    out = {}
    for t in trades:
        out.setdefault(t["book"], {}).setdefault(t["match"], 0)
        out[t["book"]][t["match"]] += 1
    return out


# ---- decision specs (BL-34) ----
def l24_points(logs, a):
    """The token's +24 h outcome-label times (one per sighting), oldest first."""
    return [t for t, k, d in logs.rows.get(a, []) if k == "label" and str(d.get("horizon")) == "24h"]


def base_facts(logs, cs, a, t):
    """Log facts as of t plus the pool match of the candle line covering t (no candle read): the universe's inputs.
    pool_age_gt_h = GeckoTerminal pool_created_at to t (a check, never a rule)."""
    f = logs.asof(t).facts(a)
    if not f:
        return f
    line = cs.line(a, t)
    f["match"] = line["match"] if line and line.get("pool") else "none"
    created = line.get("pool_created") if line else None
    if created:
        f["pool_age_gt_h"] = (t - _ts(created)) / 3600
    return f


def entry_facts(f, cs, eth, a, t):
    """The base facts plus the candle fields at t: ret_6h_pct of the token (candles that ended at or before t),
    eth_ret_6h_pct, eth_ret_24h_pct. A missing price leaves the field out."""
    f = dict(f)
    candles, _, _ = cs.get(a, t)
    r = candle_return(candles or [], t, 6)
    if r is not None:
        f["ret_6h_pct"] = r
    for h in (6, 24):
        e = eth.ret(t, h)
        if e is not None:
            f[f"eth_ret_{h}h_pct"] = e
    return f


def sim_spec_trade(book, a, t, f, spec, cs, eth):
    """One trade of a decision spec at t, priced with SpecFees at the token's +24 h depth; hold-ETH over the fills."""
    candles, match, end = cs.get(a, t)
    base = {"book": book, "address": a, "symbol": f.get("symbol", ""), "decision_t": t, "match": match,
            "depth": f.get("l24_depth")}
    if not candles:
        return dict(base, status="no_data", pnl=None)
    fees = SpecFees(spec["friction"]["fee_pct"], f.get("l24_depth"))
    kw = dict(qliq=f.get("l24_depth"))
    r = simulate(candles, end, t, spec["exits"], spec["ticket_usd"], fees, stop_fill=spec.get("stop_fill", STOP_FILL),
                 **kw)
    out = dict(base, **r)
    if r["status"] == "closed":
        e0, e1 = eth.price(r["entry_t"]), eth.price(r["exit_t"])
        out["eth_ret"] = (e1 / e0 - 1) if e0 and e1 else None
        out["ret"] = r["pnl"] / spec["ticket_usd"]
        lvl = simulate(candles, end, t, spec["exits"], spec["ticket_usd"], fees, stop_fill="level", **kw)
        out["pnl_level"] = lvl.get("pnl")
        closes = [[c[0], c[4], c[4], c[4], c[4], c[5]] if c[0] > r["entry_t"] else c for c in candles]
        out["pnl_close_only"] = simulate(closes, end, t, spec["exits"], spec["ticket_usd"], fees,
                                         stop_fill=spec.get("stop_fill", STOP_FILL), **kw).get("pnl")
    return out


def twin_pool(logs, cs, spec, t, decide_window_h):
    """Twin candidates at t, from what is known at t only: tokens whose +24 h decision fell in (t - window, t], whose
    universe facts as of t hold, with a qualified pool on the candle line (identity, not candles)."""
    lo = t - decide_window_h * 3600
    out = []
    for a in sorted(logs.rows):
        if not any(lo < x <= t for x in l24_points(logs, a)):
            continue
        f = base_facts(logs, cs, a, t)
        if f and f["match"] in spec["match"] and holds(spec["universe"], f):
            out.append(a)
    return out


def run_decision_spec(root, spec, holdout=False, now=None):
    """A spec with "decision": "l24" (BL-34). Development: development days only, cut at the first holdout day."""
    now = now or cdl._now()
    dev, hold = split(root, now, spec)
    frozen = load_lock(root) is not None
    if holdout:
        why = holdout_refusal(root, spec, now)
        if why:
            raise PermissionError(why)
        days, cutoff, decide = dev + hold, None, set(hold)
    else:
        days, cutoff, decide = dev, (_day_start(hold[0]) if hold else None), set(dev)
    logs = Logs(root, days, cutoff)
    cs = Candles(candle_paths(root, set(days)), cutoff, spec["match"])
    eth_days = {_iso(_day_start(d) - 86400)[:10] for d in days} | set(days)
    eth = EthSeries(root, None if holdout else eth_days, cutoff)
    seed = int(spec["sha256"][:12], 16)
    trades = []
    counts = dict.fromkeys(("decisions", "log_rules", "no_candle_file", "not_in_file", "match_not_qualified",
                            "universe", "no_ret", "no_eth", "entries"), 0)
    fetched = {DAY_FILE.fullmatch(p.name).group(1) for p in candle_paths(root)}
    for a in sorted(logs.rows):
        for t in l24_points(logs, a):
            if _iso(t)[:10] not in decide:
                continue
            counts["decisions"] += 1
            f = base_facts(logs, cs, a, t)
            if not (f and holds(spec["universe"], f)):
                break  # one decision per token: the first +24 h label decides
            counts["log_rules"] += 1
            if f["match"] not in spec["match"]:
                sight_day = _iso(t - int(f.get("since_sight_h", 24) * 3600))[:10]
                key = ("no_candle_file" if sight_day not in fetched else
                       "not_in_file" if f["match"] == "none" else "match_not_qualified")
                counts[key] += 1
                break
            counts["universe"] += 1
            ef = entry_facts(f, cs, eth, a, t)
            if "ret_6h_pct" not in ef:
                counts["no_ret"] += 1
            if "eth_ret_6h_pct" not in ef or "eth_ret_24h_pct" not in ef:
                counts["no_eth"] += 1
            if holds(spec["entry"], ef):
                counts["entries"] += 1
                trades.append(sim_spec_trade(spec["name"], a, t, ef, spec, cs, eth))
            break  # one trade per token: its first +24 h decision decides
    trades.sort(key=lambda x: x["decision_t"])
    twin = []
    for i, tr in enumerate(trades):
        t = tr["decision_t"]
        pool = twin_pool(logs, cs, spec, t, spec["twin"]["window_h"])
        if not pool:
            twin.append({"book": "twin", "address": "", "symbol": "", "decision_t": t, "status": "twin_skip",
                         "match": "none", "pnl": None, "eligible": 0})
            continue
        a = random.Random(seed + i).choice(pool)
        x = sim_spec_trade("twin", a, t, base_facts(logs, cs, a, t), spec, cs, eth)
        x["eligible"] = len(pool)
        twin.append(x)
    allt = trades + twin
    return {"spec": spec["name"], "sha256": spec["sha256"], "mode": "holdout" if holdout else "development",
            "split": {"dev": dev, "holdout": hold, "frozen": frozen, "own": True}, "ran_at": _iso(now), "counts": counts,
            "plumbing": plumbing(allt), "eth_coverage": eth.coverage(), "match": match_counts(allt),
            "pool_age": pool_age_check(allt, logs, cs), "trades": allt,
            "summary": summarize([t for t in allt if t["status"] != "twin_skip"]) if holdout else None,
            "pass": pass_tests(trades, twin, spec) if holdout else None,
            "by_pool_age": age_report(allt, cs, spec) if holdout and spec.get("report_pool_age") else None}


def age_report(trades, cs, spec):
    """Holdout only, never the verdict (BL-34 spec v2): per book and pool-age bucket at the decision (GeckoTerminal
    pool_created_at; spec "report_pool_age" = [[label, lo_h, hi_h], ...], "unknown" = no created time): n, closed,
    mean net per closed trade, and how many of the first bucket are under 24 h."""
    buckets = spec["report_pool_age"]
    out = {}
    for t in trades:
        if not t.get("address"):
            continue
        line = cs.line(t["address"], t["decision_t"])
        created = line.get("pool_created") if line else None
        age = (t["decision_t"] - _ts(created)) / 3600 if created else None
        label = "unknown"
        for name, lo, hi in buckets:
            if age is not None and lo is not None and (hi is None or age <= hi) and (age > lo or lo == 0):
                label = name
                break
        b = out.setdefault(t["book"], {}).setdefault(label, {"n": 0, "closed": 0, "net": 0.0, "under_24h": 0})
        b["n"] += 1
        if age is not None and age < 24:
            b["under_24h"] += 1
        if t["status"] == "closed":
            b["closed"] += 1
            b["net"] += t["pnl"]
    for book in out.values():
        for b in book.values():
            b["mean_net"] = b["net"] / b["closed"] if b["closed"] else None
    return out


def plumbing(trades):
    """Counts only (no P&L): per book n, closed, pending, no_fill, no_data, twin_skip."""
    out = {}
    for t in trades:
        b = out.setdefault(t["book"], dict.fromkeys(("n", "closed", "pending", "no_fill", "no_data", "twin_skip"), 0))
        b["n"] += 1
        b[t["status"]] = b.get(t["status"], 0) + 1
    return out


def pool_age_check(trades, logs, cs):
    """How many entries (both books) sit outside 24-72 h of GeckoTerminal pool age at the decision; unknown counted."""
    out = {"inside": 0, "outside": 0, "unknown": 0}
    for t in trades:
        if not t.get("address"):
            continue
        line = cs.line(t["address"], t["decision_t"])
        created = line.get("pool_created") if line else None
        if not created:
            out["unknown"] += 1
            continue
        age = (t["decision_t"] - _ts(created)) / 3600
        out["inside" if 24 <= age <= 72 else "outside"] += 1
    return out


def max_dd_pct(pnls_by_exit, book_usd):
    """Max drawdown (%) of book_usd + cumulative P&L, in the given order; drawdown = (peak - equity) / peak."""
    eq = peak = float(book_usd)
    dd = 0.0
    for x in pnls_by_exit:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0.0)
    return dd


def pass_tests(trades, twin, spec):
    """The five pass tests (holdout only) and the verdict: pass | inconclusive (< min_closed) | retired (fails 2, 3 or 4)
    | not passed: drawdown (only 5 fails; BL-34 spec v2: forward paper vs twin, no money)."""
    p = spec["pass"]
    closed = [t for t in trades if t["status"] == "closed"]
    tw = [t for t in twin if t["status"] == "closed"]
    mean = lambda xs: sum(xs) / len(xs) if xs else None
    m, mt = mean([t["pnl"] for t in closed]), mean([t["pnl"] for t in tw])
    paired = [t for t in closed if t.get("eth_ret") is not None]
    mr, me = mean([t["ret"] for t in paired]), mean([t["eth_ret"] for t in paired])
    dd = max_dd_pct([t["pnl"] for t in sorted(closed, key=lambda x: x["exit_t"])], p["book_usd"])
    tests = {"1_min_closed": len(closed) >= p["min_closed"], "2_net_positive": m is not None and m > 0,
             "3_beats_twin": m is not None and mt is not None and m > mt,
             "4_beats_hold_eth": mr is not None and me is not None and mr > me, "5_max_dd": dd < p["max_dd_pct"]}
    if not tests["1_min_closed"]:
        verdict = "inconclusive"
    elif not (tests["2_net_positive"] and tests["3_beats_twin"] and tests["4_beats_hold_eth"]):
        verdict = "retired"
    else:
        verdict = "pass" if tests["5_max_dd"] else "not passed: drawdown"  # v2: forward paper vs twin, no money
    return {"tests": tests, "verdict": verdict, "closed": len(closed), "mean_net": m, "twin_mean_net": mt,
            "mean_ret": mr, "hold_eth_mean_ret": me, "paired_eth": len(paired), "max_dd_pct": dd,
            "level_mean_net": mean([t["pnl_level"] for t in closed if t.get("pnl_level") is not None]),
            "close_only_mean_net": mean([t["pnl_close_only"] for t in closed if t.get("pnl_close_only") is not None])}


def render_plumbing(res):
    """Development run of a decision spec: counts only, never P&L, win rate or the pass tests (the owner, BL-33 p2)."""
    c = res["counts"]
    out = [split_line(res["split"]["dev"], res["split"]["holdout"], res["split"]["frozen"], res["split"].get("own")),
           f"spec {res['spec']} sha256 {res['sha256'][:12]} mode development (plumbing only: no P&L, no win rate, "
           f"no pass tests, no results file)",
           f"  decisions (+24 h labels on development days) {c['decisions']}; pass the log rules {c['log_rules']}; "
           f"of those, no pool because the sight day has no candle file yet {c['no_candle_file']}, token not in the "
           f"fetched file {c['not_in_file']}, pool match not qualified {c['match_not_qualified']}",
           f"  in the universe {c['universe']}; without a token 6 h return {c['no_ret']}; without ETH returns "
           f"{c['no_eth']}; entries {c['entries']}"]
    for book, b in res["plumbing"].items():
        out.append(f"  {book:28} n {b['n']} closed {b['closed']} pending {b['pending']} no_fill {b['no_fill']} "
                   f"no_data {b['no_data']} twin_skip {b['twin_skip']}")
    out.append("  ETH candles per day (288 = full): " + (", ".join(f"{d} {n}" for d, n in sorted(res["eth_coverage"].items()))
                                                       or "none"))
    pa = res["pool_age"]
    out.append(f"  pool age at the decision (GeckoTerminal pool_created_at, a check): inside 24-72 h {pa['inside']}, "
               f"outside {pa['outside']}, unknown {pa['unknown']}")
    return "\n".join(out)


def render_holdout(res):
    """The one holdout run: plumbing + the five pass tests + the always-reported lines."""
    p = res["pass"]
    out = [render_plumbing(res).replace("mode development (plumbing only: no P&L, no win rate, no pass tests, "
                                        "no results file)", "mode HOLDOUT")]
    for k, v in p["tests"].items():
        out.append(f"  test {k}: {'PASS' if v else 'FAIL'}")
    out.append(f"  verdict: {p['verdict']} (closed {p['closed']}; mean net {_f(p['mean_net'], '{:+.4f}')} USD vs twin "
               f"{_f(p['twin_mean_net'], '{:+.4f}')}; mean return {_f(p['mean_ret'], '{:+.4f}')} vs hold-ETH "
               f"{_f(p['hold_eth_mean_ret'], '{:+.4f}')} over {p['paired_eth']}; max drawdown {p['max_dd_pct']:.1f}%)")
    out.append(f"  always reported, never the verdict: stop fill level {_f(p['level_mean_net'], '{:+.4f}')}, tick-like "
               f"close-only exits {_f(p['close_only_mean_net'], '{:+.4f}')} USD per trade")
    for book, bs in (res.get("by_pool_age") or {}).items():
        out.append(f"  by pool age, {book}: " + "; ".join(
            f"{k} n {v['n']} closed {v['closed']} mean net {_f(v['mean_net'], '{:+.4f}')}"
            + (f" (under 24 h {v['under_24h']})" if v["under_24h"] else "") for k, v in bs.items()))
    return "\n".join(out)


# ---- the honesty check ----
def logged_lots(root, days):
    """The twin's logged lots bought on `days`: BUY + its SELLs from the trades.csv of those days only (tools/replay.py
    lots()); a lot still open at the end of the last day is open here (the holdout days are never read)."""
    trades = [r for d in days for r in replay._read(Path(root) / f"data/daily/{d}/trades.csv")]
    return replay.lots([r for r in trades if r["book"] == "baseline"], set(days))


def validate_twin(root, now=None):
    """Re-run the twin's logged trades on development days through simulate() with the same exits and ticket, and
    compare the simulated exit time, exit price and P&L with the logged ones, per trade."""
    now = now or cdl._now()
    dev, hold = split(root, now)
    cutoff = _day_start(hold[0]) if hold else None
    logs = Logs(root, dev, cutoff)
    cs = Candles([Path(root) / cdl.DIR / "twin-validation.jsonl.gz"], cutoff)
    tasks = [r for d in dev for r in replay._read(Path(root) / f"data/daily/{d}/tasks.csv")]
    eq = replay.entry_qliq(tasks)
    rows = []
    for lot in logged_lots(root, dev):
        b, sells = lot["buy"], lot["sells"]
        t = _ts(b["ts"])
        candles, match, end = cs.get(b["address"], t)
        q = replay._num(eq.get(("baseline", b["address"], b["ts"])))
        last = sells[-1] if sells else None
        lq = (float(last["fill_price"]) / (1 - float(last["friction_pct"]) / 100)) if last and float(last["friction_pct"]) < 100 else None
        row = {"ts": b["ts"], "symbol": b["symbol"], "address": b["address"], "match": match,
               "logged_price": float(b["intended_price"]), "logged_exit": last["reason"] if lot["closed"] else "open",
               "logged_exit_t": last["ts"] if lot["closed"] else None, "logged_exit_price": lq if lot["closed"] else None,
               "logged_pnl": sum(replay._num(s["pnl_usd"]) or 0 for s in sells) if lot["closed"] else None,
               "rules": "+".join(s["reason"] for s in sells) or "-"}
        if not candles:
            rows.append(dict(row, status="no_data"))
            continue
        kw = dict(qliq=q, qliq_at=lambda t0, t1, a=b["address"]: logs.asof(t1).last(a, "qliq", after=t0),
                  liq=replay._num(b["liq_usd"]), liq_at=lambda t1, a=b["address"], t=t: logs.asof(t1).last(a, "liq", after=t))
        core = simulate(candles, end, t, config.EXIT, float(b["usd"]), CoreFees(b["launchpad"]), **kw)
        sell_pct = float(last["friction_pct"]) if last else float(b["friction_pct"])
        old = simulate(candles, end, t, config.EXIT, float(b["usd"]),
                       FixedFees(float(b["friction_pct"]), min(sell_pct, 99.0)), **kw)
        lvl = simulate(candles, end, t, config.EXIT, float(b["usd"]),
                       FixedFees(float(b["friction_pct"]), min(sell_pct, 99.0)), stop_fill="level", **kw)
        row.update(status=core["status"], wait_s=core.get("wait_s"), pnl_logged_fric_level=lvl.get("pnl"))
        if core["status"] in ("closed", "pending"):
            row.update(sim_price=core["entry_price"], sim_exit=core["exit_rule"] or "open",
                       sim_exit_t=_iso(core["exit_t"]) if core["exit_t"] else None, sim_exit_price=core["exit_price"],
                       sim_rules="+".join(s["rule"] for s in core["sells"]) or "-",
                       pnl_core=core["pnl"], pnl_logged_fric=old["pnl"])
        rows.append(row)
    return {"mode": "validate-twin", "ran_at": _iso(now), "split": {"dev": dev, "holdout": hold,
            "frozen": load_lock(root) is not None}, "rows": rows}


# ---- printing and writing ----
def _f(x, fmt="{:.2f}"):
    return "na" if x is None else fmt.format(x)


def render_validation(v):
    rows = v["rows"]
    out = [split_line(v["split"]["dev"], v["split"]["holdout"], v["split"]["frozen"]),
           "Twin validation (BL-33): the twin's logged trades on development days, simulated on 5-minute candles with "
           "the same exits (core/config.py EXIT) and ticket",
           "  entry% = simulated entry open vs the logged BUY quote; exit_dt = simulated - logged exit time (min);"
           " pnl_old = simulated with the logged friction %, pnl_new = core friction (BL-32)",
           f"  {'buy ts':20} {'symbol':10} {'match':6} {'status':8} {'entry%':>7} {'logged exit':14} {'sim exit':14} "
           f"{'exit_dt':>7} {'exit%':>7} {'pnl':>6} {'pnl_old':>7} {'pnl_new':>7}"]
    for r in rows:
        ent = (r["sim_price"] / r["logged_price"] - 1) * 100 if r.get("sim_price") else None
        dt = ((_ts(r["sim_exit_t"]) - _ts(r["logged_exit_t"])) / 60
              if r.get("sim_exit_t") and r.get("logged_exit_t") else None)
        ex = ((r["sim_exit_price"] / r["logged_exit_price"] - 1) * 100
              if r.get("sim_exit_price") and r.get("logged_exit_price") else None)
        out.append(f"  {r['ts']:20} {r['symbol'][:10]:10} {r['match']:6} {r['status']:8} {_f(ent, '{:+.1f}'):>7} "
                   f"{r['logged_exit'][:14]:14} {(r.get('sim_exit') or '-')[:14]:14} {_f(dt, '{:+.0f}'):>7} "
                   f"{_f(ex, '{:+.1f}'):>7} {_f(r['logged_pnl'], '{:+.2f}'):>6} {_f(r.get('pnl_logged_fric'), '{:+.2f}'):>7} "
                   f"{_f(r.get('pnl_core'), '{:+.2f}'):>7}")
    st = {}
    for r in rows:
        st[r["status"]] = st.get(r["status"], 0) + 1
    both = [r for r in rows if r.get("pnl_core") is not None and r["logged_pnl"] is not None]
    tried = sum(st.get(s, 0) for s in ("closed", "pending", "no_fill"))
    out.append(f"  trades {len(rows)}: " + ", ".join(f"{k} {v}" for k, v in sorted(st.items()))
               + f"; no_fill rate {_f(100 * st.get('no_fill', 0) / tried if tried else None, '{:.1f}')}%")
    m = {}
    for r in rows:
        m[r["match"]] = m.get(r["match"], 0) + 1
    out.append("  match: " + ", ".join(f"{k} {m[k]}" for k in cdl.MATCH if k in m))
    if both:
        same = sum(r["sim_exit"] == r["logged_exit"] for r in both)
        out.append(f"  closed in both: {len(both)}; same exit rule {same}; logged P&L {sum(r['logged_pnl'] for r in both):+.2f}"
                   f" USD, simulated at the logged friction {sum(r['pnl_logged_fric'] for r in both):+.2f} "
                   f"(stop at its level instead: {sum(r['pnl_logged_fric_level'] or 0 for r in both):+.2f}), "
                   f"at core friction {sum(r['pnl_core'] for r in both):+.2f}")
    return "\n".join(out)


def render_run(res):
    out = [split_line(res["split"]["dev"], res["split"]["holdout"], res["split"]["frozen"]),
           f"spec {res['spec']} sha256 {res['sha256'][:12]} mode {res['mode']}"]
    for book, s in res["summary"].items():
        out.append(f"  {book:10} n {s['n']} closed {s['closed']} pending {s['pending']} no_fill {s['no_fill']} "
                   f"({_f(s['no_fill_pct'], '{:.1f}')}%) no_data {s['no_data']} net {s['net_usd']:+.2f} USD "
                   f"per trade {_f(s['net_per_trade_usd'], '{:+.4f}')} wins {s['wins']}")
    for book, m in res["match"].items():
        out.append(f"  match {book}: " + ", ".join(f"{k} {m[k]}" for k in cdl.MATCH if k in m))
    return "\n".join(out)


def write_results(root, name, res, rows):
    d = Path(root) / RESULTS / f"{res['ran_at'].replace(':', '')}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({k: v for k, v in res.items() if k not in ("trades", "rows")},
                                               indent=1, default=str) + "\n")
    keys = sorted({k for r in rows for k in r if k != "sells"})
    with (d / "trades.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.backtest", description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--split", action="store_true")
    g.add_argument("--validate-twin", action="store_true")
    ap.add_argument("--spec")
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    if not (a.split or a.validate_twin or a.spec):
        ap.error("one of --split, --validate-twin or --spec is required")
    if a.split:
        sp = load_spec(a.spec) if a.spec else None
        dev, hold = split(a.root, spec=sp)
        print(split_line(dev, hold, load_lock(a.root) is not None, bool(spec_days(sp))))
        return 0
    if a.validate_twin:
        v = validate_twin(a.root)
        print(render_validation(v))
        if a.write:
            print("results:", write_results(a.root, "twin-validation", v, v["rows"]))
        return 0
    spec = load_spec(a.spec)
    if a.holdout and not a.write:
        print("refused: a holdout run must --write its result and lock")
        return 2
    try:
        res = run_spec(a.root, spec, holdout=a.holdout)
    except PermissionError as e:
        print(e)
        return 2
    if spec.get("decision") and not a.holdout:
        print(render_plumbing(res))  # never P&L on development days of a frozen spec, never a results file
        return 0
    print(render_holdout(res) if spec.get("decision") else render_run(res))
    if a.write:
        print("results:", write_results(a.root, spec["name"], res, res["trades"]))
    if a.holdout:
        print("lock:", write_lock(a.root, spec, res["split"]["dev"], res["split"]["holdout"],
                                  _ts(res["ran_at"]), res["summary"], res.get("pass")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
