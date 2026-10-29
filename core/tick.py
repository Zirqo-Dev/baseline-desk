"""One tick (BL-7): roll the day, exits first, then scan -> verify -> paper buy.

Run by .github/workflows/tick.yml every 5 min:  python -m core.tick
Every tick ends with exactly one TICK heartbeat row (Gate A counts them), even when it crashes;
a crash then re-raises so the Actions run shows red.

Order of lead checks (DECISIONS 2026-10-23, BL-7 4b): watchlist re-checks that are due (oldest
first_seen first, the 3 h clock), then fetch_error retries, then new leads from the scan.
At most MAX_CHECKS facts() calls per tick, and at most config.GOPLUS_MAX_PER_TICK GoPlus calls.
DexScreener first (BL-20): once the GoPlus calls are used, the loop goes on; leads the DexScreener
side settles still get their row ("dex_only"), leads that need GoPlus are "deferred". Deferred and
the overflow past MAX_CHECKS log no row, are not a retry try and wait for a later tick.
Tokens held at the start of the tick (before exits) are not re-checked and log no row;
they count as "held" (BL-7 4a). So a token sold this tick can't be bought back in the same tick.

Random baseline (BL-8, core/baseline.py): its exits run after the desk's; after the desk's checks
it may buy one random eligible lead (every baseline.CADENCE ticks), picked from what the desk's
facts() calls already fetched, so no extra request and no change to the desk. On the UTC day roll,
before exits, core/report.py writes data/report.json for the day just closed in its own try:
a failure is logged as report=error:<type>, retried next tick, and the run ends red after the TICK row.
The baseline exits and the baseline buy each run in their own try too: an error logs one row
(book=baseline, action ERROR, reason error:<type>) and bl_error=<type>, the desk's exits, verify and buy
still run, the baseline buy is skipped after a baseline exit error (stays due), and the run ends red.
TICK detail: the desk keys as before, then bl_positions bl_due bl_eligible bl_bought report bl_error.
"""
import sys
import time
from pathlib import Path

from adapters import base
from core import baseline, config, desk, exits, report
from core.book import ROOT, TASK_FIELDS, Book, _append, day, ts, utcnow
from core.leads import Leads

MAX_CHECKS = 20  # facts() calls per tick (the owner, BL-7 4b)
COUNTS = ["positions", "seen", "rechecks", "new", "checked", "passed", "bought", "held", "deferred", "dex_only"]
BL_COUNTS = ["bl_positions", "bl_due", "bl_eligible", "bl_bought"]  # BL-8, after the desk keys


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


def verdict(reason):
    """The desk's outcome for a lead, as the baseline BUY row logs it (desk=...)."""
    return {"": "PASS", "goplus_not_ready": "WATCH"}.get(reason, reason)


def verify_and_buy(book, leads, held, client, now, n, seen=None, verdicts=None):
    """Scan, check and buy for the desk. seen/verdicts (BL-8) collect each facts() lead's
    DexScreener side and the desk's outcome for the baseline; they change nothing for the desk."""
    seen = {} if seen is None else seen
    verdicts = {} if verdicts is None else verdicts
    found = base.scan(client)
    first, new, n["held"] = queue(leads, found, held, now)
    n["seen"], n["rechecks"], n["new"] = len(found), len(first), len(new)
    todo = first + new
    n["deferred"] = max(len(todo) - MAX_CHECKS, 0)
    todo = todo[:MAX_CHECKS]
    for address in todo:
        seen[address] = {}
        f = base.facts(address, now=now, client=client,
                       goplus_ok=lambda: goplus_calls(client) < config.GOPLUS_MAX_PER_TICK, dex=seen[address])
        if f is None:  # needs GoPlus and the cap is used: no row, not a retry try
            n["deferred"] += 1
            verdicts[address] = "deferred"
            continue
        n["checked"] += 1
        if f.get("goplus_skipped"):
            n["dex_only"] += 1
        reason = leads.record(address, f, now)
        verdicts[address] = verdict(reason)
        if reason == "":
            n["passed"] += 1
            if desk.buy(book, address, f, now):
                n["bought"] += 1


def run(root=ROOT, now=None, client=None, clock=time.monotonic, log=print):
    """One tick. Returns the heartbeat counts; re-raises a crash after logging the TICK row."""
    root, now = Path(root), now or utcnow()
    start = clock()
    client = client or base.Client()
    n = dict.fromkeys(COUNTS, 0)
    b = dict.fromkeys(BL_COUNTS, 0)
    b["report"] = "none"
    reason, crash, report_error, bl_errors = "", None, None, []

    def bl_try(bl, stage, step, after_error=False):
        """One baseline step; an error is logged and kept, never stops the desk (pick 7)."""
        if bl_errors and not after_error:  # after a baseline error, skip the baseline buy this tick
            return None
        try:
            return step()
        except Exception as e:
            bl_errors.append(e)
            bl.task(now, stage, "", "", "ERROR", f"error:{type(e).__name__}")
            return None

    try:
        book, bl, leads = Book("desk", root), Book(baseline.NAME, root), Leads(root)
        rs = report.load(root, now)
        if book.s["day"] and book.s["day"] != day(now):  # UTC day roll: closing values first
            report.on_roll(rs, [book, bl], book.s["day"])
        book.roll_day(now)
        bl.roll_day(now)
        if rs["due"]:
            try:
                log("REPORT " + report.run_due(root, now, rs, [book, bl])["summary"])
                b["report"] = "ok"
            except Exception as e:  # trading goes on; stays due, retried next tick, run ends red
                b["report"], report_error = f"error:{type(e).__name__}", e
        report.save(root, rs)
        held, bl_held = set(book.positions), set(bl.positions)
        n["positions"], b["bl_positions"] = len(held), len(bl_held)
        b["bl_due"] = int(baseline.tick_count(bl))
        exits.check(book, lambda a: base.quote(a, book.positions[a].get("pair", ""), client), now)
        bl_try(bl, "EXIT", lambda: exits.check(
            bl, lambda a: base.quote(a, bl.positions[a].get("pair", ""), client), now))
        seen, verdicts = {}, {}
        if config.PAUSED:
            reason = "paused"
        elif book.halted(now):
            reason = "loss_halt"
        else:
            verify_and_buy(book, leads, held, client, now, n, seen, verdicts)

            def bl_buy():
                b["bl_eligible"] = len(baseline.eligible(seen, bl_held))
                return b["bl_due"] and baseline.buy(bl, seen, verdicts, bl_held, now)

            if bl_try(bl, "EXECUTE", bl_buy):
                b["bl_bought"] = 1
        book.save()
        report.track(rs, [book])
        bl_try(bl, "BOOK", lambda: (bl.save(), report.track(rs, [bl])), after_error=True)
        report.save(root, rs)
    except Exception as e:  # the heartbeat row must still be written
        reason, crash = f"error:{type(e).__name__}", e
    n["goplus"] = goplus_calls(client)
    n["requests"] = len(client.calls)
    n["secs"] = round(clock() - start, 1)
    n.update(b)
    n["bl_error"] = type(bl_errors[0]).__name__ if bl_errors else "none"
    detail = " ".join(f"{k}={v}" for k, v in n.items())
    # written straight to tasks.csv, so it works even when the state file is what broke
    _append(root / config.TASKS.format(day=day(now)), TASK_FIELDS, {
        "ts": ts(now), "book": "desk", "stage": "TICK", "action": "HEARTBEAT",
        "reason": reason, "detail": detail})
    log(f"TICK {reason or 'ok'} {detail}")
    if crash:
        raise crash
    if report_error:
        raise report_error
    if bl_errors:
        raise bl_errors[0]
    return n


def main():
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
