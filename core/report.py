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
"""
import csv
import json
import os
import sys
from pathlib import Path

from core import config, friction
from core.book import ROOT, Book, day, load_state, save_key, ts, utcnow

BOOKS = ["desk", "baseline"]
RULES = ["liq_pull", "hard_stop", "ladder_1", "ladder_2", "trail", "time_stop"]
SLIP_RULES = ["hard_stop", "trail"]
GATE_B = {"target": 100, "kill": 200}
REPORT = "data/report.json"


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


def write(root, report):
    path = Path(root) / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, path)


def run_due(root, now, s, books):
    """In the tick: write the report for the closed day in s["due"], then clear it. Raises on failure (stays due)."""
    report = build(root, now, s["due"], s, books)
    write(root, report)
    s["due"] = ""
    return report


def main(argv):
    root = Path(argv[0]) if argv else ROOT
    now = utcnow()
    report = build(root, now, day(now), load(root, now))
    write(root, report)
    print(report["summary"])
    print(f"wrote {root / REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
