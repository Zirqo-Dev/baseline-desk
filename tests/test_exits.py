import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import desk, exits
from core.book import Book

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
LIQ = 150_000
E = 1.03  # filled entry price for a 1.00 quote at 150k liquidity (3% friction)


class ExitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)
        desk.buy(self.book, "0xa", {"symbol": "TEST", "price": 1.0, "liq_usd": LIQ,
                                    "launchpad": "uniswap"}, T0)
        self.orig = self.book.positions["0xa"]["orig_qty"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_at(self, price, liq=LIQ, minutes=5):
        exits.check(self.book, lambda a: {"price": price, "liq_usd": liq}, T0 + timedelta(minutes=minutes))

    def rows(self, name):
        with (self.root / f"data/daily/2026-10-23/{name}.csv").open() as fh:
            return list(csv.DictReader(fh))

    def last_exit_task(self):
        return [r for r in self.rows("tasks") if r["stage"] == "EXIT"][-1]

    def pos(self):
        return self.book.positions.get("0xa")

    # ---- the six rules ----
    def test_liq_pull_closes_all(self):
        self.run_at(E, liq=LIQ * 0.6)
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "liq_pull")

    def test_hard_stop_closes_all(self):
        self.run_at(E * 0.74)
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "hard_stop")

    def test_ladder_1_sells_a_third(self):
        self.run_at(E * 1.41)
        self.assertAlmostEqual(self.pos()["qty"], self.orig * 0.67)
        self.assertEqual(self.pos()["ladder_done"], [0])

    def test_ladder_2(self):
        self.run_at(E * 1.41)
        self.run_at(E * 2.01)
        self.assertEqual(self.rows("trades")[-1]["reason"], "ladder_2")

    def test_trail_closes_remainder(self):
        self.run_at(E * 1.41)                     # rung 1, peak = 1.41E
        self.run_at(E * 1.41 * 0.74)              # 26% under the peak
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "trail")

    def test_time_stop_closes_all(self):
        self.run_at(E * 1.05, minutes=44)
        self.assertIsNotNone(self.pos())
        self.run_at(E * 1.05, minutes=45)
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "time_stop")

    # ---- The owner's three details ----
    def test_rungs_sell_33pct_of_original_not_remainder(self):
        self.run_at(E * 1.41)
        self.run_at(E * 2.01)
        self.assertAlmostEqual(self.pos()["qty"], self.orig * 0.34)

    def test_time_stop_off_after_first_rung(self):
        self.run_at(E * 1.41, minutes=5)
        self.run_at(E * 1.08, minutes=60)         # under +10%, over the trail stop (1.0575E)
        self.assertAlmostEqual(self.pos()["qty"], self.orig * 0.67)
        self.assertEqual(self.last_exit_task()["action"], "HOLD")

    def test_liq_pull_and_hard_stop_close_whole_after_a_rung(self):
        self.run_at(E * 1.41)
        self.run_at(E * 1.20, liq=LIQ * 0.5)
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "liq_pull")
        self.assertAlmostEqual(float(self.rows("trades")[-1]["qty"]), self.orig * 0.67)

    def test_hard_stop_after_a_rung_closes_remainder(self):
        self.run_at(E * 1.41)
        self.run_at(E * 0.70)                     # gap straight through trail and stop
        self.assertIsNone(self.pos())
        self.assertEqual(self.rows("trades")[-1]["reason"], "hard_stop")

    # ---- logging ----
    def test_stop_slippage_logged(self):
        self.run_at(E * 0.50)                     # gaps far below the -25% stop
        t = self.rows("trades")[-1]
        self.assertAlmostEqual(float(t["intended_price"]), E * 0.75)
        self.assertAlmostEqual(float(t["fill_price"]), E * 0.50 * 0.97)

    def test_no_quote_holds(self):
        exits.check(self.book, lambda a: None, T0 + timedelta(minutes=5))
        exits.check(self.book, lambda a: {"price": 1.0, "liq_usd": None}, T0 + timedelta(minutes=10))
        self.assertIsNotNone(self.pos())
        self.assertEqual(self.last_exit_task()["reason"], "no_quote")

    def test_every_check_is_a_task_row(self):
        self.run_at(E * 1.01)
        self.run_at(E * 1.41)
        self.assertEqual([r["action"] for r in self.rows("tasks") if r["stage"] == "EXIT"], ["HOLD", "SELL"])


if __name__ == "__main__":
    unittest.main()
