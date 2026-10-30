"""Replay harness (BL-29): evidence for the Nov 6 review. Offline, by hand only, never on the tick path.

    python -m tools.replay [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--root DIR]

Reads data/daily/*/tasks.csv, trades.csv and outcomes.csv only. No network, writes nothing (stdout only).
It shows evidence; it never picks a rule (DECISIONS 2026-10-30, BL-29).

Unit = one sighting: a desk token past the gate (a VERIFY row without goplus=skipped and not a gate kill), keyed by
address + first_seen (the row's first_seen=, else its ts, as core/labels.py does). A sighting is in the window when its
first past-gate row is. It is bucketed by its FIRST VERIFY row that carries qliq (BL-28): qliq na / <1k / 1k-5k /
5k-25k / >=25k USD, and by that same row's s24 (BL-25, summed over every Base pair): na / 0 / 1-9 / >=10.

Table 1, outcomes per horizon x bucket, from outcomes.csv labels with |age_min - horizon| <= LABEL_WINDOW_MIN only
(drops late labels). Labels are read for the window plus FOLLOW_DAYS days after it; each file by its own header
(16 columns before BL-28, 17 with qliq). Per cell: n labels, gone %, median chg_pct (over the labels that have one,
n_chg), share with chg_pct <= -50 %, share with pair_s1 > 0 (BL-26: the deepest pair only, never mixed with BL-25 sums).
Fewer than MIN_N in a cell: "n too small" instead of the numbers.
Table 2, the desk's PASS rows in the window with their qliq, s24 and outcome labels.
Table 3, every desk and baseline trade bought in the window, re-priced with core/friction.py using qliq@entry instead of
the logged liquidity (option A: the exits use the entry qliq too; exits log no qliq). Entry qliq: the baseline BUY row,
or the desk PASS row at the same ts. size/qliq = the trade's USD size / qliq@entry, a plain ratio of two logged numbers,
not a friction model. friction.side_pct() is a step function (thin under 100k USD), so a re-pricing mostly shows 3% -> 5%;
it does not model price impact. "na" where no qliq was logged (every trade before BL-28).
"""
import argparse
import csv
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

from core import friction, labels
from core.book import ROOT
from core.report import GATE_KILLS, _kv

LABEL_WINDOW_MIN = 30
FOLLOW_DAYS = 2
MIN_N = 20
QLIQ_BUCKETS = ["na", "<1k", "1k-5k", "5k-25k", ">=25k"]
SELLS_BUCKETS = ["na", "0", "1-9", ">=10"]
NA = "na"


# ---- reading ----
def _read(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))  # each file by its own header (16 or 17 outcome columns)


def days(root, start, end):
    """Day folders from start to end (inclusive), as they exist."""
    out = sorted(p.name for p in (Path(root) / "data/daily").iterdir() if p.is_dir()) \
        if (Path(root) / "data/daily").exists() else []
    return [d for d in out if (not start or d >= start) and (not end or d <= end)]


def _plus(d, n):
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def load(root, start, end):
    """tasks rows of the window; trades and outcomes rows of the window and FOLLOW_DAYS days after it (later sells of
    a lot bought in the window, later labels of a sighting in the window)."""
    root = Path(root)
    win = days(root, start, end)
    last = _plus(win[-1], FOLLOW_DAYS) if win else end
    tasks, trades, outcomes = [], [], []
    for d in win:
        tasks += _read(root / f"data/daily/{d}/tasks.csv")
    for d in days(root, win[0] if win else start, last) if win else []:
        trades += _read(root / f"data/daily/{d}/trades.csv")
        outcomes += _read(root / f"data/daily/{d}/outcomes.csv")
    return win, tasks, trades, outcomes


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


# ---- sightings ----
def past_gate(r):
    return (r.get("book") == "desk" and r.get("stage") == "VERIFY" and "goplus=skipped" not in r["detail"].split()
            and r.get("reason") not in GATE_KILLS)


def qliq_bucket(q):
    q = _num(q)
    if q is None:
        return NA
    return "<1k" if q < 1_000 else "1k-5k" if q < 5_000 else "5k-25k" if q < 25_000 else ">=25k"


def sells_bucket(s):
    s = _num(s)
    if s is None:
        return NA
    return "0" if s == 0 else "1-9" if s < 10 else ">=10"


def sightings(tasks):
    """{(address, first_seen): {symbol, first_ts, qliq, s24, qb, sb}} for desk sightings past the gate."""
    out = {}
    for r in tasks:
        if not past_gate(r):
            continue
        kv = _kv(r["detail"])
        key = (r["address"], kv.get("first_seen", r["ts"]))
        s = out.setdefault(key, {"symbol": r["symbol"], "first_ts": r["ts"], "qliq": NA, "s24": NA, "tagged": False})
        if not s["tagged"] and "qliq" in kv:  # bucketed by the FIRST row that carries qliq
            s.update(qliq=kv["qliq"], s24=kv.get("s24", NA), tagged=True)
    for s in out.values():
        s["qb"], s["sb"] = qliq_bucket(s["qliq"]), (sells_bucket(s["s24"]) if s["tagged"] else NA)
    return out


def on_time(row):
    """A label within LABEL_WINDOW_MIN of its horizon."""
    h, age = _num(row.get("horizon", "").rstrip("h")), _num(row.get("age_min"))
    return h is not None and age is not None and abs(age - h * 60) <= LABEL_WINDOW_MIN


def label_index(outcomes):
    """{(address, first_seen, "1h"): row} for on-time labels."""
    return {(r["address"], r["first_seen"], r["horizon"]): r for r in outcomes if on_time(r)}


# ---- table 1 ----
def cell(rows):
    """n, gone %, median chg_pct (n_chg), share chg <= -50 %, share pair_s1 > 0; None for the stats under MIN_N."""
    n = len(rows)
    if n < MIN_N:
        return {"n": n, "small": True}
    gone = [r for r in rows if r.get("gone") == "1"]
    chg = [c for c in (_num(r.get("chg_pct")) for r in rows) if c is not None]
    s1 = [v for v in (_num(r.get("pair_s1")) for r in rows if r.get("gone") != "1") if v is not None]
    return {"n": n, "small": False, "gone": 100 * len(gone) / n, "n_chg": len(chg),
            "med": statistics.median(chg) if chg else None,
            "le50": 100 * sum(c <= -50 for c in chg) / len(chg) if chg else None,
            "s1": 100 * sum(v > 0 for v in s1) / len(s1) if s1 else None}


def outcome_table(sights, idx, by):
    """{horizon: {bucket: cell}} with by = "qb" or "sb"."""
    buckets = QLIQ_BUCKETS if by == "qb" else SELLS_BUCKETS
    out = {}
    for h in labels.HORIZONS:
        hz = f"{h}h"
        out[hz] = {b: cell([idx[(a, fs, hz)] for (a, fs), s in sights.items() if s[by] == b and (a, fs, hz) in idx])
                   for b in buckets}
    return out


# ---- table 3 ----
def entry_qliq(tasks):
    """{(book, address, ts): qliq} from baseline BUY rows and desk PASS rows."""
    out = {}
    for r in tasks:
        kv = _kv(r["detail"])
        if "qliq" not in kv:
            continue
        if r["book"] == "baseline" and r["stage"] == "EXECUTE" and r["action"] == "BUY":
            out[("baseline", r["address"], r["ts"])] = kv["qliq"]
        elif r["book"] == "desk" and r["stage"] == "VERIFY" and r["action"] == "PASS":
            out[("desk", r["address"], r["ts"])] = kv["qliq"]
    return out


def reprice(buy, sells, qliq):
    """Re-priced P&L of one lot with friction from qliq (entry and exits, option A); None without a qliq."""
    q = _num(qliq)
    if q is None:
        return None
    usd, qty = float(buy["usd"]), float(buy["qty"])
    fill_in, pct_in = friction.buy_fill(float(buy["intended_price"]), q, buy["launchpad"])  # BUY intended = the quote
    new_qty, pnl = usd / fill_in, 0.0
    for s in sells:
        share = float(s["qty"]) / qty
        quote = float(s["fill_price"]) / (1 - float(s["friction_pct"]) / 100)  # as core/report.py: fill before friction
        fill_out, _ = friction.sell_fill(quote, q, s["launchpad"])
        pnl += share * new_qty * fill_out - share * usd
    return {"pnl": pnl, "pct_in": pct_in}


def lots(trades, win):
    """[{buy, sells, closed}] per book and address, for BUYs on a window day, in order."""
    open_, out = {}, []
    for r in trades:
        k = (r["book"], r["address"])
        if r["side"] == "BUY":
            open_.pop(k, None)
            if r["ts"][:10] in win:
                open_[k] = {"buy": r, "sells": [], "closed": False}
                out.append(open_[k])
        elif k in open_:
            open_[k]["sells"].append(r)
            if not r["reason"].startswith("ladder_"):  # every rule but the ladder ends the position
                open_.pop(k)["closed"] = True
    return out


def trade_table(tasks, trades, win):
    eq = entry_qliq(tasks)
    rows = []
    for lot in lots(trades, win):
        buy, sells, closed = lot["buy"], lot["sells"], lot["closed"]
        q = eq.get((buy["book"], buy["address"], buy["ts"]), NA)
        r = reprice(buy, sells, q)
        logged = sum(_num(s["pnl_usd"]) or 0.0 for s in sells)
        size = 100 * float(buy["usd"]) / _num(q) if _num(q) else None
        rows.append({"ts": buy["ts"], "book": buy["book"], "symbol": buy["symbol"], "usd": float(buy["usd"]),
                     "liq": _num(buy["liq_usd"]), "qliq": q, "size_pct": size,
                     "pct_logged": _num(buy["friction_pct"]), "pct_new": r["pct_in"] if r else None,
                     "pnl_logged": logged, "pnl_new": r["pnl"] if r else None,
                     "exits": "+".join(s["reason"] for s in sells) or "-", "closed": closed})
    return rows


# ---- printing ----
def _f(x, fmt="{:.1f}", na=NA):
    return na if x is None else fmt.format(x)


def render(root, start, end):
    win, tasks, trades, outcomes = load(root, start, end)
    sights = sightings(tasks)
    idx = label_index(outcomes)
    out = []
    p = out.append
    p(f"Baseline replay (BL-29) | window {win[0] if win else start} .. {win[-1] if win else end} "
      f"| labels within +/-{LABEL_WINDOW_MIN} min of +1/+6/+24 h | evidence only, no rule change")
    p(f"unit: one sighting (address + first_seen) of a desk token past the gate; {len(sights)} sightings, "
      f"{sum(s['tagged'] for s in sights.values())} with a qliq row; {len(idx)} on-time labels; "
      f"cells under {MIN_N}: n too small")
    for by, title, buckets in (("qb", "qliq at first tagged check (USD)", QLIQ_BUCKETS),
                               ("sb", "s24 at first tagged check (BL-25, all Base pairs)", SELLS_BUCKETS)):
        p("")
        p(f"Table 1{'a' if by == 'qb' else 'b'}: outcomes by {title}")
        p(f"  {'h':4} {'bucket':7} {'n':>5} {'gone%':>6} {'n_chg':>6} {'med_chg%':>9} {'<=-50%':>7} {'pair_s1>0%':>10}")
        t = outcome_table(sights, idx, by)
        for hz, cells in t.items():
            for b in buckets:
                c = cells[b]
                if c["small"]:
                    p(f"  {hz:4} {b:7} {c['n']:>5}  n too small")
                else:
                    p(f"  {hz:4} {b:7} {c['n']:>5} {_f(c['gone']):>6} {c['n_chg']:>6} {_f(c['med']):>9} "
                      f"{_f(c['le50']):>7} {_f(c['s1']):>10}")
    p("")
    p("Table 2: desk PASS rows with their outcomes (chg_pct of on-time labels; gone = no Base pair)")
    p(f"  {'ts':20} {'symbol':12} {'qliq':>8} {'s24':>5}  {'+1h':>8} {'+6h':>8} {'+24h':>8}")
    passes = [r for r in tasks if r["book"] == "desk" and r["stage"] == "VERIFY" and r["action"] == "PASS"]
    for r in passes:
        kv = _kv(r["detail"])
        fs = kv.get("first_seen", r["ts"])
        cols = []
        for h in labels.HORIZONS:
            lab = idx.get((r["address"], fs, f"{h}h"))
            cols.append(NA if lab is None else "gone" if lab.get("gone") == "1" else lab.get("chg_pct") or NA)
        p(f"  {r['ts']:20} {r['symbol'][:12]:12} {kv.get('qliq', NA):>8} {kv.get('s24', NA):>5}  "
          + " ".join(f"{c:>8}" for c in cols))
    if not passes:
        p("  none")
    p("")
    p("Table 3: trades re-priced with friction from qliq@entry (entry and exits, option A), next to the logged P&L")
    p("  friction.side_pct() is a step function (thin under 100k USD): mostly 3% -> 5%, no price impact modelled.")
    p("  exits use the entry qliq (exits log none): understates exit friction if the pool thinned after entry.")
    p("  size/qliq = trade USD / qliq@entry: a plain ratio of two logged numbers, not a friction model.")
    p(f"  {'buy ts':20} {'book':8} {'symbol':10} {'usd':>6} {'liq':>9} {'qliq':>8} {'size/qliq%':>10} "
      f"{'fric%':>5} {'new%':>5} {'pnl':>7} {'new_pnl':>7}  exits")
    rows = trade_table(tasks, trades, set(win))
    for t in rows:
        p(f"  {t['ts']:20} {t['book']:8} {t['symbol'][:10]:10} {t['usd']:>6.2f} {_f(t['liq'], '{:.0f}'):>9} "
          f"{t['qliq']:>8} {_f(t['size_pct']):>10} {_f(t['pct_logged']):>5} {_f(t['pct_new']):>5} "
          f"{t['pnl_logged']:>+7.2f} {_f(t['pnl_new'], '{:+.2f}'):>7}  {t['exits']}{'' if t['closed'] else ' (open)'}")
    if not rows:
        p("  none")
    sized = [t for t in rows if t["size_pct"] is not None]
    p(f"  trades: {len(rows)}, with qliq: {len(sized)}; size above 10% of qliq: "
      f"{sum(t['size_pct'] > 10 for t in sized)}; size above 100% of qliq: {sum(t['size_pct'] > 100 for t in sized)}")
    for b in ("desk", "baseline"):
        mine = [t for t in rows if t["book"] == b]
        priced = [t for t in mine if t["pnl_new"] is not None]
        p(f"  {b}: {len(mine)} trades, logged P&L {sum(t['pnl_logged'] for t in mine):+.2f} USD; "
          f"re-priced {len(priced)}: logged {sum(t['pnl_logged'] for t in priced):+.2f} -> "
          f"{sum(t['pnl_new'] for t in priced):+.2f} USD"
          + (f" (n too small: {len(priced)} < {MIN_N})" if len(priced) < MIN_N else ""))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.replay", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", default="", help="first day YYYY-MM-DD (default: the first day there is)")
    ap.add_argument("--to", dest="end", default="", help="last day YYYY-MM-DD (default: the last day there is)")
    ap.add_argument("--root", default=str(ROOT), help="repo root holding data/ (default: this repo)")
    a = ap.parse_args(argv)
    print(render(a.root, a.start, a.end))
    return 0


if __name__ == "__main__":
    sys.exit(main())
