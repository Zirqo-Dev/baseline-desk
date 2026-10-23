"""One tick (BL-7): roll the day, exits first, then scan -> verify -> paper buy.

Run by .github/workflows/tick.yml every 5 min:  python -m core.tick
Every tick ends with exactly one TICK heartbeat row (Gate A counts them), even when it crashes;
a crash then re-raises so the Actions run shows red.

Order of lead checks (DECISIONS 2026-10-23, BL-7 4b): watchlist re-checks that are due (oldest
first_seen first, the 3 h clock), then fetch_error retries, then new leads from the scan.
At most MAX_CHECKS facts() calls per tick, and at most config.GOPLUS_MAX_PER_TICK GoPlus calls
(every facts() makes one); the rest count as "deferred", log no row and wait for a later tick.
Tokens held at the start of the tick (before exits) are not re-checked and log no row;
they count as "held" (BL-7 4a). So a token sold this tick can't be bought back in the same tick.
"""
import sys
import time
from pathlib import Path

from adapters import base
from core import config, desk, exits
from core.book import ROOT, TASK_FIELDS, Book, _append, day, ts, utcnow
from core.leads import Leads

MAX_CHECKS = 20  # facts() calls per tick (Pete, BL-7 4b)
COUNTS = ["positions", "seen", "rechecks", "new", "checked", "passed", "bought", "held", "deferred"]


def queue(leads, found, held, now):
    """Addresses to check this tick, in order, and how many scan leads we hold."""
    watch = sorted((w["first_seen"], a) for a, w in leads.s["watch"].items() if leads.due(a, now))
    retry = sorted((r["last"], a) for a, r in leads.s["retry"].items()
                   if a not in leads.s["watch"] and leads.due(a, now))
    first = [a for _, a in watch] + [a for _, a in retry]
    first = [a for a in first if a not in held]
    new, n_held = [], 0
    for lead in found:
        a = lead["address"]
        if a in held:
            n_held += 1
        elif a not in first and a not in new and leads.due(a, now):
            new.append(a)
    return first, new, n_held


def goplus_calls(client):
    """GoPlus requests made so far with this client (one client per tick); the URL is in each calls entry."""
    return sum(1 for call in client.calls if any(str(x).startswith(base.GOPLUS) for x in call))


def verify_and_buy(book, leads, held, client, now, n):
    found = base.scan(client)
    first, new, n["held"] = queue(leads, found, held, now)
    n["seen"], n["rechecks"], n["new"] = len(found), len(first), len(new)
    todo = first + new
    n["deferred"] = max(len(todo) - MAX_CHECKS, 0)
    todo = todo[:MAX_CHECKS]
    for i, address in enumerate(todo):
        if goplus_calls(client) >= config.GOPLUS_MAX_PER_TICK:  # no row, not a retry try
            n["deferred"] += len(todo) - i
            break
        f = base.facts(address, now=now, client=client)
        n["checked"] += 1
        if leads.record(address, f, now) == "":
            n["passed"] += 1
            if desk.buy(book, address, f, now):
                n["bought"] += 1


def run(root=ROOT, now=None, client=None, clock=time.monotonic, log=print):
    """One tick. Returns the heartbeat counts; re-raises a crash after logging the TICK row."""
    root, now = Path(root), now or utcnow()
    start = clock()
    client = client or base.Client()
    n = dict.fromkeys(COUNTS, 0)
    reason, crash = "", None
    try:
        book, leads = Book("desk", root), Leads(root)
        book.roll_day(now)
        held = set(book.positions)
        n["positions"] = len(held)
        exits.check(book, lambda a: base.quote(a, book.positions[a].get("pair", ""), client), now)
        if config.PAUSED:
            reason = "paused"
        elif book.halted(now):
            reason = "loss_halt"
        else:
            verify_and_buy(book, leads, held, client, now, n)
        book.save()
    except Exception as e:  # the heartbeat row must still be written
        reason, crash = f"error:{type(e).__name__}", e
    n["goplus"] = goplus_calls(client)
    n["requests"] = len(client.calls)
    n["secs"] = round(clock() - start, 1)
    detail = " ".join(f"{k}={v}" for k, v in n.items())
    # written straight to tasks.csv, so it works even when the state file is what broke
    _append(root / config.TASKS.format(day=day(now)), TASK_FIELDS, {
        "ts": ts(now), "book": "desk", "stage": "TICK", "action": "HEARTBEAT",
        "reason": reason, "detail": detail})
    log(f"TICK {reason or 'ok'} {detail}")
    if crash:
        raise crash
    return n


def main():
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
