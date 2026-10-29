"""EXIT: six rules, checked every run, at most one fires per position per run.

Order: liquidity pull, hard stop, ladder rung 1, ladder rung 2, trail, time stop.
Gains are measured from the FILLED entry price (friction included).
Each check logs one EXIT task row (SELL or HOLD). Each sell logs the intended
price (where the rule should have fired) next to the fill (stop slippage).
"""
from datetime import datetime, timezone

from core import config, desk


def age_min(pos, now):
    entry = datetime.strptime(pos["entry_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now - entry).total_seconds() / 60


def decide(pos, price, liq_usd, now):
    """Pick the exit for one position. Returns (rule, qty or None for all, intended price) or None."""
    e = config.EXIT
    entry = pos["entry_fill"]
    pct = (price / entry - 1) * 100

    if liq_usd <= pos["entry_liq_usd"] * (1 - e["liq_drop_pct"] / 100):
        return "liq_pull", None, price
    if pct <= e["hard_stop_pct"]:
        return "hard_stop", None, entry * (1 + e["hard_stop_pct"] / 100)
    for i, (gain, share) in enumerate(e["ladder"]):
        if i not in pos["ladder_done"] and pct >= gain:
            # rungs sell a share of the ORIGINAL size; the last rung can't sell more than remains
            return f"ladder_{i + 1}", min(pos["orig_qty"] * share, pos["qty"]), entry * (1 + gain / 100)
    if pos["ladder_done"]:
        stop = pos["peak_price"] * (1 - e["trail_pct"] / 100)
        if price <= stop:
            return "trail", None, stop
    elif age_min(pos, now) >= e["time_stop_min"] and pct < e["time_stop_min_pct"]:
        return "time_stop", None, price
    return None


def check(book, quote, now):
    """Run the exit rules on every open position.

    quote(address) -> {"price": float, "liq_usd": float, "source": "pool" | "fallback"} or None.
    A missing number means HOLD (reason "no_quote"); the position is checked again next run.
    A fallback quote (our pool is gone) adds "quote=fallback" to the EXIT row and the trade row.
    """
    for address in list(book.positions):
        pos = book.positions[address]
        q = quote(address) or {}
        price, liq = q.get("price"), q.get("liq_usd")
        if price is None or liq is None:
            book.task(now, "EXIT", pos["symbol"], address, "HOLD", "no_quote")
            continue
        note = "quote=fallback" if q.get("source") == "fallback" else ""
        book.mark(address, price, liq)
        pct = (price / pos["entry_fill"] - 1) * 100
        out = decide(pos, price, liq, now)
        if out is None:
            book.task(now, "EXIT", pos["symbol"], address, "HOLD", "", " ".join(filter(None, [f"pct={pct:.2f}", note])))
            continue
        rule, qty, intended = out
        if rule.startswith("ladder_"):
            pos["ladder_done"].append(int(rule.split("_")[1]) - 1)
        desk.sell(book, address, price, qty, rule, liq, now, intended=intended, note=note)
        book.task(now, "EXIT", pos["symbol"], address, "SELL", rule,
                  " ".join(filter(None, [f"pct={pct:.2f} intended={intended:.10g} quote={price:.10g}", note])))
    book.save()
