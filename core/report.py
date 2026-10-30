"""REPORT (BL-8): desk vs random baseline -> data/report.json.

Built inside the tick on the UTC day roll, for the day just closed (core/tick.py), before that
tick's exits, so values are the closing marks. Also by hand:  python -m core.report [root]
(day = today so far, values at the current marks; state.json is only read).

State key "report" in data/state.json:
  since       first day of BL-8: closes are counted from this day on (Gate B)
  due         closed day whose report is still to write ("" = none); a failed report stays due
  equity      {book: [{day, value_usd, cash_usd, open}]}, one entry per closed day, taken at the roll
  peak        {book: highest value seen at the end of a tick}
  max_dd_pct  {book: deepest fall from that peak, in %}, checked at the end of every tick

Values are LIQUIDATION values (CLAUDE.md section 6, paper must not flatter): cash + each open position
sold at its last quote minus the sell-side friction it would pay (friction.sell_fill on the last quote's
liquidity and the position's launchpad), the same for both books. value_usd, net_pnl, multiple,
equity_daily, desk_lead_usd and max_dd_pct all use it. The loss halt still uses Book.value() (last price).

From trades.csv: a close is a SELL that ends the position (every rule but ladder_1/ladder_2);
win/loss % is per position (all its SELL pnl over the BUY cost); stop_slip_pct is the mean of
(quote / intended - 1) x 100 over hard_stop and trail sells, friction left out (quote = fill
before friction). The desk side may be all zeros: zeros are shown, never invented numbers.
DECISIONS 2026-10-29, BL-8.

Machine cards (BL-23): after the money report, the same roll also writes report.json["machine"] and
data/tokens_24h.json, built only from data/daily/*/tasks.csv (no request), desk book only:
  last_tick       the last TICK row of the day: ts and its seen new checked dex_only goplus passed bought requests secs
  funnel          the day: scan = distinct tokens with a desk VERIFY row, gate = those with no gate kill,
                  safety = distinct tokens with a PASS row, buy = desk EXECUTE BUY rows, exits_open = desk open at the roll
  funnel_since    running totals of the daily scan/gate/safety/buy since "since" (token-days, not distinct tokens)
  kills           desk VERIFY SKIP rows of the day by full reason, split "gate" (goplus=skipped, liquidity,
                  fetch_error_x3, missing:<DexScreener field>) and "safety" (everything else); kills_since = running totals
  kill_log        the last 10 kills of the day, newest first
  runs, tick_secs TICK rows of the day vs expected (288 for a closed day), rate in %; median and max secs
  ticks_per_hour  TICK rows per UTC hour for the 48 hours ending at the roll, missing hours as 0
  tokens_24h.json {day, as_of, tokens}: the latest desk VERIFY or EXECUTE row per token of the day, overwritten
Running totals live in the "totals" state key ({through, funnel, kills}): each closed day is added once (through =
last day added), so a retry never double-counts; the first run backfills from "since" with the files there.
The cards run after the money report, in their own try in the tick (machine_due): a failure leaves report.json
with the money cards only, logs machine=error:<type>, retries next tick and ends the run red. DECISIONS 2026-10-30, BL-23.
"""
import copy
import csv
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config, friction, verify
from core.book import ROOT, Book, day, load_state, save_key, ts, utcnow

BOOKS = ["desk", "baseline"]
RULES = ["liq_pull", "hard_stop", "ladder_1", "ladder_2", "trail", "time_stop"]
SLIP_RULES = ["hard_stop", "trail"]
GATE_B = {"target": 100, "kill": 200}
REPORT = "data/report.json"
TOKENS = "data/tokens_24h.json"  # BL-23


def liq_value(book):
    """Cash + every open position sold now at its last quote, minus the friction that sale would pay."""
    total = book.cash
    for p in book.positions.values():
        fill, _ = friction.sell_fill(p["last_price"], p.get("last_liq_usd", p["entry_liq_usd"]), p["launchpad"])
        total += p["qty"] * fill
    return total


def _dd(peak, value):
    return (peak - value) / peak * 100 if peak > 0 else 0.0


# ---- state ----
def load(root, now):
    s = load_state(Path(root) / config.STATE).get("report") or {}
    s.setdefault("since", day(now))
    s.setdefault("due", "")
    s.setdefault("machine_due", "")  # BL-23: closed day whose machine cards are still to write
    for k in ("equity", "peak", "max_dd_pct"):
        s.setdefault(k, {})
    return s


def save(root, s):
    save_key(Path(root) / config.STATE, "report", s)


def on_roll(s, books, closed):
    """Day roll: keep each book's closing value for the day just closed and mark its report due."""
    for b in books:
        eq = s["equity"].setdefault(b.name, [])
        if not eq or eq[-1]["day"] != closed:
            eq.append({"day": closed, "value_usd": round(liq_value(b), 6), "cash_usd": round(b.cash, 6),
                       "open": len(b.positions)})
    s["due"] = closed


def track(s, books):
    """End of tick: running peak and max drawdown per book, at liquidation value."""
    for b in books:
        v = liq_value(b)
        peak = max(s["peak"].get(b.name, float(config.START_BALANCE)), v)
        s["peak"][b.name] = peak
        s["max_dd_pct"][b.name] = max(s["max_dd_pct"].get(b.name, 0.0), _dd(peak, v))


# ---- trades ----
def trades(root):
    rows = []
    for p in sorted((Path(root) / "data/daily").glob("*/trades.csv")):
        with p.open(newline="") as fh:
            rows += list(csv.DictReader(fh))
    return rows


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def book_stats(rows, name, since, upto):
    """Closes, win/loss and exits per rule for one book, SELLs from `since` to `upto` (days)."""
    lots, pcts, slips = {}, [], []
    by_rule = dict.fromkeys(RULES, 0)
    closes = closes_day = 0
    for r in rows:
        d = r["ts"][:10]
        if r["book"] != name or d > upto:
            continue
        a = r["address"]
        if r["side"] == "BUY":
            lots[a] = {"cost": float(r["usd"]), "pnl": 0.0}
            continue
        counted, rule, lot = d >= since, r["reason"], lots.get(a)
        if lot:
            lot["pnl"] += float(r["pnl_usd"] or 0)
        if counted:
            by_rule[rule] = by_rule.get(rule, 0) + 1
            if rule in SLIP_RULES and float(r["intended_price"] or 0) > 0:
                quote = float(r["fill_price"]) / (1 - float(r["friction_pct"]) / 100)
                slips.append((quote / float(r["intended_price"]) - 1) * 100)
        if rule.startswith("ladder_"):
            continue
        lots.pop(a, None)
        if counted:
            closes += 1
            closes_day += int(d == upto)
            if lot and lot["cost"] > 0:
                pcts.append(lot["pnl"] / lot["cost"] * 100)
    wins, losses = [p for p in pcts if p > 0], [p for p in pcts if p <= 0]
    return {"closes": closes, "closes_day": closes_day,
            "win_rate": round(len(wins) / len(pcts) * 100, 2) if pcts else 0.0,
            "avg_win_pct": round(_mean(wins), 2), "avg_loss_pct": round(_mean(losses), 2),
            "stop_slip_pct": round(_mean(slips), 2), "exits_by_rule": by_rule}


# ---- report ----
def build(root, now, report_day, s, books=None):
    """The report dict for report_day. Values from that day's closing entry, else the current liquidation value."""
    books = books or [Book(n, root) for n in BOOKS]
    rows, start = trades(root), float(config.START_BALANCE)
    out = {"as_of": ts(now), "day": report_day, "start_balance": start}
    for b in books:
        eq = s["equity"].get(b.name, [])
        close = next((e for e in eq if e["day"] == report_day), None)
        value = close["value_usd"] if close else liq_value(b)
        cash = close["cash_usd"] if close else b.cash
        n_open = close["open"] if close else len(b.positions)
        out[b.name] = {
            "value_usd": round(value, 2), "cash_usd": round(cash, 2),
            "net_pnl_usd": round(value - start, 2), "net_pnl_pct": round((value / start - 1) * 100, 2),
            "multiple": round(value / start, 4),
            "equity_daily": [{"day": e["day"], "value_usd": round(e["value_usd"], 2)}
                             for e in eq if e["day"] <= report_day],
            "open": n_open,
            "max_dd_pct": round(max(s["max_dd_pct"].get(b.name, 0.0),  # also the value shown, if it is the low
                                    _dd(max(s["peak"].get(b.name, start), value), value)), 2),
            **book_stats(rows, b.name, s["since"], report_day)}
    d, bl = out.pop("desk"), out.pop("baseline")
    out["desk_lead_usd"] = round(d["value_usd"] - bl["value_usd"], 2)
    out["gate_b"] = {"desk_closes": d["closes"], **GATE_B}
    out["summary"] = (f"{report_day}: desk {d['value_usd']:.2f} USD ({d['closes']} closes, {d['open']} open), "
                      f"baseline {bl['value_usd']:.2f} USD ({bl['closes']} closes, {bl['open']} open), "
                      f"desk lead {out['desk_lead_usd']:+.2f} USD. Gate B: {d['closes']}/{GATE_B['target']} "
                      f"desk closes (kill at {GATE_B['kill']} without beating the baseline).")
    out["desk"], out["baseline"] = d, bl  # top-level fields first, then the books
    return out


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write(root, report):
    _write(Path(root) / REPORT, json.dumps(report, indent=2))


def run_due(root, now, s, books):
    """In the tick: write the report for the closed day in s["due"], then clear it. Raises on failure (stays due).
    Marks the machine cards of that day due (BL-23); run_machine() adds them in its own try."""
    report = build(root, now, s["due"], s, books)
    write(root, report)
    s["machine_due"], s["due"] = s["due"], ""
    return report


# ---- machine cards (BL-23) ----
GATE_KILLS = {"liquidity", "fetch_error_x3"} | {f"missing:{k}" for k in verify.DEX_FIELDS}
FUNNEL = ["scan", "gate", "safety", "buy"]
LAST_TICK = ["seen", "new", "checked", "dex_only", "goplus", "passed", "bought", "requests", "secs"]
TICKS_PER_DAY = 288  # tick.yml every 5 min
KILL_LOG = 10


def tasks(root, d):
    p = Path(root) / config.TASKS.format(day=d)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _kv(detail):
    return dict(kv.split("=", 1) for kv in detail.split() if "=" in kv)


def _num(x):
    try:
        return int(x)
    except ValueError:
        return float(x)


def kill_stage(row):
    """'gate' when DexScreener settled the skip (BL-20), else 'safety'."""
    return "gate" if "goplus=skipped" in row["detail"].split() or row["reason"] in GATE_KILLS else "safety"


def _desk(rows, stage):
    return [r for r in rows if r["book"] == "desk" and r["stage"] == stage]


def day_counts(rows):
    """Funnel and kills of one day's tasks rows, desk book only."""
    v = _desk(rows, "VERIFY")
    kills, gate_killed = {"gate": {}, "safety": {}}, set()
    for r in v:
        if r["action"] != "SKIP":
            continue
        stage = kill_stage(r)
        kills[stage][r["reason"]] = kills[stage].get(r["reason"], 0) + 1
        if stage == "gate":
            gate_killed.add(r["address"])
    scan = {r["address"] for r in v}
    funnel = {"scan": len(scan), "gate": len(scan - gate_killed),
              "safety": len({r["address"] for r in v if r["action"] == "PASS"}),
              "buy": sum(1 for r in _desk(rows, "EXECUTE") if r["action"] == "BUY")}
    return {"funnel": funnel, "kills": kills}


def since_totals(root, s, upto, rows_upto=None):
    """Running funnel and kills from s["since"] through upto. A copy of s["totals"] plus the days after its
    "through" (the files that exist); the caller stores it once the cards are written."""
    t = copy.deepcopy(s.get("totals") or {"through": "", "funnel": dict.fromkeys(FUNNEL, 0),
                                          "kills": {"gate": {}, "safety": {}}})
    days = sorted(p.parent.name for p in (Path(root) / "data/daily").glob("*/tasks.csv"))
    for d in days:
        if d < s["since"] or d <= t["through"] or d > upto:
            continue
        c = day_counts(rows_upto if d == upto and rows_upto is not None else tasks(root, d))
        for k in FUNNEL:
            t["funnel"][k] += c["funnel"][k]
        for stage, by in c["kills"].items():
            for reason, n in by.items():
                t["kills"][stage][reason] = t["kills"][stage].get(reason, 0) + n
    t["through"] = max(t["through"], upto)
    return t


def ticks_per_hour(root, end, rows_by_day):
    """TICK rows per UTC hour for the 48 hours before end (an hour boundary); missing hours are 0."""
    hours = [end - timedelta(hours=h) for h in range(48, 0, -1)]
    n = dict.fromkeys((h.strftime("%Y-%m-%dT%H") for h in hours), 0)
    for d in sorted({day(h) for h in hours}):
        rows = rows_by_day[d] if d in rows_by_day else tasks(root, d)
        for r in _desk(rows, "TICK"):
            if r["ts"][:13] in n:
                n[r["ts"][:13]] += 1
    return [{"hour": h, "n": c} for h, c in n.items()]


def tokens_24h(rows):
    """The latest desk VERIFY or EXECUTE row per token (never a baseline row), newest first."""
    last, checks = {}, {}
    for r in rows:
        if r["book"] != "desk" or r["stage"] not in ("VERIFY", "EXECUTE") or not r["address"]:
            continue
        last[r["address"]] = r
        checks[r["address"]] = checks.get(r["address"], 0) + (r["stage"] == "VERIFY")
    out = [{"symbol": r["symbol"], "address": a, "verdict": r["action"], "reason": r["reason"],
            "checks": checks[a], "last": r["ts"], "lp_unknown": "lp_unknown" in r["detail"].split()}
           for a, r in last.items()]
    return sorted(out, key=lambda t: (t["last"], t["address"]), reverse=True)


def machine(root, now, d, s, open_n, closed=True):
    """The machine cards for day d, its tokens list and the new running totals. closed=False: d is today so far."""
    rows = tasks(root, d)
    c, t = day_counts(rows), since_totals(root, s, d, rows)
    ticks = _desk(rows, "TICK")
    secs = [float(_kv(r["detail"]).get("secs", 0)) for r in ticks]
    start = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if closed:
        end, expected = start + timedelta(days=1), TICKS_PER_DAY
    else:  # by hand: the hours so far, the :03 :08 ... slots already passed
        end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        m = int((now - start).total_seconds() // 60)
        expected = (m - 3) // 5 + 1 if m >= 3 else 0
    last = None
    if ticks:
        kv = _kv(ticks[-1]["detail"])
        last = {"ts": ticks[-1]["ts"], **{k: _num(kv[k]) for k in LAST_TICK if k in kv}}
    kills = [r for r in _desk(rows, "VERIFY") if r["action"] == "SKIP"]
    cards = {
        "day": d, "as_of": ts(now), "last_tick": last,
        "funnel": {"from": d, "to": d, **c["funnel"], "exits_open": open_n},
        "funnel_since": {"from": s["since"], "to": d, **t["funnel"]},
        "kills": {"from": d, "to": d, **c["kills"]},
        "kills_since": {"from": s["since"], "to": d, **t["kills"]},
        "kill_log": [{"ts": r["ts"], "symbol": r["symbol"], "stage": kill_stage(r), "reason": r["reason"]}
                     for r in kills[::-1][:KILL_LOG]],
        "runs": {"ticks": len(ticks), "expected": expected,
                 "rate": round(len(ticks) / expected * 100, 2) if expected else 0.0},
        "tick_secs": {"median": round(statistics.median(secs), 1) if secs else 0.0,
                      "max": round(max(secs), 1) if secs else 0.0},
        "ticks_per_hour": ticks_per_hour(root, end, {d: rows}),
    }
    return cards, tokens_24h(rows), t


def write_tokens(root, d, now, tokens):
    """Overwrite data/tokens_24h.json, one token per line (never appended)."""
    body = ",\n".join("  " + json.dumps(x, ensure_ascii=False) for x in tokens)
    _write(Path(root) / TOKENS, f'{{"day": {json.dumps(d)}, "as_of": {json.dumps(ts(now))}, "tokens": [\n{body}\n]}}\n')


def _open(s, books, d):
    close = next((e for e in s["equity"].get("desk", []) if e["day"] == d), None)
    return close["open"] if close else len(next(b for b in books if b.name == "desk").positions)


def run_machine(root, now, s, books):
    """In the tick, after the report: add the machine cards of s["machine_due"] to report.json and write
    tokens_24h.json, then store the running totals and clear it. Raises on failure (stays due, totals unchanged)."""
    d = s["machine_due"]
    cards, tokens, totals = machine(root, now, d, s, _open(s, books, d))
    rep = json.loads((Path(root) / REPORT).read_text())
    if rep.get("day") != d:
        raise ValueError(f"report.json is for {rep.get('day')}, not {d}")
    rep["machine"] = cards
    write(root, rep)
    write_tokens(root, d, now, tokens)
    s["totals"], s["machine_due"] = totals, ""
    return cards


def main(argv):
    root = Path(argv[0]) if argv else ROOT
    now = utcnow()
    s = load(root, now)
    books = [Book(n, root) for n in BOOKS]
    report = build(root, now, day(now), s, books)
    report["machine"], tokens, _ = machine(root, now, day(now), s, _open(s, books, day(now)), closed=False)
    write(root, report)
    write_tokens(root, day(now), now, tokens)
    print(report["summary"])
    print(f"wrote {root / REPORT} and {root / TOKENS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
