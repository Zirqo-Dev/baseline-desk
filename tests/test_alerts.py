"""Offline tests for BL-3: core/alerts.py (Telegram) and its own try in core/tick.py. No network: urlopen is faked."""
import contextlib
import io
import json
import re
import tempfile
import traceback
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from core import alerts, baseline, config, labels, live, report, tick
from core.book import Book, save_key
from tests import test_baseline, test_tick
from tests.test_live import traded, trades
from tests.test_tick import GOOD, T0, addr, detail, good, tick_rows

TOKEN = "123456789:FAKE-token_Abc-XyZ0987"
CHAT = "987650001"
OWNER = "FakeOwnerName"
HANDLE = "fake_owner_handle"
BOT = "fake_desk_bot"
SECRETS = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
FORBIDDEN = [TOKEN, CHAT, OWNER, HANDLE, BOT, "api.telegram.org", "bot123456789"]
REPLY_OK = json.dumps({"ok": True, "result": {
    "message_id": 7, "from": {"id": 1, "is_bot": True, "first_name": "Desk", "username": BOT},
    "chat": {"id": int(CHAT), "first_name": OWNER, "username": HANDLE, "type": "private"}, "text": "x"}}).encode()
ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = tick.run


class Reply:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTelegram:
    """Stands in for urllib.request.urlopen. mode: ok | http | url | timeout | notok | garbage."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def __call__(self, req, timeout=None):
        form = urllib.parse.parse_qs(req.data.decode())
        self.calls.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout,
                           "form": {k: v[0] for k, v in form.items()}})
        url = req.full_url
        if self.mode == "http":  # urllib's HTTPError carries the URL (token) and the reply
            raise urllib.error.HTTPError(url, 403, f"Forbidden {url} {OWNER}", {}, io.BytesIO(REPLY_OK))
        if self.mode == "url":
            raise urllib.error.URLError(f"<urlopen error {url} chat {CHAT}>")
        if self.mode == "timeout":
            raise TimeoutError(f"timed out {url}")
        if self.mode == "notok":
            return Reply(json.dumps({"ok": False, "description": f"chat {CHAT} of {OWNER} not found"}).encode())
        if self.mode == "garbage":  # JSONDecodeError's message quotes the reply
            return Reply(f"<html>{OWNER} {HANDLE} {CHAT}".encode())
        return Reply(REPLY_OK)

    @property
    def texts(self):
        return [c["form"]["text"] for c in self.calls]


def env(**kw):
    return mock.patch.dict("os.environ", kw or SECRETS)


def no_env():
    return mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})


def wire(fake):
    return mock.patch("urllib.request.urlopen", fake)


# the 2026-10-31 report.json values: closes (running, since BL-8) and closes_day (that day) differ for each book
REP = {"day": "2026-10-31", "desk": {"value_usd": 47.61, "closes": 1, "closes_day": 0},
       "baseline": {"value_usd": 27.62, "closes": 61, "closes_day": 22},
       "desk_lead_usd": 19.99, "gate_b": {"desk_closes": 1, "target": 100, "kill": 200}}


class FormatTest(unittest.TestCase):
    def test_buy(self):
        self.assertEqual(alerts.buy_text("SI", "0xfe80", 5, 0.00299009, 159111.34, 4321.4),
                         "Baseline desk BUY SI\n$5.00 at 0.00299009\nliq $159,111 · real depth $4,321\n"
                         "https://dexscreener.com/base/0xfe80")
        self.assertIn("real depth na", alerts.buy_text("SI", "0x1", 5, 1, 60000, "na"))

    def test_sell(self):
        self.assertEqual(alerts.sell_text("SI", "liq_pull", "-92.16", "-3.100623", 47.6078),
                         "Baseline desk SELL SI · liq_pull\nprice -92.16% · pnl -$3.10\ndesk cash after $47.61")
        self.assertEqual(alerts.sell_text("SI", "ladder_1", "47.35", "0.708392", None),
                         "Baseline desk SELL SI · ladder_1\nprice +47.35% · pnl +$0.71\ndesk cash after na")

    def test_daily(self):
        self.assertEqual(alerts.daily_text(REP, 288, 288, 0),
                         "Baseline daily · 2026-10-31\ndesk $47.61 · random $27.62 · desk lead +$19.99\n"
                         "Gate B 1/100 desk closes\ncloses that day: desk 0 · random 22\nruns 288/288 · ERROR rows 0")

    def test_daily_closes_that_day_is_closes_day_for_both_books(self):
        rep = dict(REP, desk=dict(REP["desk"], closes=40, closes_day=3), baseline=dict(REP["baseline"], closes=90, closes_day=7),
                   gate_b=dict(REP["gate_b"], desk_closes=40))
        lines = alerts.daily_text(rep, 287, 288, 2).splitlines()
        self.assertEqual(lines[2], "Gate B 40/100 desk closes")  # the running total belongs to Gate B only
        self.assertEqual(lines[3], "closes that day: desk 3 · random 7")
        self.assertNotRegex(lines[3], r"\b(40|90)\b")

    def test_error(self):
        self.assertEqual(alerts.error_text(T0, [("labels", "URLError"), ("live", "OSError")]),
                         "Baseline run RED · 2026-10-23 12:00 UTC\nlabels error:URLError\nlive error:OSError")
        self.assertEqual(alerts.error_text(T0, [("crash", "JSONDecodeError")]).splitlines()[1],
                         "crash error:JSONDecodeError")

    def test_test_message_is_marked_and_shaped_like_a_buy(self):
        t = alerts.test_text()
        self.assertTrue(t.startswith("TEST · Baseline desk BUY "))
        self.assertEqual([x.split()[0] for x in t.splitlines()[1:]], ["$5.00", "liq", "https://dexscreener.com/base/0x"
                                                                       + "0" * 40])

    def test_plain_text_symbols_and_request_shape(self):
        sym = "w_COIN*[BSTONK]_`x`"
        fake = FakeTelegram()
        with wire(fake):
            got = alerts.deliver((TOKEN, CHAT), alerts.buy_text(sym, "0x1", 5, 1, 60000, 1000), T0)
        self.assertEqual(got, "ok")
        call = fake.calls[0]
        self.assertEqual((call["method"], call["timeout"]), ("POST", 10))
        self.assertEqual(call["url"], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(call["form"]["chat_id"], CHAT)
        self.assertNotIn("parse_mode", call["form"])
        self.assertEqual(call["form"]["text"].splitlines()[0], f"Baseline desk BUY {sym}")  # verbatim, no escaping
        self.assertEqual(json.loads(call["form"]["link_preview_options"]), {"is_disabled": True})


class QuietHoursTest(unittest.TestCase):
    """22:00 to 07:00 America/Toronto; DST ends 2026-11-01 (EDT -> EST), starts 2026-03-08 (EST -> EDT)."""

    def q(self, *a):
        return alerts.quiet(datetime(*a, tzinfo=timezone.utc))

    def test_fall_change(self):
        self.assertFalse(self.q(2026, 11, 1, 1, 59))   # Oct 31 21:59 EDT
        self.assertTrue(self.q(2026, 11, 1, 2, 0))     # Oct 31 22:00 EDT
        self.assertTrue(self.q(2026, 11, 1, 10, 59))   # Nov 1 05:59 EST (after the change)
        self.assertTrue(self.q(2026, 11, 1, 11, 59))   # Nov 1 06:59 EST
        self.assertFalse(self.q(2026, 11, 1, 12, 0))   # Nov 1 07:00 EST
        self.assertFalse(self.q(2026, 11, 2, 2, 30))   # Nov 1 21:30 EST: same UTC time as a quiet one before
        self.assertTrue(self.q(2026, 11, 2, 3, 0))     # Nov 1 22:00 EST
        self.assertFalse(self.q(2026, 10, 31, 11, 0))  # Oct 31 07:00 EDT

    def test_spring_change(self):
        self.assertFalse(self.q(2026, 3, 7, 2, 59))    # Mar 6 21:59 EST
        self.assertTrue(self.q(2026, 3, 7, 3, 0))      # Mar 6 22:00 EST
        self.assertTrue(self.q(2026, 3, 7, 11, 30))    # Mar 7 06:30 EST (before the change)
        self.assertFalse(self.q(2026, 3, 9, 11, 30))   # Mar 9 07:30 EDT: same UTC time, other answer
        self.assertTrue(self.q(2026, 3, 8, 10, 59))    # Mar 8 06:59 EDT (after the change)
        self.assertFalse(self.q(2026, 3, 8, 11, 0))    # Mar 8 07:00 EDT
        self.assertTrue(self.q(2026, 3, 9, 2, 30))     # Mar 8 22:30 EDT
        self.assertFalse(self.q(2026, 3, 9, 1, 59))    # Mar 8 21:59 EDT

    def test_disable_notification_follows_quiet_hours(self):
        fake = FakeTelegram()
        with wire(fake):
            alerts.deliver((TOKEN, CHAT), "x", datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc))   # 23:00 EDT
            alerts.deliver((TOKEN, CHAT), "x", datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc))  # 13:00 EST
        self.assertEqual([c["form"]["disable_notification"] for c in fake.calls], ["true", "false"])



class TickAlertTest(unittest.TestCase):
    """The tick with the fake adapter (test_tick) and a fake Telegram."""

    run_tick = test_tick.TickTest.run_tick

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.found, self.facts, self.quotes, self.order = [], {}, {}, []

    def tearDown(self):
        self.tmp.cleanup()

    def hb(self):
        return detail([r for r in tick_rows(self.root) if r["stage"] == "TICK"][-1])

    def state(self):
        return json.loads((self.root / config.STATE).read_text())

    def tick(self, minutes=0, fake=None, secrets=True, **patches):
        fake = fake or FakeTelegram()
        with (env() if secrets else no_env()), wire(fake):
            n = self.run_tick(minutes, **patches)
        return n, fake

    def buy_and_sell(self, fake=None):
        """Tick 0 buys addr(1) (desk, qliq 43210, over the BL-32 floor); tick 5 sells it on the hard stop."""
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1, qliq=43210)
        a = self.tick(0, fake)
        self.quotes[addr(1)] = {"price": GOOD["price"] / 2, "liq_usd": GOOD["liq_usd"], "source": "pool"}
        return a, self.tick(5, fake)

    # -- off --
    def test_no_secrets_no_request(self):
        fake = FakeTelegram()
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1)
        n, _ = self.tick(0, fake, secrets=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual((n["alerts"], n["alerts_sent"]), ("off", 0))
        self.assertEqual((self.hb()["alerts"], self.hb()["alerts_sent"]), ("off", "0"))
        self.assertEqual(list(self.hb())[-2:], ["alerts", "alerts_sent"])
        self.assertNotIn("alerts", self.state())  # nothing queued, state untouched
        for e in ({"TELEGRAM_BOT_TOKEN": TOKEN}, {"TELEGRAM_CHAT_ID": CHAT}, {}):
            with wire(fake):
                self.assertEqual(alerts.run(self.root, T0, [("crash", "X")], REP, env=e), ("off", 0))
        self.assertEqual(fake.calls, [])

    # -- real alerts from the tick's rows --
    def test_buy_and_sell_alerts(self):
        (n0, f0), (n1, f1) = self.buy_and_sell()
        self.assertEqual((n0["alerts"], n0["alerts_sent"]), ("ok", 1))
        cash = Book("desk", self.root).cash
        self.assertEqual(f0.texts[0], alerts.buy_text("T1", addr(1), "5.0", desk_fill(self.root), GOOD["liq_usd"], "43210"))
        self.assertIn("real depth $43,210", f0.texts[0])
        self.assertEqual((n1["alerts"], n1["alerts_sent"]), ("ok", 1))
        sell = f1.texts[0].splitlines()
        self.assertEqual(sell[0], "Baseline desk SELL T1 · hard_stop")
        self.assertTrue(sell[1].startswith("price -5"))
        self.assertEqual(sell[2], f"desk cash after ${cash:,.2f}")
        self.assertNotIn("alerts", self.state())  # sent the same tick: nothing waited, state untouched
        self.assertEqual(self.hb()["alerts"], "ok")

    def test_baseline_trades_send_nothing(self):
        b = Book(baseline.NAME, self.root)
        b.s["ticks_since_buy"] = baseline.CADENCE - 1
        b.save()
        self.found = [addr(2)]
        self.facts[addr(2)] = dict(good(2), honeypot=True)  # desk skips, the baseline may buy it
        n, fake = self.tick(0)
        self.assertTrue(any(r["book"] == baseline.NAME and r["action"] == "BUY" for r in tick_rows(self.root)))
        self.assertEqual((n["alerts"], n["alerts_sent"], fake.calls), ("none", 0, []))

    def test_daily_summary_after_the_day_roll_report(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1)
        self.tick(0)
        self.found = []
        n, fake = self.tick(13 * 60)  # 2026-10-24 01:00 UTC: the roll writes the Oct 23 report
        self.assertEqual(n["report"], "ok")
        rep = json.loads((self.root / report.REPORT).read_text())
        daily = [t for t in fake.texts if t.startswith("Baseline daily")]
        self.assertEqual(daily, [alerts.daily_text(rep, 1, 288, 0)])
        self.assertIn("runs 1/288 · ERROR rows 0", daily[0])

    def test_no_daily_summary_when_the_report_fails(self):
        self.tick(0)
        fake = FakeTelegram()
        with mock.patch.object(report, "run_due", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):  # the report error ends the run red, not the alert
                self.tick(13 * 60, fake)
        self.assertEqual(self.hb()["report"], "error:RuntimeError")
        self.assertEqual(fake.texts, [alerts.error_text(T0 + timedelta(hours=13), [("report", "RuntimeError")])])
        self.assertEqual((self.hb()["alerts"], self.hb()["alerts_sent"]), ("ok", "1"))

    # -- red runs --
    def test_error_alert_names_the_step_once_per_hour(self):
        fake = FakeTelegram()
        broken = mock.patch.object(labels, "run", side_effect=OSError("disk"))
        broken.start()
        self.addCleanup(broken.stop)
        for m in (0, 5, 55, 60, 65):
            with self.assertRaises(OSError):  # the labels error ends the run red, not the alert
                self.tick(m, fake)
        self.assertEqual(fake.texts, [alerts.error_text(T0, [("labels", "OSError")]),
                                      alerts.error_text(T0 + timedelta(minutes=60), [("labels", "OSError")])])
        self.assertEqual(self.state()["alerts"]["last_error"], "2026-10-23T13:00:00Z")

    def test_crash_still_sends_the_crash_alert(self):
        fake = FakeTelegram()
        self.tick(0, fake)
        before = (self.root / live.LIVE).read_text()
        (self.root / config.STATE).write_text("{not json")
        for m in (5, 10):
            with self.assertRaises(ValueError):  # the crash still re-raises: run red
                self.tick(m, fake)
            self.assertEqual(self.hb()["live"], "none")
        self.assertEqual(fake.texts, [alerts.error_text(T0 + timedelta(minutes=5), [("crash", "JSONDecodeError")])])
        self.assertEqual([detail(r)["alerts_sent"] for r in tick_rows(self.root) if r["stage"] == "TICK"],
                         ["0", "1", "0"])  # the second crash in the hour: no alert (TICK rows, the state is broken)
        self.assertEqual((self.root / live.LIVE).read_text(), before)  # no live.json on a crash
        self.assertEqual((self.root / config.STATE).read_text(), "{not json")  # never "repaired" by alerts

    def test_crash_alert_with_a_readable_state(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        fake = FakeTelegram()
        with mock.patch("core.desk.buy", side_effect=ZeroDivisionError("boom")):
            with self.assertRaises(ZeroDivisionError):
                self.tick(0, fake)
        self.assertEqual(fake.texts, [alerts.error_text(T0, [("crash", "ZeroDivisionError")])])
        self.assertEqual(self.hb()["alerts"], "ok")

    # -- failures never touch the run --
    def test_failing_send_keeps_every_result_and_the_run_green(self):
        def three(root, mode):
            self.root, self.found, self.facts, self.quotes, self.order = root, [], {}, {}, []
            ticks = test_baseline.DeskUnchangedTest.ticks
            patches = [env(), wire(FakeTelegram(mode))] if mode else [no_env()]
            on = ticks(self, root, True, patches)
            st = on[2]
            st.pop("alerts", None)
            return (on[0], on[1], st, on[3], json.loads((root / live.LIVE).read_text()),
                    json.loads((root / report.REPORT).read_text()), trades(root))

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            off, bad = three(Path(a), None), three(Path(b), "url")
        self.assertEqual([h["alerts"] for h in off[0]], ["off"] * 3)
        self.assertEqual([h["alerts"] for h in bad[0]], ["error:URLError"] * 3)
        self.assertEqual((off[3], bad[3]), ([], []))  # no run ends red
        strip = lambda hb: {k: v for k, v in hb.items() if not k.startswith("alerts")}
        self.assertEqual([strip(h) for h in off[0]], [strip(h) for h in bad[0]])
        self.assertEqual([r for r in off[1] if r["stage"] != "TICK"], [r for r in bad[1] if r["stage"] != "TICK"])
        self.assertTrue(any(r["book"] == baseline.NAME and r["action"] == "BUY" for r in off[1]))
        self.assertEqual(off[2], bad[2])  # desk, baseline, leads, report state
        self.assertEqual(off[4], bad[4])  # live.json
        self.assertEqual(off[5], bad[5])  # report.json
        self.assertEqual(off[6], bad[6])  # trades.csv
        self.assertEqual([r["reason"] for r in bad[1] if r["stage"] == "TICK"], [""] * 3)

    def test_alerts_raising_is_logged_never_red(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        with mock.patch.object(alerts, "run", side_effect=RuntimeError("x")):
            n, _ = self.tick(0)
        self.assertEqual((n["alerts"], n["alerts_sent"]), ("error:RuntimeError", 0))
        self.assertEqual([r["reason"] for r in tick_rows(self.root) if r["stage"] == "TICK"], [""])
        self.assertIn(addr(1), Book("desk", self.root).positions)

    # -- queue --
    def test_retry_next_tick_then_drop_after_an_hour(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1)
        n, fake = self.tick(0, FakeTelegram("timeout"))
        self.assertEqual((n["alerts"], n["alerts_sent"], len(fake.calls)), ("error:TimeoutError", 0, 1))
        q = self.state()["alerts"]["queue"]
        self.assertEqual([(a["kind"], a["at"]) for a in q], [("buy", "2026-10-23T12:00:00Z")])
        n, fake = self.tick(5)  # the next tick sends it
        self.assertEqual((n["alerts"], n["alerts_sent"]), ("ok", 1))
        self.assertTrue(fake.texts[0].startswith("Baseline desk BUY T1"))
        self.assertEqual(self.state()["alerts"]["queue"], [])
        # a failed alert waits at most an hour
        with wire(FakeTelegram("http")):
            self.assertEqual(alerts.run(self.root, T0 + timedelta(minutes=10), [("live", "OSError")], env=SECRETS),
                             ("error:HTTPError", 0))
        self.assertEqual(len(self.state()["alerts"]["queue"]), 1)
        with wire(FakeTelegram("notok")):
            self.assertEqual(alerts.run(self.root, T0 + timedelta(minutes=69), env=SECRETS), ("error:NotOk", 0))
        self.assertEqual(len(self.state()["alerts"]["queue"]), 1)
        fake = FakeTelegram()
        with wire(fake):
            self.assertEqual(alerts.run(self.root, T0 + timedelta(minutes=70), env=SECRETS), ("none", 0))
        self.assertEqual((fake.calls, self.state()["alerts"]["queue"]), ([], []))

    def test_max_three_sends_and_twenty_waiting(self):
        old = [{"kind": "sell", "at": "2026-10-23T11:59:00Z", "text": f"old {i}"} for i in range(25)]
        save_key(self.root / config.STATE, "alerts", {"queue": old, "last_error": ""})
        fake = FakeTelegram()
        with wire(fake):
            self.assertEqual(alerts.run(self.root, T0, env=SECRETS), ("ok", 3))
        self.assertEqual(fake.texts, ["old 5", "old 6", "old 7"])  # 20 kept (the oldest 5 dropped), 3 sent
        self.assertEqual(len(self.state()["alerts"]["queue"]), 17)
        with wire(fake):
            self.assertEqual(alerts.run(self.root, T0 + timedelta(minutes=5), env=SECRETS), ("ok", 3))
        self.assertEqual(len(fake.calls), 6)

    def test_first_failure_stops_the_tick_sends(self):
        old = [{"kind": "sell", "at": "2026-10-23T11:59:00Z", "text": f"old {i}"} for i in range(5)]
        save_key(self.root / config.STATE, "alerts", {"queue": old, "last_error": ""})
        fake = FakeTelegram("timeout")
        with wire(fake):
            self.assertEqual(alerts.run(self.root, T0, env=SECRETS), ("error:TimeoutError", 0))
        self.assertEqual(len(fake.calls), 1)  # one 10 s timeout per tick at most
        self.assertEqual(len(self.state()["alerts"]["queue"]), 5)

    def test_same_requests_with_alerts_on_or_off(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1)
        n_on, fake = self.tick(0)
        self.assertEqual(len(fake.calls), 1)
        with tempfile.TemporaryDirectory() as other:
            self.root = Path(other)
            n_off, _ = self.tick(0, secrets=False)
        self.assertEqual(n_on["requests"], n_off["requests"])  # no data request added


def desk_fill(root):
    return [t for t in trades(root) if t["book"] == "desk" and t["side"] == "BUY"][-1]["fill_price"]


class LeakTest(unittest.TestCase):
    """B: the fake token, chat id and anything from Telegram's reply never reach stdout, stderr, data/ or an error."""

    run_tick = test_tick.TickTest.run_tick

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.found, self.facts, self.quotes, self.order = [], {}, {}, []

    def tearDown(self):
        self.tmp.cleanup()

    def assert_clean(self, text, where):
        for s in FORBIDDEN:
            self.assertNotIn(s, text, f"{s!r} leaked into {where}")

    def assert_data_clean(self):
        files = [p for p in (self.root / "data").rglob("*") if p.is_file()]
        names = {p.name for p in files}
        self.assertTrue({"tasks.csv", "state.json", "live.json"} <= names, names)
        for p in files:
            self.assert_clean(p.read_text(encoding="utf-8"), p.name)

    def test_tick_paths(self):
        for mode in ("ok", "http", "url", "timeout", "notok", "garbage"):
            with self.subTest(mode=mode):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory()
                self.root = Path(self.tmp.name)
                self.found, self.facts, self.quotes = [addr(1)], {addr(1): traded(1)}, {}
                out, err = io.StringIO(), io.StringIO()
                raised = []
                fake = FakeTelegram(mode)
                with env(), wire(fake), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.run_tick_print(0)
                    self.quotes[addr(1)] = {"price": GOOD["price"] / 2, "liq_usd": GOOD["liq_usd"], "source": "pool"}
                    with mock.patch.object(labels, "run", side_effect=OSError("disk")):
                        try:
                            self.run_tick_print(5)
                        except Exception as e:  # the labels error ends the run red
                            raised.append(e)
                    self.run_tick_print(10)
                self.assertTrue(fake.calls)
                self.assertEqual(len(raised), 1)
                tb = "".join(traceback.format_exception(raised[0]))
                self.assert_clean(tb, "the raised error")
                self.assert_clean(out.getvalue(), "stdout")
                self.assert_clean(err.getvalue(), "stderr")
                self.assertIn("alerts=", out.getvalue())
                self.assert_data_clean()
                for hb in [detail(r) for r in tick_rows(self.root) if r["stage"] == "TICK"]:
                    self.assertRegex(hb["alerts"], r"^(ok|none|error:[A-Za-z]+)$")

    def run_tick_print(self, minutes):
        """test_tick's fake adapter, but with log=print: what the public Actions log would show."""
        with mock.patch.object(tick, "run", lambda *a, **k: REAL_RUN(*a, **dict(k, log=print))):
            return test_tick.TickTest.run_tick(self, minutes)

    def test_deliver_returns_only_the_type_name(self):
        for mode, want in (("ok", "ok"), ("http", "error:HTTPError"), ("url", "error:URLError"),
                           ("timeout", "error:TimeoutError"), ("notok", "error:NotOk"),
                           ("garbage", "error:JSONDecodeError")):
            with wire(FakeTelegram(mode)):
                self.assertEqual(alerts.deliver((TOKEN, CHAT), "x", T0), want)

    def test_cli_test_path(self):
        for mode, envs, want, code in (("ok", SECRETS, "ok", 0), ("http", SECRETS, "error:HTTPError", 1),
                                       ("url", SECRETS, "error:URLError", 1), ("garbage", SECRETS, "error:JSONDecodeError", 1),
                                       ("ok", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": CHAT}, "off", 1)):
            with self.subTest(mode=mode, want=want):
                out, err, fake = io.StringIO(), io.StringIO(), FakeTelegram(mode)
                with mock.patch.dict("os.environ", envs), wire(fake), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = alerts.main(["--test"])
                self.assertEqual((out.getvalue(), err.getvalue(), rc), (want + "\n", "", code))
                self.assertEqual(len(fake.calls), 0 if want == "off" else 1)
                if fake.calls:
                    self.assertTrue(fake.calls[0]["form"]["text"].startswith("TEST · Baseline desk BUY"))

    def test_cli_test_writes_nothing(self):
        before = sorted(p for p in ROOT.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts)
        stamp = {p: p.stat().st_mtime_ns for p in before if p.is_file()}
        with env(), wire(FakeTelegram()), contextlib.redirect_stdout(io.StringIO()):
            alerts.main(["--test"])
        after = sorted(p for p in ROOT.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts)
        self.assertEqual(before, after)
        self.assertEqual(stamp, {p: p.stat().st_mtime_ns for p in after if p.is_file()})

    def test_no_print_or_str_of_exceptions_in_alerts(self):
        src = (ROOT / "core/alerts.py").read_text()
        self.assertEqual(re.findall(r"print\(", src), ["print(", "print("])  # usage and the ok/off/error line
        self.assertNotRegex(src, r"str\(e\)|\{e\}|repr\(e\)|\braise\b")
        self.assertNotIn("parse_mode\"", src)


class WorkflowTest(unittest.TestCase):
    def text(self, name):
        return (ROOT / ".github/workflows" / name).read_text()

    def test_alert_test_is_dispatch_only_read_only(self):
        y = self.text("alert-test.yml")
        on = re.search(r"^on:\n((?:  .*\n)+)", y, re.M).group(1)
        self.assertEqual(on.strip(), "workflow_dispatch:")
        self.assertNotRegex(y, r"\b(schedule|push|pull_request|workflow_run|repository_dispatch):")
        self.assertRegex(y, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotIn("write", y)
        self.assertNotIn("git commit", y)
        self.assertNotIn("git push", y)
        self.assertIn("persist-credentials: false", y)
        self.assertIn("run: python -m core.alerts --test", y)

    def test_secrets_only_in_their_step(self):
        t = self.text("tick.yml")
        steps = re.split(r"\n      - ", t)
        with_secrets = [s for s in steps if "secrets." in s]
        self.assertEqual(len(with_secrets), 1)
        self.assertTrue(with_secrets[0].startswith("name: Tick"))
        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", with_secrets[0])
        self.assertIn("TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}", with_secrets[0])
        self.assertNotRegex(t.split("\njobs:")[0], r"secrets\.|TELEGRAM")  # no workflow-level env
        a = self.text("alert-test.yml")
        self.assertEqual([s for s in re.split(r"\n      - ", a) if "secrets." in s][0].split("\n")[0],
                         "name: Send test alert")

    def test_no_debug_output(self):
        for name in ("tick.yml", "alert-test.yml"):
            y = self.text(name)
            for bad in ("set -x", "set -o xtrace", "printenv", "env |", "| env", "toJSON(secrets", "toJson(secrets",
                        "ACTIONS_STEP_DEBUG", "ACTIONS_RUNNER_DEBUG", "echo $TELEGRAM", "echo \"$TELEGRAM", "${TELEGRAM"):
                self.assertNotIn(bad, y, f"{bad!r} in {name}")
            self.assertNotRegex(y, r"(?m)^\s*(run:\s*)?env\s*$")


if __name__ == "__main__":
    unittest.main()
