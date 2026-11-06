"""Offline tests for BL-27: data/live.json (core/live.py) and its own try in core/tick.py. No network."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from core import baseline, config, desk, exits, friction, live, report, tick
from core.book import TASK_FIELDS, Book, _append, ts
from tests import test_baseline, test_tick
from tests.test_tick import T0, addr, detail, good, tick_rows

TOP = ["as_of", "last_tick", "today", "exit", "books"]
POS = ["symbol", "address", "entry_ts", "age_min", "cost_usd", "value_usd", "value_pct", "price_pct", "liq_usd",
       "qliq", "ladders_done", "time_stop_at"]
N = {"seen": 7, "new": 3, "checked": 4, "dex_only": 2, "goplus": 2, "passed": 1, "bought": 1, "requests": 11,
     "secs": 4.2, "positions": 0, "held": 0, "labels": "ok"}


def traded(i, qliq=12345):
    return dict(good(i), trade={"b1": 1, "s1": 1, "b24": 9, "s24": 9, "liq": 60000, "dex": "uniswap:v4", "qliq": qliq})


def trades(root):
    out = []
    for p in sorted((root / "data/daily").glob("*/trades.csv")):
        with p.open() as fh:
            out += list(csv.DictReader(fh))
    return out


class Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ShapeTest(Root):
    def setUp(self):
        super().setUp()
        self.desk = Book("desk", self.root)
        desk.buy(self.desk, addr(1), dict(traded(1), price=1.0, liq_usd=150_000), T0)
        desk.buy(self.desk, addr(2), dict(good(2), price=2.0, liq_usd=80_000), T0 + timedelta(minutes=5))
        self.desk.save()
        bl = Book(baseline.NAME, self.root)
        desk.buy(bl, addr(3), dict(traded(3, qliq="na"), price=0.5, liq_usd=60_000, launchpad="clanker"), T0)
        bl.save()

    def build(self, minutes=20):
        return live.build(self.root, T0 + timedelta(minutes=minutes), N, 0)

    def test_shape(self):
        d = self.build()
        self.assertEqual(list(d), TOP)
        self.assertEqual(d["as_of"], "2026-10-23T12:20:00Z")
        self.assertEqual(d["last_tick"], {"ts": "2026-10-23T12:20:00Z", "seen": 7, "new": 3, "checked": 4,
                                          "dex_only": 2, "goplus": 2, "passed": 1, "bought": 1, "exits": 0,
                                          "requests": 11, "secs": 4.2})
        self.assertEqual(d["exit"], {"hard_stop_pct": -25, "time_stop_min": 45, "time_stop_min_pct": 10, "trail_pct": 25})
        self.assertEqual(list(d["books"]), ["desk", "baseline"])
        for b in d["books"].values():
            self.assertEqual(list(b), ["cash_usd", "value_usd", "open"])
            for p in b["open"]:
                self.assertEqual(list(p), POS)
        self.assertEqual([p["address"] for p in d["books"]["desk"]["open"]], [addr(1), addr(2)])  # oldest first
        p = d["books"]["desk"]["open"][0]
        self.assertEqual((p["symbol"], p["entry_ts"], p["age_min"], p["ladders_done"]),
                         ("T1", "2026-10-23T12:00:00Z", 20, []))
        self.assertLess(len(json.dumps(d, indent=1)), 3000)

    def test_values_match_report_liq_value(self):
        d = self.build()
        for name in report.BOOKS:
            b = Book(name, self.root)
            card = d["books"][name]
            self.assertAlmostEqual(card["value_usd"], report.liq_value(b), places=4)
            self.assertAlmostEqual(card["cash_usd"], b.cash, places=4)
            self.assertAlmostEqual(card["cash_usd"] + sum(p["value_usd"] for p in card["open"]), card["value_usd"], places=3)
        p = d["books"]["baseline"]["open"][0]  # clanker at 60k: 5% + 1% each side
        self.assertAlmostEqual(p["value_usd"], 5.0 / 1.06 * 0.94, places=4)
        self.assertAlmostEqual(p["value_pct"], (0.94 / 1.06 - 1) * 100, places=2)

    def test_price_pct_is_the_exit_row_pct_and_liq_is_the_last(self):
        exits.check(self.desk, lambda a: {"price": 1.1, "liq_usd": 140_000, "source": "pool"}, T0 + timedelta(minutes=10))
        p = self.build()["books"]["desk"]["open"][0]
        row = [r for r in tick_rows(self.root) if r["stage"] == "EXIT" and r["address"] == addr(1)][-1]
        self.assertEqual(f"{p['price_pct']:.2f}", detail(row)["pct"])
        self.assertEqual(p["liq_usd"], 140_000)

    def test_ladder_partial_cost_basis(self):
        exits.check(self.desk, lambda a: {"price": 1.5, "liq_usd": 150_000, "source": "pool"} if a == addr(1) else None,
                    T0 + timedelta(minutes=10))
        pos = Book("desk", self.root).positions[addr(1)]
        p = self.build()["books"]["desk"]["open"][0]
        self.assertEqual(p["ladders_done"], ["ladder_1"])
        self.assertAlmostEqual(p["cost_usd"], 5.0 * (1 - 0.33), places=4)  # the remaining qty's cost
        self.assertAlmostEqual(p["cost_usd"], pos["cost_usd"], places=4)
        self.assertAlmostEqual(p["value_usd"], pos["qty"] * friction.sell_fill(1.5, 150_000, pos["launchpad"])[0], places=4)
        self.assertAlmostEqual(p["value_pct"], (p["value_usd"] / p["cost_usd"] - 1) * 100, places=1)

    def test_age_never_negative(self):
        self.assertEqual(live.build(self.root, T0 - timedelta(minutes=3), N, 0)["books"]["desk"]["open"][0]["age_min"], 0)

    def test_time_stop_at(self):
        d = self.build()
        self.assertEqual([p["time_stop_at"] for p in d["books"]["desk"]["open"]],
                         ["2026-10-23T12:45:00Z", "2026-10-23T12:50:00Z"])

    def test_qliq_from_entry_or_null(self):
        d = self.build()
        self.assertEqual([p["qliq"] for p in d["books"]["desk"]["open"]], [12345, None])  # good(2) has no trade
        self.assertIsNone(d["books"]["baseline"]["open"][0]["qliq"])  # "na" on the row
        b = Book("desk", self.root)
        del b.positions[addr(1)]["entry_qliq"]  # a position bought before BL-27
        b.save()
        self.assertIsNone(self.build()["books"]["desk"]["open"][0]["qliq"])

    def test_today_counts(self):
        now = datetime(2026, 10, 23, 12, 28, 13, tzinfo=timezone.utc)
        path = self.root / config.TASKS.format(day="2026-10-23")
        for r in [{"stage": "TICK", "action": "HEARTBEAT"}, {"stage": "TICK", "action": "HEARTBEAT", "reason": "error:X"},
                  {"stage": "EXIT", "action": "ERROR", "book": "baseline"}]:
            _append(path, TASK_FIELDS, {"ts": ts(now), "book": "desk", **r})
        t = live.build(self.root, now, N, 0)["today"]
        self.assertEqual(t["day"], "2026-10-23")
        self.assertEqual(t["ticks"], 3)  # 2 TICK rows so far + this tick's, written after live.json
        self.assertEqual((t["expected"], t["errors"]), (150, 1))

    def test_empty_books_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            got = live.run(Path(d), T0, N, 0)
            self.assertEqual(got["books"]["desk"], {"cash_usd": 50.0, "value_usd": 50.0, "open": []})
            self.assertEqual(json.loads((Path(d) / live.LIVE).read_text()), got)

    def test_no_adapter_import(self):
        src = Path(live.__file__).read_text()
        self.assertNotIn("adapters", src)
        self.assertNotIn("Client", src)


class TickLiveTest(unittest.TestCase):
    """The live step inside core/tick.py with the BL-7 fake adapter."""

    setUp, tearDown, run_tick = test_tick.TickTest.setUp, test_tick.TickTest.tearDown, test_tick.TickTest.run_tick

    def live(self):
        return json.loads((self.root / live.LIVE).read_text())

    def test_written_every_tick_with_the_tick_row_counts(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = traded(1)
        self.run_tick(0)
        hb = detail([r for r in tick_rows(self.root) if r["stage"] == "TICK"][-1])
        d = self.live()
        self.assertEqual(hb["live"], "ok")
        for k in live.LAST_TICK:
            if k != "exits":
                self.assertEqual(str(d["last_tick"][k]), hb[k], k)
        self.assertEqual(d["last_tick"]["exits"], 0)
        self.assertEqual(d["today"]["ticks"], 1)  # this tick's TICK row counts
        self.assertEqual([p["address"] for p in d["books"]["desk"]["open"]], [addr(1)])
        self.assertEqual(d["books"]["desk"]["open"][0]["qliq"], 12345)
        self.quotes[addr(1)] = {"price": good(1)["price"] * 0.5, "liq_usd": good(1)["liq_usd"], "source": "pool"}
        self.run_tick(5)
        d = self.live()
        self.assertEqual((d["as_of"], d["last_tick"]["exits"], d["books"]["desk"]["open"]),
                         ("2026-10-23T12:05:00Z", 1, []))  # hard stop: one desk EXIT SELL row
        self.assertEqual(d["today"]["ticks"], 2)

    def test_error_keeps_the_desk_and_is_retried(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        with mock.patch.object(live, "build", side_effect=RuntimeError("live broke")):
            with self.assertRaises(RuntimeError):  # red, but only after the trade and the TICK row
                self.run_tick(0)
        rows = tick_rows(self.root)
        self.assertEqual([(r["stage"], r["action"]) for r in rows],
                         [("BOOK", "REFILL"), ("VERIFY", "PASS"), ("EXECUTE", "BUY"), ("TICK", "HEARTBEAT")])  # BL-31: the twin's one-off restart row first
        self.assertEqual((detail(rows[-1])["live"], rows[-1]["reason"]), ("error:RuntimeError", ""))
        self.assertIn(addr(1), Book("desk", self.root).positions)
        self.assertFalse((self.root / live.LIVE).exists())
        self.run_tick(5)
        self.assertEqual(detail(tick_rows(self.root)[-1])["live"], "ok")
        self.assertEqual(self.live()["today"]["ticks"], 2)

    def test_crashed_tick_writes_no_live_json(self):
        self.found = [addr(1)]
        self.run_tick(0)
        before = (self.root / live.LIVE).read_text()
        (self.root / config.STATE).write_text("{not json")
        with self.assertRaises(ValueError):
            self.run_tick(5)
        hb = tick_rows(self.root)[-1]
        self.assertTrue(hb["reason"].startswith("error:"))
        self.assertEqual(detail(hb)["live"], "none")
        self.assertEqual((self.root / live.LIVE).read_text(), before)  # the old file stays: stale says it

    def test_no_request(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        n_on = self.run_tick(0)
        with tempfile.TemporaryDirectory() as other:
            self.root = Path(other)
            with mock.patch.object(live, "run"):
                n_off = self.run_tick(0)
        self.assertEqual(n_on["requests"], n_off["requests"])


class DeskUnchangedTest(unittest.TestCase):
    """The same ticks with live.json on or broken, and with or without entry_qliq: decisions and trades equal."""

    T = test_baseline.DeskUnchangedTest
    run_tick, ticks, assert_desk_same = T.run_tick, T.ticks, T.assert_desk_same
    keys = tick.COUNTS + ["goplus"] + tick.BL_COUNTS

    def test_live_on_and_broken(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on = self.ticks(Path(a), True)
            broken = self.ticks(Path(b), True, [mock.patch.object(live, "build", side_effect=RuntimeError("x"))])
            self.assertEqual(trades(Path(a)), trades(Path(b)))
        self.assertEqual([h["live"] for h in on[0]], ["ok"] * 3)
        self.assertEqual([h["live"] for h in broken[0]], ["error:RuntimeError"] * 3)
        self.assertEqual([type(e).__name__ for e in broken[3]], ["RuntimeError"] * 3)
        self.assert_desk_same(on, broken, self.keys)
        bl = lambda rows: [r for r in rows if r["book"] == baseline.NAME]
        self.assertTrue(bl(on[1]))
        self.assertEqual(bl(on[1]), bl(broken[1]))
        self.assertEqual(on[2][baseline.NAME], broken[2][baseline.NAME])

    def test_entry_qliq_changes_no_decision(self):
        def run(root, patches):
            self.root, self.found, self.facts, self.quotes, self.order = root, [], {}, {}, []
            for p in patches:
                p.start()
            try:
                b = Book(baseline.NAME, root)
                b.s["ticks_since_buy"] = baseline.CADENCE - 1
                b.save()
                self.found = [addr(i) for i in range(1, 8)]
                self.facts.update({addr(i): traded(i, qliq=100 * i) for i in range(1, 4)})
                self.run_tick(0)
                st = json.loads((root / config.STATE).read_text())
                bought = sorted((p.get("entry_qliq") for n in report.BOOKS for p in st[n]["positions"].values()), key=str)
                for i in range(1, 8):  # up, then down: ladder and hard stop for both books
                    self.quotes[addr(i)] = {"price": good(i)["price"] * 1.5, "liq_usd": good(i)["liq_usd"], "source": "pool"}
                self.run_tick(5)
                for i in range(1, 8):
                    self.quotes[addr(i)] = {"price": good(i)["price"] * 0.5, "liq_usd": good(i)["liq_usd"], "source": "pool"}
                self.run_tick(10)
            finally:
                for p in patches:
                    p.stop()
            state = json.loads((root / config.STATE).read_text())
            for name in report.BOOKS:
                for p in state[name]["positions"].values():
                    p.pop("entry_qliq", None)
            return tick_rows(root), trades(root), {k: state[k] for k in report.BOOKS + ["leads"]}, bought

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with_q = run(Path(a), [])
            without = run(Path(b), [mock.patch.object(desk, "entry_qliq", return_value=None)])
        self.assertEqual(with_q[3], [100, 200, 200, 300])  # desk bought 1-3, the baseline one of them: qliq stored
        self.assertEqual(without[3], [None] * 4)
        verdicts = lambda rows: [(r["book"], r["stage"], r["address"], r["action"], r["reason"])
                                 for r in rows if r["stage"] != "TICK"]
        self.assertEqual(verdicts(with_q[0]), verdicts(without[0]))
        self.assertTrue([t for t in with_q[1] if t["side"] == "SELL"])
        self.assertEqual(with_q[1], without[1])
        self.assertEqual(with_q[2], without[2])
        self.assertEqual([r["detail"] for r in with_q[0]], [r["detail"] for r in without[0]])


if __name__ == "__main__":
    unittest.main()
