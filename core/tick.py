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
Machine cards (BL-23): right after the report, in their own try, core/report.py adds the machine cards
to report.json and writes data/tokens_24h.json (tasks.csv only, no request). A failure keeps the money
cards, logs machine=error:<type>, retries next tick (machine_due) and ends the run red after the TICK row.
Outcome labels (BL-26, logging only, core/labels.py): every desk check past dex_skip() is handed over as a sight;
after the desk and baseline steps, in their own try, the labels due (+1 h / +6 h / +24 h after first sight) are taken
with at most labels.MAX_CALLS batched DexScreener requests and written to data/daily/<day>/outcomes.csv. A failure
logs labels=error:<type>, leaves them due for the next tick and ends the run red after the TICK row.
TICK detail: the desk keys as before, then bl_positions bl_due bl_eligible bl_bought report machine bl_error,
then labels labels_due labels_done labels_calls.
"""
import sys
import time
from pathlib import Path

from adapters import base
from core import baseline, config, desk, exits, labels, report
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


def verify_and_buy(book, leads, held, client, now, n, seen=None, verdicts=None, sights=None):
    """Scan, check and buy for the desk. seen/verdicts (BL-8) collect each facts() lead's
    DexScreener side and the desk's outcome for the baseline; sights (BL-26) collect each check past
    dex_skip() for the outcome labels. They change nothing for the desk."""
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
        first_seen = (leads.s["watch"].get(address) or {}).get("first_seen") or ts(now)
        reason = leads.record(address, f, now)
        if sights is not None and not f.get("goplus_skipped"):
            sights.append(labels.sight(address, f, first_seen, now))
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
    b["report"] = b["machine"] = "none"
    reason, crash, report_error, machine_error, bl_errors = "", None, None, None, []
    lb = {"labels": "none", "labels_due": 0, "labels_done": 0, "labels_calls": 0}
    labels_error = None

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
        if rs["machine_due"] and not rs["due"]:
            try:
                m = report.run_machine(root, now, rs, [book, bl])
                log(f"MACHINE {m['day']}: " + " ".join(f"{k}={m['funnel'][k]}" for k in report.FUNNEL))
                b["machine"] = "ok"
            except Exception as e:  # money cards kept; stays due, retried next tick, run ends red
                b["machine"], machine_error = f"error:{type(e).__name__}", e
        report.save(root, rs)
        held, bl_held = set(book.positions), set(bl.positions)
        n["positions"], b["bl_positions"] = len(held), len(bl_held)
        b["bl_due"] = int(baseline.tick_count(bl))
        exits.check(book, lambda a: base.quote(a, book.positions[a].get("pair", ""), client), now)
        bl_try(bl, "EXIT", lambda: exits.check(
            bl, lambda a: base.quote(a, bl.positions[a].get("pair", ""), client), now))
        seen, verdicts, sights = {}, {}, []
        if config.PAUSED:
            reason = "paused"
        elif book.halted(now):
            reason = "loss_halt"
        else:
            verify_and_buy(book, leads, held, client, now, n, seen, verdicts, sights)

            def bl_buy():
                b["bl_eligible"] = len(baseline.eligible(seen, bl_held))
                return b["bl_due"] and baseline.buy(bl, seen, verdicts, bl_held, now)

            if bl_try(bl, "EXECUTE", bl_buy):
                b["bl_bought"] = 1
        book.save()
        report.track(rs, [book])
        bl_try(bl, "BOOK", lambda: (bl.save(), report.track(rs, [bl])), after_error=True)
        report.save(root, rs)
        calls = len(client.calls)
        try:  # BL-26: last, never in the way of a trade; stays due, retried next tick, run ends red
            got = labels.run(root, now, sights, client)
            lb.update(labels="ok" if got["due"] or got["calls"] else "none", labels_due=got["due"],
                      labels_done=got["done"], labels_calls=got["calls"])
        except Exception as e:
            lb.update(labels=f"error:{type(e).__name__}", labels_calls=len(client.calls) - calls)
            labels_error = e
    except Exception as e:  # the heartbeat row must still be written
        reason, crash = f"error:{type(e).__name__}", e
    n["goplus"] = goplus_calls(client)
    n["requests"] = len(client.calls)
    n["secs"] = round(clock() - start, 1)
    n.update(b)
    n["bl_error"] = type(bl_errors[0]).__name__ if bl_errors else "none"
    n.update(lb)
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
    if machine_error:
        raise machine_error
    if bl_errors:
        raise bl_errors[0]
    if labels_error:
        raise labels_error
    return n


def main():
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
