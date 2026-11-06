import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config, desk, friction
from core.book import Book

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def facts(liq, price=1.0, launchpad="uniswap", symbol="TEST", qliq=None):
    """BL-32: qliq (real depth) defaults to liq in these desk tests."""
    return {"symbol": symbol, "price": price, "liq_usd": liq, "launchpad": launchpad,
            "qliq": liq if qliq is None else qliq}


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def round_trip(self, qliq, liq=1_000_000):
        self.n = getattr(self, "n", 0) + 1
        book = Book(f"rt{self.n}", root=self.root)  # a fresh book per round trip (BL-31 cooldown after the sell)
        pos = desk.buy(book, "0xa", facts(liq, qliq=qliq), NOW)
        cost = pos["cost_usd"]
        proceeds = desk.sell(book, "0xa", 1.0, None, "test", liq, NOW)  # no exit qliq: the entry qliq
        return proceeds / cost - 1

    @staticmethod
    def by_hand(qliq, fee=0.0):
        """BL-32: buy 5 USD at base + fee + 100 x 5 / qliq; sell qty x 1.0 at base + fee + 100 x that / qliq."""
        pin = 3.0 + fee + 100 * 5 / qliq
        qty = 5 / (1 + pin / 100)
        pout = 3.0 + fee + 100 * qty / qliq
        return qty * (1 - pout / 100) / 5 - 1

    def test_round_trip_at_the_floor_is_base_plus_impact(self):
        self.assertAlmostEqual(self.round_trip(10_000), self.by_hand(10_000), places=12)
        self.assertAlmostEqual(self.round_trip(10_000), -0.0591, delta=0.0001)  # 3.05% in, ~3.05% out

    def test_round_trip_deep_pool_is_about_base_only(self):
        self.assertAlmostEqual(self.round_trip(10_000_000), -0.0583, delta=0.0001)  # 3% each side, no 5% step
        self.assertAlmostEqual(self.round_trip(60_000), self.by_hand(60_000), places=12)  # was -9.5% (thin step)

    def test_total_liquidity_never_prices_a_fill(self):
        """BL-32: two buys with the same qliq and very different liq_usd fill at the same price."""
        a = desk.buy(self.book, "0xa", facts(60_000, qliq=20_000, symbol="A"), NOW)
        b = desk.buy(self.book, "0xb", facts(5_000_000, qliq=20_000, symbol="B"), NOW)
        self.assertEqual(a["entry_fill"], b["entry_fill"])

    def test_clanker_adds_pool_fee(self):
        self.assertAlmostEqual(friction.side_pct(5, 1_000_000_000, "clanker"), 4.0, places=6)
        self.assertAlmostEqual(friction.side_pct(5, 10_000, "clanker"), 4.05, places=12)

    def test_size(self):
        # BL-31: a fixed TICKET_USD (5) ticket, whatever the cash; still capped by the pool (BL-32: qliq x 0.01)
        self.assertEqual(desk.size(50.0, 150_000), (5.0, ""))
        self.assertEqual(desk.size(30.0, 150_000), (5.0, ""))
        self.assertEqual(desk.size(5000.0, 1_000_000), (5.0, ""))   # no longer 10% of cash (SIZE_PCT not read)
        self.assertEqual(desk.size(5.0, 60_000), (5.0, ""))         # exactly one ticket of cash
        self.assertEqual(desk.size(4.99, 150_000), (0.0, "size"))   # cash < ticket
        self.assertEqual(desk.size(50.0, 400), (0.0, "size"))       # pool cap 4 < MIN_TICKET
        self.assertEqual(desk.size(50.0, 499), (0.0, "size"))       # pool cap 4.99
        self.assertEqual(desk.size(50.0, 500), (5.0, ""))           # pool cap 5.00
        self.assertEqual(desk.size(50.0, None), (0.0, "size"))      # BL-32: no qliq, no ticket

    def test_max_open_and_skip_logged(self):
        for i in range(config.MAX_OPEN):
            self.assertIsNotNone(desk.buy(self.book, f"0x{i}", facts(150_000, symbol=f"T{i}"), NOW))
        self.assertIsNone(desk.buy(self.book, "0xz", facts(150_000, symbol="TZ"), NOW))
        with (self.root / "data/daily/2026-10-23/tasks.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[-1]["action"], "SKIP")
        self.assertEqual(rows[-1]["reason"], "max_open")

    def test_loss_halt_until_next_utc_day(self):
        desk.buy(self.book, "0xa", facts(150_000), NOW)            # sets day_start_value = 50
        self.book.s["cash"] = 30.0                                   # value ~35 = -30%
        self.assertIsNone(desk.buy(self.book, "0xb", facts(150_000, symbol="B"), NOW))
        self.book.s["cash"] = 45.0                                   # recovery does not lift it today
        self.assertIsNone(desk.buy(self.book, "0xb", facts(150_000, symbol="B"), NOW))
        tomorrow = NOW + timedelta(days=1)
        self.assertIsNotNone(desk.buy(self.book, "0xb", facts(150_000, symbol="B"), tomorrow))
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
