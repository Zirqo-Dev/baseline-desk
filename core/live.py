"""LIVE (BL-27, logging/display only): data/live.json, rewritten at the end of every tick for the site.

Open positions of both books, the last tick's counts, today's tick count and the exit numbers the site's chips name.
Built from data/state.json as saved by this tick (what the next tick will see) and today's tasks.csv: no request.
No rule, verdict, exit or config reads it. core/tick.py runs it in its own try just before the TICK row:
TICK detail live=ok|error:<type>; a failure is retried next tick (the file is rewritten every tick) and the run
ends red after the TICK row. A crashed tick writes no live.json, so the site's age badge goes stale and says so.

Shape (about 1 KB):
  as_of      tick time, UTC ISO
  last_tick  ts + seen new checked dex_only goplus passed bought exits requests secs (exits = desk EXIT SELL rows
             this tick; the rest are the TICK row's own values)
  today      day, ticks (TICK rows today incl. this tick's), expected (:03 :08 ... slots passed), errors (ERROR rows)
  exit       hard_stop_pct time_stop_min time_stop_min_pct trail_pct, from core/config.py EXIT
  books      desk / baseline: cash_usd, value_usd (report.liq_value), open: [position, oldest first]
  position   symbol address entry_ts age_min cost_usd (remaining qty) value_usd (remaining qty sold at the last quote
             after sell friction, as report.liq_value) value_pct (vs cost_usd) price_pct (last price vs entry fill, as
             the EXIT row's pct=) liq_usd (last) qliq (entry_qliq: real depth at entry, null when not known)
             ladders_done (["ladder_1", ...]) time_stop_at (entry_ts + time_stop_min, always written)
DECISIONS 2026-11-01, BL-27.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config, report
from core.book import Book, day, ts

LIVE = "data/live.json"
LAST_TICK = ["seen", "new", "checked", "dex_only", "goplus", "passed", "bought", "exits", "requests", "secs"]
EXIT_KEYS = ["hard_stop_pct", "time_stop_min", "time_stop_min_pct", "trail_pct"]


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def position(address, p, now):
    """One open position as the site shows it. Values on the same basis as report.liq_value()."""
    last, cost = p["last_price"], p["cost_usd"]
    liq = p.get("last_liq_usd", p["entry_liq_usd"])
    fill, _ = report.sell_value(p)  # BL-32: same depth rule as report.liq_value()
    value, entry = p["qty"] * fill, _t(p["entry_ts"])
    qliq = p.get("entry_qliq")
    return {
        "symbol": p["symbol"], "address": address, "entry_ts": p["entry_ts"],
        "age_min": max(0, int((now - entry).total_seconds() // 60)),  # never negative on a skewed clock
        "cost_usd": round(cost, 4), "value_usd": round(value, 4),
        "value_pct": round((value / cost - 1) * 100, 2) if cost > 0 else None,
        "price_pct": round((last / p["entry_fill"] - 1) * 100, 2),
        "liq_usd": round(liq), "qliq": qliq,
        "ladders_done": [f"ladder_{i + 1}" for i in sorted(p["ladder_done"])],
        "time_stop_at": ts(entry + timedelta(minutes=config.EXIT["time_stop_min"])),
    }


def book_card(b, now):
    rows = sorted(b.positions.items(), key=lambda kv: (kv[1]["entry_ts"], kv[0]))
    return {"cash_usd": round(b.cash, 4), "value_usd": round(report.liq_value(b), 4),
            "open": [position(a, p, now) for a, p in rows]}


def today(root, now):
    """Today's TICK rows (this tick's row is written after live.json, so +1), slots passed and ERROR rows."""
    rows = report.tasks(root, day(now))
    return {"day": day(now), "ticks": sum(1 for r in rows if r["stage"] == "TICK") + 1,
            "expected": report.slots(now), "errors": sum(1 for r in rows if r["action"] == "ERROR")}


def build(root, now, n, exits_sold):
    """The live.json dict. n = the tick's heartbeat counts; exits_sold = desk EXIT SELL rows this tick."""
    counts = dict(n, exits=exits_sold)
    return {
        "as_of": ts(now),
        "last_tick": {"ts": ts(now), **{k: counts[k] for k in LAST_TICK if k in counts}},
        "today": today(root, now),
        "exit": {k: config.EXIT[k] for k in EXIT_KEYS},
        "books": {name: book_card(Book(name, root), now) for name in report.BOOKS},
    }


def write(root, live):
    report._write(Path(root) / LIVE, json.dumps(live, indent=1) + "\n")


def run(root, now, n, exits_sold):
    """In the tick: build and overwrite data/live.json. Raises on failure (the tick logs live=error:<type>)."""
    live = build(root, now, n, exits_sold)
    write(root, live)
    return live
