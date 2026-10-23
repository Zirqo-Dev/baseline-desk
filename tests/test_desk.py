import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config, desk, friction
from core.book import Book

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def facts(liq, price=1.0, launchpad="uniswap", symbol="TEST"):
    return {"symbol": symbol, "price": price, "liq_usd": liq, "launchpad": launchpad}


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def round_trip(self, liq):
        pos = desk.buy(self.book, "0xa", facts(liq), NOW)
        cost = pos["cost_usd"]
        proceeds = desk.sell(self.book, "0xa", 1.0, None, "test", liq, NOW)
        return proceeds / cost - 1

    def test_round_trip_150k_loses_5_8pct(self):
        self.assertAlmostEqual(self.round_trip(150_000), -0.058, delta=0.001)

    def test_round_trip_60k_loses_9_5pct(self):
        self.assertAlmostEqual(self.round_trip(60_000), -0.095, delta=0.001)

    def test_clanker_adds_pool_fee(self):
        self.assertEqual(friction.side_pct(150_000, "clanker"), 4.0)

    def test_size(self):
        self.assertEqual(desk.size(50.0, 150_000), (5.0, ""))       # 50*0.10 = 5
        self.assertEqual(desk.size(30.0, 150_000), (5.0, ""))       # clamped up to MIN_TICKET
        self.assertEqual(desk.size(5000.0, 1_000_000), (250, ""))   # clamped to MAX_TICKET
        self.assertEqual(desk.size(5000.0, 60_000), (250, ""))      # pool cap 600
        self.assertEqual(desk.size(5000.0, 20_000), (200, ""))      # pool cap 200
        self.assertEqual(desk.size(4.99, 150_000), (0.0, "size"))   # cash < MIN_TICKET
        self.assertEqual(desk.size(50.0, 400), (0.0, "size"))       # pool cap 4 < MIN_TICKET

    def test_max_open_and_skip_logged(self):
        for i in range(config.MAX_OPEN):
            self.assertIsNotNone(desk.buy(self.book, f"0x{i}", facts(150_000), NOW))
        self.assertIsNone(desk.buy(self.book, "0xz", facts(150_000), NOW))
        with (self.root / "data/daily/2026-10-23/tasks.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[-1]["action"], "SKIP")
        self.assertEqual(rows[-1]["reason"], "max_open")

    def test_loss_halt_until_next_utc_day(self):
        desk.buy(self.book, "0xa", facts(150_000), NOW)            # sets day_start_value = 50
        self.book.s["cash"] = 30.0                                   # value ~35 = -30%
        self.assertIsNone(desk.buy(self.book, "0xb", facts(150_000), NOW))
        self.book.s["cash"] = 45.0                                   # recovery does not lift it today
        self.assertIsNone(desk.buy(self.book, "0xb", facts(150_000), NOW))
        tomorrow = NOW + timedelta(days=1)
        self.assertIsNotNone(desk.buy(self.book, "0xb", facts(150_000), tomorrow))
        self.assertEqual(Book("desk", root=self.root).s["day"], "2026-10-24")

    def test_state_persists_per_book(self):
        desk.buy(self.book, "0xa", facts(150_000), NOW)
        other = Book("baseline", root=self.root)
        other.save()
        again = Book("desk", root=self.root)
        self.assertIn("0xa", again.positions)
        self.assertEqual(Book("baseline", root=self.root).cash, config.START_BALANCE)


if __name__ == "__main__":
    unittest.main()
