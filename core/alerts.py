"""ALERTS (BL-3): Telegram messages to the owner's phone. Display only: no rule, verdict, exit, report or
live.json reads or changes anything here.

Env TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (repo secrets, passed to the tick step only). Either missing or empty:
alerts=off, no request, nothing queued, state untouched.

SECRETS, HARD RULE (Actions logs are public): the token, the chat id, the request URL and Telegram's reply are never
printed, logged, written to data/ or raised. A send reports only "ok" or "error:<ExceptionTypeName>"; the exception
object itself never leaves deliver(), and str(exception) is never used (urllib errors carry the URL).

Events, desk only, built from this tick's own rows (tasks.csv / trades.csv rows at this tick's ts) and results;
no data request:
  error  a red run: each failed step (crash, report, machine, baseline, labels, live) with its error type;
         at most one error alert per hour
  sell   every desk SELL: rule, price %, pnl, desk cash after that sale
  buy    every desk BUY: symbol, $ size, fill, liq, qliq, DexScreener link
  daily  right after a successful day-roll report: desk value, random value, desk lead, Gate B n/100,
         closes that day (desk, random), runs x/288 and ERROR rows of the closed day; BL-43 adds a refill line
         when a book was topped up that day ("refilled +X (not P&L)", from state.json report.equity), Gate B since
         with the random closes, and a per-trade line per book (report.json per_trade). Reads only data/.
Plain text only (no parse_mode), so symbols like w_COIN or [BSTONK] can never break a send. Link previews off.
Quiet hours 22:00 to 07:00 America/Toronto (zoneinfo, DST-safe): sent with disable_notification=true, never dropped.

Queue (state key "alerts": queue, last_error): new alerts join the end; more than MAX_QUEUE drops the oldest; an alert
older than MAX_AGE_MIN is dropped; at most MAX_SENDS sends per tick, oldest first, stopping at the first failure
(the rest wait for the next tick). core/tick.py runs it last, in its own try, after live.json, also on a crashed tick:
TICK detail ends with alerts=ok|off|none|error:<type> alerts_sent=<n>. Alerts never make the run red.

By hand / alert-test.yml:  python -m core.alerts --test  sends one message marked TEST shaped like a desk BUY,
writes nothing and prints only ok, off or error:<type> (exit 0 on ok, else 1).
DECISIONS 2026-11-01, BL-3.
"""
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core import config, report
from core.book import day, load_state, save_key, ts, utcnow

API = "https://api.telegram.org"
TIMEOUT = 10        # seconds per send
MAX_SENDS = 3       # per tick
MAX_QUEUE = 20      # alerts waiting in state
MAX_AGE_MIN = 60    # an unsent alert older than this is dropped
ERROR_EVERY_MIN = 60  # at most one error alert per hour
QUIET_TZ = ZoneInfo("America/Toronto")
QUIET_FROM, QUIET_TO = 22, 7  # local hours: from 22:00 to 07:00
KEY = "alerts"
DEX = "https://dexscreener.com/base/"


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---- sending ----
def secrets(env=None):
    """(token, chat id) or None when either is missing or empty."""
    env = os.environ if env is None else env
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    return (token, chat) if token and chat else None


def quiet(now):
    """True from 22:00 to 07:00 America/Toronto (DST from zoneinfo)."""
    h = now.astimezone(QUIET_TZ).hour
    return h >= QUIET_FROM or h < QUIET_TO


def deliver(creds, text, now):
    """One sendMessage. Returns "ok" or "error:<ExceptionTypeName>" and never raises: the exception (which can carry
    the URL, so the token) and Telegram's reply (the owner's name) stay inside this function."""
    try:
        token, chat = creds
        body = urllib.parse.urlencode({
            "chat_id": chat, "text": text,  # plain text: no parse_mode
            "disable_notification": "true" if quiet(now) else "false",
            "link_preview_options": json.dumps({"is_disabled": True}),
        }).encode()
        req = urllib.request.Request(f"{API}/bot{token}/sendMessage", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            ok = json.loads(r.read().decode("utf-8")).get("ok") is True
        return "ok" if ok else "error:NotOk"
    except Exception as e:  # noqa: BLE001 - only the type name may leave
        return f"error:{type(e).__name__}"


# ---- message texts (plain text) ----
def _usd(x, sign=False):
    x = float(x)
    s = f"${abs(x):,.2f}"
    return ("-" if x < 0 else "+" if sign else "") + s


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _liq(x):
    v = _num(x)
    return f"${v:,.0f}" if v is not None else "na"


def buy_text(symbol, address, usd, fill, liq, qliq, test=False):
    head = "TEST · " if test else ""
    return (f"{head}Baseline desk BUY {symbol}\n"
            f"{_usd(usd)} at {float(fill):.6g}\n"
            f"liq {_liq(liq)} · real depth {_liq(qliq)}\n"
            f"{DEX}{address}")


def sell_text(symbol, rule, pct, pnl, cash_after):
    p = _num(pct)
    pct_s = f"{p:+.2f}%" if p is not None else "na"
    cash_s = _usd(cash_after) if cash_after is not None else "na"
    return (f"Baseline desk SELL {symbol} · {rule}\n"
            f"price {pct_s} · pnl {_usd(pnl, sign=True)}\n"
            f"desk cash after {cash_s}")


def _since(t):
    """2026-11-06T20:33:15Z -> 2026-11-06 20:33 UTC; anything else -> na."""
    return f"{t[:10]} {t[11:16]} UTC" if isinstance(t, str) and len(t) >= 16 else "na"


def _per_trade(pt):
    """BL-43: one book's report.json per_trade as 'n closes · +$x (+y%) each', a dash when n = 0."""
    n = (pt or {}).get("n")
    if not isinstance(n, int):
        return "na"
    if n == 0:
        return "0 closes · —"
    return (f"{n} close{'' if n == 1 else 's'} · {_usd(pt['net_per_trade_usd'], True)} "
            f"({float(pt['net_per_trade_pct']):+.2f}%) each")


def daily_text(rep, runs, expected, errors, refilled=None):
    """refilled = {book: USD topped up on the report day} (BL-43): a line only when a book was refilled; top-ups are
    never P&L. Every BL-3 line stays as it was; BL-43 adds the refill, Gate B since and per-trade lines."""
    d, bl, gb = rep["desk"], rep["baseline"], rep["gate_b"]
    names = {"desk": "desk", "baseline": "random"}
    topped = [f"{names[b]} refilled {_usd(x, True)} (not P&L)" for b, x in (refilled or {}).items()
              if b in names and x is not None and round(x, 2) > 0]
    since = _since(gb.get("since"))
    return "\n".join(
        [f"Baseline daily · {rep['day']}",
         f"desk {_usd(d['value_usd'])} · random {_usd(bl['value_usd'])} · desk lead {_usd(rep['desk_lead_usd'], True)}"]
        + ([" · ".join(topped)] if topped else [])
        + [f"Gate B {gb['desk_closes']}/{gb['target']} desk closes",
           f"Gate B since {since} · random {gb.get('baseline_closes', 'na')} closes · kill at {gb.get('kill', 'na')}",
           f"desk per trade since {since}: {_per_trade(d.get('per_trade'))}",
           f"random per trade since {since}: {_per_trade(bl.get('per_trade'))}",
           f"closes that day: desk {d['closes_day']} · random {bl['closes_day']}",
           f"runs {runs}/{expected} · ERROR rows {errors}"])


def error_text(now, red):
    """red = [(step, type name)]."""
    lines = "\n".join(f"{step} error:{name}" for step, name in red)
    return f"Baseline run RED · {now.strftime('%Y-%m-%d %H:%M')} UTC\n{lines}"


def test_text():
    return buy_text("TEST_COIN", "0x" + "0" * 40, 5.0, 0.00123456, 123456, 4321, test=True)


# ---- this tick's events ----
def _csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _kv(detail):
    return dict(kv.split("=", 1) for kv in (detail or "").split() if "=" in kv)


def _desk_cash(root):
    try:
        return float(load_state(Path(root) / config.STATE)["desk"]["cash"])
    except Exception:  # noqa: BLE001 - a broken state only blanks the cash line
        return None


def trade_texts(root, now):
    """SELL then BUY texts for the desk's rows at this tick's ts (tasks.csv and trades.csv), in row order."""
    root, t = Path(root), ts(now)
    tasks = [r for r in _csv(root / config.TASKS.format(day=day(now)))
             if r["ts"] == t and r["book"] == "desk"]
    trades = [r for r in _csv(root / config.TRADES.format(day=day(now)))
              if r["ts"] == t and r["book"] == "desk"]
    pct = {r["address"]: _kv(r["detail"]).get("pct") for r in tasks if r["stage"] == "EXIT" and r["action"] == "SELL"}
    qliq = {r["address"]: _kv(r["detail"]).get("qliq", "na") for r in tasks
            if r["stage"] == "VERIFY" and r["action"] == "PASS"}
    sells = [r for r in trades if r["side"] == "SELL"]
    buys = [r for r in trades if r["side"] == "BUY"]
    cash = _desk_cash(root)
    # cash after each sale: today's end cash + this tick's buys, minus the proceeds of the later sales
    after_exits = None if cash is None else cash + sum(float(b["usd"]) for b in buys)
    out = []
    for i, r in enumerate(sells):
        later = sum(float(x["usd"]) for x in sells[i + 1:])
        out.append(("sell", sell_text(r["symbol"], r["reason"], pct.get(r["address"]), r["pnl_usd"] or 0,
                                      None if after_exits is None else after_exits - later)))
    for r in buys:
        out.append(("buy", buy_text(r["symbol"], r["address"], r["usd"], r["fill_price"], r["liq_usd"],
                                    qliq.get(r["address"], "na"))))
    return out


def refilled_on(root, day_):
    """BL-43: USD each book was topped up on day_ = the change in refills_usd between that day's equity entry and the
    one before it (state.json report.equity; entries before BL-31 have none = 0). A book without an entry for day_,
    or an unreadable state: None (no line)."""
    try:
        eq = (load_state(Path(root) / config.STATE).get("report") or {}).get("equity") or {}
    except Exception:  # noqa: BLE001 - a broken state only drops the refill line
        return None
    out = {}
    for b, entries in eq.items():
        i = next((k for k, e in enumerate(entries) if e.get("day") == day_), None)
        if i is None:
            continue
        prev = float(entries[i - 1].get("refills_usd", 0.0)) if i > 0 else 0.0
        out[b] = float(entries[i].get("refills_usd", 0.0)) - prev
    return out


def daily(root, rep):
    rows = report.tasks(root, rep["day"])
    runs = sum(1 for r in rows if r["book"] == "desk" and r["stage"] == "TICK")
    errors = sum(1 for r in rows if r["action"] == "ERROR")
    return daily_text(rep, runs, report.TICKS_PER_DAY, errors, refilled_on(root, rep["day"]))


def _red_alerted_within(root, now, minutes):
    """Broken state only: was a red run's alert already sent within `minutes` (TICK rows, today and yesterday)?"""
    since = now - timedelta(minutes=minutes)
    for d in sorted({day(since), day(now)}):
        for r in _csv(Path(root) / config.TASKS.format(day=d)):
            if r["stage"] == "TICK" and r["reason"].startswith("error:") and _t(r["ts"]) > since:
                if int(_kv(r["detail"]).get("alerts_sent", "0") or 0) > 0:
                    return True
    return False


# ---- in the tick ----
def run(root, now, red=(), rep=None, env=None):
    """Queue this tick's alerts and send up to MAX_SENDS. red = [(step, type name)] of a red run; rep = the report
    dict of a successful day-roll report. Returns (status, sent). status: off | none | ok | error:<type>."""
    creds = secrets(env)
    if creds is None:
        return "off", 0
    root = Path(root)
    try:
        s = load_state(root / config.STATE).get(KEY) or {}
        state_ok = True
    except Exception:  # noqa: BLE001 - a broken state.json (a crash) must not stop the crash alert
        s, state_ok = {}, False
    queue = list(s.get("queue") or [])
    last_error = s.get("last_error", "")
    before = (json.dumps(queue), last_error)

    new = []
    if red:
        if state_ok:
            due = not last_error or now - _t(last_error) >= timedelta(minutes=ERROR_EVERY_MIN)
        else:
            due = not _red_alerted_within(root, now, ERROR_EVERY_MIN)
        if due:
            new.append(("error", error_text(now, red)))
            last_error = ts(now)
    new += trade_texts(root, now)
    if rep is not None:
        new.append(("daily", daily(root, rep)))
    queue += [{"kind": k, "at": ts(now), "text": text} for k, text in new]
    queue = [a for a in queue if now - _t(a["at"]) < timedelta(minutes=MAX_AGE_MIN)][-MAX_QUEUE:]

    sent, status = 0, "none"
    while queue and sent < MAX_SENDS:
        got = deliver(creds, queue[0]["text"], now)
        if got != "ok":
            status = got
            break
        queue.pop(0)
        sent += 1
        status = "ok"
    if state_ok and (json.dumps(queue), last_error) != before:
        try:
            save_key(root / config.STATE, KEY, {"queue": queue, "last_error": last_error})
        except Exception as e:  # noqa: BLE001 - the sends already made still count
            status = f"error:{type(e).__name__}"
    return status, sent


def main(argv):
    if argv != ["--test"]:
        print("usage: python -m core.alerts --test")
        return 2
    creds = secrets()
    out = "off" if creds is None else deliver(creds, test_text(), utcnow())
    print(out)
    return 0 if out == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
