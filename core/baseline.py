"""Random baseline (BL-8): a shadow book "baseline" that buys a random eligible lead every CADENCE ticks.

Same START_BALANCE, sizing, friction, exits, quote logic, day roll and loss halt as the desk
(it runs on core/book.py, core/desk.py and core/exits.py); only entry selection differs.
Its EXECUTE, EXIT and trade rows carry book=baseline. DECISIONS 2026-10-29, BL-8.

Eligible = this tick's leads that got a facts() call (checked, or deferred by the GoPlus cap) whose
DexScreener side passes core/verify.py dex_skip() and has sells_1h > 0, minus tokens the baseline held
at the start of the tick. No GoPlus call and no extra request: the DexScreener facts come from the
`dex` dict facts() fills. Launchpad (for friction) is pool shape only, so a Clanker pool whose fee
is unknown is "other": the baseline can pay less friction than the desk (it is flattered).

Cadence: state "ticks_since_buy" goes up by one every tick and resets to 0 only on a BUY; at CADENCE or
more the buy is due. Nothing eligible or a blocked buy (max_open, loss_halt, size) logs one EXECUTE SKIP
row and the buy stays due. No scan this tick (PAUSED, desk loss halt) = no row, still due.

Replay: seed = the tick time as YYYYMMDDHHMM; pick = random.Random(seed).randrange(eligible) over the
eligible addresses sorted. The EXECUTE detail logs seed, eligible, pick and the 6-hex prefixes of the pool.
"""
import random

from core import desk
from core.verify import dex_skip

NAME = "baseline"
CADENCE = 12  # ticks between baseline buys (the owner, BL-8)


def tick_count(book):
    """Count this tick. Returns True when a buy is due."""
    book.s["ticks_since_buy"] = book.s.get("ticks_since_buy", 0) + 1
    return book.s["ticks_since_buy"] >= CADENCE


def eligible(seen, held):
    """Sorted addresses the baseline may pick. seen = {address: DexScreener facts} of this tick's leads."""
    return sorted(a for a, d in seen.items()
                  if a not in held and not dex_skip(d) and d["sells_1h"] > 0)


def seed(now):
    return int(now.strftime("%Y%m%d%H%M"))


def pick(pool, s):
    """Index into the sorted pool for seed s (replay: random.Random(s).randrange(len(pool)))."""
    return random.Random(s).randrange(len(pool))


def buy(book, seen, verdicts, held, now):
    """The due baseline buy for one tick (after the scan). Logs one EXECUTE row. Returns the position or None."""
    pool = eligible(seen, held)
    if not pool:
        book.task(now, "EXECUTE", "", "", "SKIP", "no_eligible", "eligible=0")
        book.save()
        return None
    s = seed(now)
    i = pick(pool, s)
    address = pool[i]
    f = seen[address]
    note = (f"seed={s} eligible={len(pool)} pick={i} pool={','.join(a[2:8] for a in pool)} "
            f"desk={verdicts.get(address, '')} launchpad={f['launchpad']}")
    pos = desk.buy(book, address, f, now, note=note)
    if pos:
        book.s["ticks_since_buy"] = 0
        book.save()
    return pos
