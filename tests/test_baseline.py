"""Offline tests for BL-8: the random baseline book and core/report.py. No network."""
import contextlib
import csv
import io
import json
import random
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from adapters import base
from core import baseline, config, exits, report, tick
from core import desk as desk_mod
from core.book import Book
from tests.test_adapter_base import FRESH, FakeClient, fresh_pairs, load
from tests import test_tick
from tests.test_tick import T0, addr, detail, good, tick_rows
from tests.test_verify import GOOD


def dex(i, **kw):
    """DexScreener side of a lead that passes dex_skip()."""
    d = {k: GOOD[k] for k in ["symbol", "price", "liq_usd", "age_min", "sells_1h"]}
    return {**d, "pair": f"0x{i:064x}", "launchpad": "other", "fetch_errors": [], **kw}


class EligibleTest(unittest.TestCase):
    def test_rules(self):
        seen = {addr(1): dex(1), addr(2): dex(2, sells_1h=0), addr(3): dex(3, liq_usd=1),
                addr(4): dex(4, fetch_errors=["ds_pairs"]), addr(5): {"pair": "", "fetch_errors": []},
                addr(6): dex(6)}
        self.assertEqual(baseline.eligible(seen, held={addr(6)}), [addr(1)])

    def test_sorted_by_address(self):
        seen = {addr(3): dex(3), addr(1): dex(1), addr(2): dex(2)}
        self.assertEqual(baseline.eligible(seen, set()), [addr(1), addr(2), addr(3)])


class FactsDexTest(unittest.TestCase):
    """The real facts(): the dex dict is filled, and the return value is the same with or without it."""

    def routes(self):
        return {f"token-pairs/v1/base/{FRESH}": fresh_pairs(), f"contract_addresses={FRESH}": load("goplus_fresh")}

    def test_same_return_value(self):
        d = {}
        with_dex = base.facts(FRESH, now=T0, client=FakeClient(self.routes()), dex=d)
        without = base.facts(FRESH, now=T0, client=FakeClient(self.routes()))
        self.assertEqual(with_dex, without)
        self.assertEqual(d["liq_usd"], 60_000)

    def test_deferred_lead_keeps_its_dexscreener_facts(self):
        d, c = {}, FakeClient(self.routes())
        self.assertIsNone(base.facts(FRESH, now=T0, client=c, goplus_ok=lambda: False, dex=d))
        self.assertTrue(set(d) >= {"symbol", "price", "liq_usd", "age_min", "sells_1h", "pair", "launchpad"})
        self.assertEqual(d["fetch_errors"], [])
        self.assertFalse(any("gopluslabs" in url for _, url in c.calls))  # no GoPlus call, no extra request
        self.assertEqual(len(c.calls), 1)

    def test_launchpad_is_pool_shape_only(self):
        # the asci pool is v4 with no fee in the DexScreener answer: pool shape alone says "other"
        d = {}
        base.facts(FRESH, now=T0, client=FakeClient(self.routes()), dex=d)
        self.assertEqual(d["launchpad"], "other")

    def test_dex_skip_lead_still_fills_dex(self):
        d = {}
        f = base.facts(FRESH, now=T0, client=FakeClient({"token-pairs": load("dexscreener_fresh")}), dex=d)
        self.assertTrue(f["goplus_skipped"])
        self.assertEqual(baseline.eligible({FRESH: d}, set()), [])  # under the liquidity minimum


class BaselineTickTest(unittest.TestCase):
    """The tick with the adapter faked (same fakes as tests/test_tick.py TickTest)."""

    T = test_tick.TickTest  # referenced through the module so its own tests don't run twice here
    setUp, tearDown, run_tick, heartbeats = T.setUp, T.tearDown, T.run_tick, T.heartbeats

    def due_in(self, ticks):
        b = Book(baseline.NAME, self.root)
        b.s["ticks_since_buy"] = baseline.CADENCE - ticks
        b.save()

    def bl_rows(self, stage="EXECUTE"):
        return [r for r in tick_rows(self.root) if r["book"] == "baseline" and r["stage"] == stage]

    def hb(self):
        return detail(self.heartbeats()[-1])

    # ---- cadence ----
    def test_first_buy_on_the_12th_tick_then_every_12(self):
        bought = []
        for t in range(1, 25):
            self.found = [addr(t)]
            self.facts[addr(t)] = dict(good(t), honeypot=True)
            bought.append(self.run_tick(5 * t)["bl_bought"])
        self.assertEqual([i + 1 for i, x in enumerate(bought) if x], [12, 24])
        self.assertEqual(len(self.bl_rows()), 2)  # not due = no row
        self.assertEqual(Book(baseline.NAME, self.root).s["ticks_since_buy"], 0)

    def test_nothing_eligible_stays_due_and_buys_next_tick(self):
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = dict(good(1), sells_1h=0)
        n = self.run_tick(0)
        self.assertEqual((n["bl_due"], n["bl_eligible"], n["bl_bought"]), (1, 0, 0))
        row = self.bl_rows()[-1]
        self.assertEqual((row["action"], row["reason"], row["detail"]), ("SKIP", "no_eligible", "eligible=0"))
        self.found = [addr(2)]
        self.facts[addr(2)] = dict(good(2), honeypot=True)
        n = self.run_tick(5)
        self.assertEqual((n["bl_due"], n["bl_bought"]), (1, 1))
        self.assertIn(addr(2), Book(baseline.NAME, self.root).positions)

    def test_paused_logs_no_baseline_row_and_stays_due(self):
        self.due_in(1)
        self.found = [addr(1)]
        n = self.run_tick(0, PAUSED=True)
        self.assertEqual((n["bl_due"], n["bl_bought"]), (1, 0))
        self.assertEqual(self.bl_rows(), [])
        self.assertGreaterEqual(Book(baseline.NAME, self.root).s["ticks_since_buy"], baseline.CADENCE)

    # ---- eligibility in the tick ----
    def test_eligibility_in_the_tick(self):
        self.due_in(1)
        self.found = [addr(i) for i in range(1, 5)]
        self.facts[addr(1)] = dict(good(1), sells_1h=0)                 # excluded: nobody can sell
        self.facts[addr(2)] = dict(good(2), liq_usd=1)                  # excluded: goplus=skipped (liquidity)
        self.facts[addr(3)] = dict(good(3), honeypot=True)              # included: desk SKIP honeypot
        self.facts[addr(4)] = dict(good(4), goplus_pending=["honeypot"], honeypot=None)  # included: WATCH
        n = self.run_tick(0)
        self.assertEqual(n["bl_eligible"], 2)
        row = self.bl_rows()[-1]
        d = detail(row)
        self.assertEqual(d["eligible"], "2")
        self.assertEqual(d["desk"], {addr(3): "honeypot", addr(4): "WATCH"}[row["address"]])

    def test_deferred_leads_are_eligible(self):
        self.due_in(1)
        self.found = [addr(i) for i in range(1, 4)]
        n = self.run_tick(0, GOPLUS_MAX_PER_TICK=0)
        self.assertEqual((n["deferred"], n["bl_eligible"], n["bl_bought"]), (3, 3, 1))
        d = detail(self.bl_rows()[-1])
        self.assertEqual(d["desk"], "deferred")
        self.assertEqual(d["pool"], ",".join(addr(i)[2:8] for i in range(1, 4)))

    def test_baseline_held_token_excluded(self):
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = dict(good(1), honeypot=True)
        self.run_tick(0)
        self.due_in(1)
        self.quotes[addr(1)] = {"price": GOOD["price"], "liq_usd": GOOD["liq_usd"], "source": "pool"}
        self.found = [addr(1), addr(2)]
        self.facts[addr(2)] = dict(good(2), honeypot=True)
        n = self.run_tick(5)
        self.assertEqual((n["bl_positions"], n["bl_eligible"]), (1, 1))
        self.assertEqual(self.bl_rows()[-1]["address"], addr(2))

    def test_buy_row_logs_launchpad_seed_and_desk_verdict(self):
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)  # desk PASS and buys too
        self.run_tick(0)
        row = self.bl_rows()[-1]
        d = detail(row)
        self.assertEqual((row["action"], d["desk"], d["launchpad"], d["seed"]),
                         ("BUY", "PASS", "clanker", "202610231200"))
        self.assertTrue(row["detail"].startswith("usd="))
        with (self.root / "data/daily/2026-10-23/trades.csv").open() as fh:
            trades = [r for r in csv.DictReader(fh) if r["book"] == "baseline"]
        # 80k liquidity: 5% thin + 1% Clanker fee
        self.assertEqual([(t["side"], t["launchpad"], t["friction_pct"]) for t in trades], [("BUY", "clanker", "6.0")])

    # ---- separate MAX_OPEN ----
    def test_max_open_is_per_book(self):
        bl = Book(baseline.NAME, self.root)
        for i in range(90, 90 + config.MAX_OPEN):
            bl.positions[addr(i)] = {"symbol": "X", "qty": 1, "orig_qty": 1, "cost_usd": 5, "entry_intended": 1,
                                     "entry_fill": 1, "entry_ts": "2026-10-23T11:55:00Z", "entry_liq_usd": 1e6,
                                     "launchpad": "other", "pair": "", "last_price": 1, "peak_price": 1,
                                     "ladder_done": []}
            self.quotes[addr(i)] = {"price": 1, "liq_usd": 1e6, "source": "pool"}
        bl.s["ticks_since_buy"] = baseline.CADENCE - 1
        bl.save()
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        n = self.run_tick(0)
        self.assertEqual((n["bought"], n["bl_bought"]), (1, 0))  # the full baseline never blocks the desk
        row = self.bl_rows()[-1]
        self.assertEqual((row["action"], row["reason"]), ("SKIP", "max_open"))
        self.assertIn("seed=", row["detail"])
        self.assertEqual(Book(baseline.NAME, self.root).s["ticks_since_buy"], baseline.CADENCE)  # still due

    def test_full_desk_does_not_block_the_baseline(self):
        desk = Book("desk", self.root)
        for i in range(90, 90 + config.MAX_OPEN):
            desk.positions[addr(i)] = {"symbol": "X", "qty": 1, "orig_qty": 1, "cost_usd": 5, "entry_intended": 1,
                                       "entry_fill": 1, "entry_ts": "2026-10-23T11:55:00Z", "entry_liq_usd": 1e6,
                                       "launchpad": "other", "pair": "", "last_price": 1, "peak_price": 1,
                                       "ladder_done": []}
            self.quotes[addr(i)] = {"price": 1, "liq_usd": 1e6, "source": "pool"}
        desk.save()
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        n = self.run_tick(0)
        self.assertEqual((n["bought"], n["bl_bought"]), (0, 1))

    # ---- replay ----
    def test_replay_from_the_seed(self):
        self.due_in(1)
        self.found = [addr(i) for i in range(1, 8)]
        self.facts.update({addr(i): dict(good(i), honeypot=True) for i in range(1, 8)})
        self.run_tick(0)
        row = self.bl_rows()[-1]
        d = detail(row)
        pool = sorted(addr(i) for i in range(1, 8))
        self.assertEqual([a[2:8] for a in pool], d["pool"].split(","))
        i = random.Random(int(d["seed"])).randrange(int(d["eligible"]))
        self.assertEqual((str(i), pool[i]), (d["pick"], row["address"]))
        # same leads, same time, fresh state: same pick
        first = row["address"]
        test_tick.TickTest.tearDown(self)
        test_tick.TickTest.setUp(self)
        self.due_in(1)
        self.found = [addr(i) for i in range(1, 8)]
        self.facts.update({addr(i): dict(good(i), honeypot=True) for i in range(1, 8)})
        self.run_tick(0)
        self.assertEqual(self.bl_rows()[-1]["address"], first)

    # ---- exits ----
    def test_baseline_exits_carry_its_book(self):
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = dict(good(1), honeypot=True)
        self.run_tick(0)
        self.quotes[addr(1)] = {"price": GOOD["price"] / 2, "liq_usd": GOOD["liq_usd"], "source": "pool"}
        self.found = []
        self.run_tick(5)
        exit_row = self.bl_rows("EXIT")[-1]
        self.assertEqual((exit_row["action"], exit_row["reason"]), ("SELL", "hard_stop"))
        self.assertEqual(Book(baseline.NAME, self.root).positions, {})

    # ---- report ----
    def test_report_on_the_day_roll(self):
        self.due_in(1)
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)  # desk and baseline both buy it
        self.run_tick(0)
        self.quotes[addr(1)] = {"price": GOOD["price"] / 2, "liq_usd": GOOD["liq_usd"], "source": "pool"}
        self.found = []
        self.run_tick(5)  # both hard_stop
        self.assertFalse((self.root / report.REPORT).exists())
        self.run_tick(13 * 60)  # 01:00 UTC next day
        self.assertEqual(self.hb()["report"], "ok")
        r = json.loads((self.root / report.REPORT).read_text())
        self.assertEqual(set(r), {"as_of", "day", "start_balance", "desk_lead_usd", "gate_b", "desk", "baseline",
                                  "summary"})
        self.assertEqual((r["day"], r["start_balance"], r["as_of"]), ("2026-10-23", 50.0, "2026-10-24T01:00:00Z"))
        self.assertEqual(r["gate_b"], {"desk_closes": 1, "target": 100, "kill": 200})
        for name in ("desk", "baseline"):
            b = r[name]
            self.assertEqual(set(b), {"value_usd", "cash_usd", "net_pnl_usd", "net_pnl_pct", "multiple",
                                      "equity_daily", "closes", "closes_day", "open", "win_rate", "avg_win_pct",
                                      "avg_loss_pct", "max_dd_pct", "stop_slip_pct", "exits_by_rule"})
            self.assertEqual((b["closes"], b["closes_day"], b["open"], b["win_rate"]), (1, 1, 0, 0.0))
            self.assertEqual(b["exits_by_rule"], {"liq_pull": 0, "hard_stop": 1, "ladder_1": 0, "ladder_2": 0,
                                                  "trail": 0, "time_stop": 0})
            self.assertLess(b["avg_loss_pct"], -50)
            self.assertLess(b["stop_slip_pct"], 0)
            self.assertGreater(b["max_dd_pct"], 0)
            self.assertEqual(b["equity_daily"], [{"day": "2026-10-23", "value_usd": b["value_usd"]}])
            self.assertEqual(b["net_pnl_usd"], round(b["value_usd"] - 50, 2))
        self.assertEqual(r["desk_lead_usd"], round(r["desk"]["value_usd"] - r["baseline"]["value_usd"], 2))
        self.assertIn("Gate B: 1/100", r["summary"])
        self.run_tick(13 * 60 + 5)
        self.assertEqual(self.hb()["report"], "none")  # once per roll

    def test_report_failure_still_writes_the_tick_row_and_retries(self):
        self.run_tick(0)
        with mock.patch.object(report, "build", side_effect=KeyError("x")):
            with self.assertRaises(KeyError):
                self.run_tick(13 * 60)
        hb = self.heartbeats()[-1]
        self.assertEqual((hb["reason"], detail(hb)["report"]), ("", "error:KeyError"))
        self.assertEqual(detail(hb)["seen"], "0")  # the tick still ran its scan
        self.assertEqual(report.load(self.root, T0)["due"], "2026-10-23")
        self.run_tick(13 * 60 + 5)
        self.assertEqual(self.hb()["report"], "ok")
        self.assertEqual(json.loads((self.root / report.REPORT).read_text())["day"], "2026-10-23")

    def test_by_hand_all_zeros(self):
        with contextlib.redirect_stdout(io.StringIO()):
            report.main([str(self.root)])
        r = json.loads((self.root / report.REPORT).read_text())
        self.assertEqual((r["desk"]["value_usd"], r["desk"]["closes"], r["desk"]["win_rate"],
                          r["desk"]["max_dd_pct"], r["desk_lead_usd"]), (50.0, 0, 0.0, 0.0, 0.0))
        self.assertEqual(r["desk"]["equity_daily"], [])
        self.assertFalse((self.root / config.STATE).exists())  # by hand only reads state


class BookStatsTest(unittest.TestCase):
    def row(self, side, reason, usd, pnl="", ts="2026-10-23T12:00:00Z", fill=1.0, intended=1.0, fric=3.0, a="0xa"):
        return {"ts": ts, "book": "baseline", "side": side, "address": a, "usd": str(usd), "reason": reason,
                "pnl_usd": str(pnl), "fill_price": str(fill), "intended_price": str(intended),
                "friction_pct": str(fric)}

    def test_ladder_is_not_a_close_and_pnl_is_per_position(self):
        rows = [self.row("BUY", "entry", 10),
                self.row("SELL", "ladder_1", 4, pnl=1),
                self.row("SELL", "trail", 8, pnl=2, fill=0.97, intended=1.0),
                self.row("BUY", "entry", 10, a="0xb"),
                self.row("SELL", "hard_stop", 6, pnl=-4, a="0xb", fill=0.679, intended=0.75)]
        s = report.book_stats(rows, "baseline", "2026-10-23", "2026-10-23")
        self.assertEqual((s["closes"], s["closes_day"], s["win_rate"]), (2, 2, 50.0))
        self.assertEqual((s["avg_win_pct"], s["avg_loss_pct"]), (30.0, -40.0))
        self.assertEqual(s["exits_by_rule"]["ladder_1"], 1)
        # trail: quote 1.0 vs intended 1.0 -> 0; hard_stop: quote 0.7 vs 0.75 -> -6.67
        self.assertEqual(s["stop_slip_pct"], -3.33)

    def test_closes_before_bl8_and_after_the_day_not_counted(self):
        rows = [self.row("BUY", "entry", 10, ts="2026-10-22T12:00:00Z"),
                self.row("SELL", "time_stop", 9, pnl=-1, ts="2026-10-22T13:00:00Z"),
                self.row("BUY", "entry", 10, ts="2026-10-23T12:00:00Z"),
                self.row("SELL", "time_stop", 9, pnl=-1, ts="2026-10-24T00:10:00Z")]
        s = report.book_stats(rows, "baseline", "2026-10-23", "2026-10-23")
        self.assertEqual((s["closes"], s["exits_by_rule"]["time_stop"]), (0, 0))
        self.assertEqual(report.book_stats(rows, "desk", "2026-10-23", "2026-10-24")["closes"], 0)


class DeskUnchangedTest(unittest.TestCase):
    """The same ticks with the baseline buying, never due, or broken: desk rows and state equal."""

    run_tick = test_tick.TickTest.run_tick

    def ticks(self, root, baseline_on, breaks=()):
        """Three ticks; returns (TICK row details, all rows, state, errors raised). breaks = patchers to start."""
        t = self
        t.root, t.found, t.facts, t.quotes, t.order = root, [], {}, {}, []
        patches = list(breaks)
        if not baseline_on:
            patches.append(mock.patch.object(baseline, "tick_count", return_value=False))
        errors = []

        def run(minutes):
            try:
                t.run_tick(minutes)
            except Exception as e:  # a baseline error ends the run red, after the TICK row
                errors.append(e)

        for p in patches:
            p.start()
        try:
            b = Book(baseline.NAME, root)
            b.s["ticks_since_buy"] = baseline.CADENCE - 1
            b.save()
            t.found = [addr(i) for i in range(1, 15)]
            t.facts.update({addr(1): good(1), addr(2): dict(good(2), honeypot=True),
                            addr(3): dict(good(3), liq_usd=1), addr(4): dict(good(4), sells_1h=0)})
            run(0)
            for i in (1, 2, 3, 5, 6):  # desk and baseline held tokens move
                t.quotes[addr(i)] = {"price": GOOD["price"] * 1.5, "liq_usd": GOOD["liq_usd"], "source": "pool"}
            b = Book(baseline.NAME, root)
            b.s["ticks_since_buy"] = baseline.CADENCE - 1
            b.save()
            t.found = [addr(i) for i in range(5, 12)]
            t.facts.update({addr(5): good(5), addr(6): dict(good(6), honeypot=True)})
            run(5)
            run(13 * 60)
        finally:
            for p in patches:
                p.stop()
        rows = tick_rows(root)
        state = json.loads((root / config.STATE).read_text())
        return [detail(r) for r in rows if r["stage"] == "TICK"], rows, state, errors

    def assert_desk_same(self, on, off, keys):
        (hb_on, rows_on, st_on, _), (hb_off, rows_off, st_off, _) = on, off
        desk_on = [r for r in rows_on if r["book"] == "desk" and r["stage"] != "TICK"]
        desk_off = [r for r in rows_off if r["book"] == "desk" and r["stage"] != "TICK"]
        self.assertTrue(desk_on)
        self.assertEqual(desk_on, desk_off)
        self.assertEqual(st_on["desk"], st_off["desk"])
        self.assertEqual(st_on["leads"], st_off["leads"])
        self.assertEqual(len(hb_on), 3)
        for x, y in zip(hb_on, hb_off):
            self.assertEqual({k: x[k] for k in keys}, {k: y[k] for k in keys})
        self.assertEqual([r["reason"] for r in rows_on if r["stage"] == "TICK"], [""] * 3)  # the desk's tick is ok

    def test_desk_identical(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on, off = self.ticks(Path(a), True), self.ticks(Path(b), False)
        self.assertTrue(any(r["book"] == "baseline" and r["action"] == "BUY" for r in on[1]))
        self.assertEqual((on[3], off[3]), ([], []))
        self.assert_desk_same(on, off, tick.COUNTS + ["goplus"])

    def test_baseline_exit_error_leaves_the_desk_identical(self):
        real = exits.check

        def check(book, quote, now):
            if book.name == baseline.NAME:
                raise RuntimeError("baseline exits broke")
            return real(book, quote, now)

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on = self.ticks(Path(a), True, [mock.patch.object(exits, "check", check)])
            off = self.ticks(Path(b), False)
        # the baseline holds a token from tick 1, so its exits run (and raise) in every tick
        self.assert_desk_same(on, off, tick.COUNTS + ["goplus", "requests"])
        self.assertEqual([type(e) for e in on[3]], [RuntimeError] * 3)  # every run ends red
        self.assertEqual([hb["bl_error"] for hb in on[0]], ["RuntimeError"] * 3)
        errs = [r for r in on[1] if r["book"] == "baseline" and r["action"] == "ERROR"]
        self.assertEqual([(r["stage"], r["reason"]) for r in errs], [("EXIT", "error:RuntimeError")] * 3)
        # after a baseline exit error the baseline buy is skipped: no baseline EXECUTE row at all
        self.assertFalse([r for r in on[1] if r["book"] == "baseline" and r["stage"] == "EXECUTE"])
        self.assertEqual(off[0][0]["bl_error"], "none")

    def test_baseline_buy_error_leaves_the_desk_identical(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on = self.ticks(Path(a), True, [mock.patch.object(baseline, "buy", side_effect=KeyError("x"))])
            off = self.ticks(Path(b), False)
        self.assert_desk_same(on, off, tick.COUNTS + ["goplus", "requests"])
        # a failed buy leaves the baseline due, so every tick tries again (and fails again here)
        self.assertEqual([hb["bl_error"] for hb in on[0]], ["KeyError"] * 3)
        errs = [(r["stage"], r["reason"]) for r in on[1] if r["book"] == "baseline" and r["action"] == "ERROR"]
        self.assertEqual(errs, [("EXECUTE", "error:KeyError")] * 3)
        self.assertEqual(len(on[3]), 3)
        self.assertGreaterEqual(on[2]["baseline"]["ticks_since_buy"], baseline.CADENCE)


class LiquidationValueTest(unittest.TestCase):
    """report.json prices open positions as if sold now, at the friction a real exit would pay (CLAUDE.md 6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book(baseline.NAME, self.root)
        desk_mod.buy(self.book, "0xa", dict(good(1), price=1.0, liq_usd=150_000, launchpad="other"), T0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sold_at_last_quote_minus_sell_friction(self):
        p = self.book.positions["0xa"]
        self.assertAlmostEqual(report.liq_value(self.book), self.book.cash + p["qty"] * 1.0 * 0.97)
        self.assertLess(report.liq_value(self.book), self.book.value())

    def test_uses_the_last_quote_liquidity_and_launchpad(self):
        # the pool thins under 100k: 5% per side, and a Clanker pool adds its 1% fee
        exits.check(self.book, lambda a: {"price": 1.1, "liq_usd": 95_000, "source": "pool"},
                    T0 + timedelta(minutes=5))
        p = self.book.positions["0xa"]
        self.assertEqual(p["last_liq_usd"], 95_000)
        self.assertAlmostEqual(report.liq_value(self.book), self.book.cash + p["qty"] * 1.1 * 0.95)
        p["launchpad"] = "clanker"
        self.assertAlmostEqual(report.liq_value(self.book), self.book.cash + p["qty"] * 1.1 * 0.94)

    def test_same_as_a_real_exit(self):
        expected = report.liq_value(self.book)
        desk_mod.sell(self.book, "0xa", 1.0, None, "time_stop", 150_000, T0 + timedelta(minutes=50))
        self.assertAlmostEqual(self.book.cash, expected)

    def test_report_fields_use_it(self):
        s = report.load(self.root, T0)
        desk = Book("desk", self.root)
        report.track(s, [desk, self.book])
        report.on_roll(s, [desk, self.book], "2026-10-23")
        r = report.build(self.root, T0, "2026-10-23", s, [desk, self.book])
        v = round(report.liq_value(self.book), 2)
        bl = r["baseline"]
        self.assertEqual((bl["value_usd"], bl["equity_daily"]), (v, [{"day": "2026-10-23", "value_usd": v}]))
        self.assertEqual((bl["net_pnl_usd"], bl["multiple"]), (round(v - 50, 2), round(v / 50, 4)))
        self.assertEqual(r["desk_lead_usd"], round(50 - v, 2))
        # one buy then marked at the entry price: the drawdown is the round trip (buy and sell friction)
        self.assertEqual(bl["max_dd_pct"], round((50 - report.liq_value(self.book)) / 50 * 100, 2))
        self.assertGreater(bl["max_dd_pct"], 0.5)


if __name__ == "__main__":
    unittest.main()
