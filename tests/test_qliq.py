"""Offline tests for BL-28: qliq, the USD value of the chosen pair's other side (ETH, WETH, USDC...). Logging only."""
import csv
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from adapters import base
from core import labels, leads as leads_mod, verify
from core.book import ts
from tests import test_trade
from tests.test_adapter_base import FRESH, OLDER, FakeClient, load
from tests.test_labels import Batch, pair as label_pair
from tests.test_tick import T0, addr
from tests.test_verify import GOOD

OLD_KEYS = ["b1", "s1", "b24", "s24", "liq", "dex"]  # BL-25, before qliq


class FormulaTest(unittest.TestCase):
    def test_fresh_v4_fixture(self):
        p = load("dexscreener_fresh")[0]  # asci / ETH, uniswap v4
        self.assertEqual(p["quoteToken"]["symbol"], "ETH")  # the quote side is the non-token side
        t = base.trade_facts([p], p, FRESH)
        self.assertEqual((t["liq"], t["qliq"]), (9494, 4648))  # 1.9405 ETH x (0.0002443 / 0.000000102)

    def test_brett_weth_fixture(self):
        p = load("dexscreener_older")[0]  # BRETT / WETH, aerodrome
        self.assertEqual(p["quoteToken"]["symbol"], "WETH")
        q = base.quote_liq(p, OLDER)
        self.assertEqual(q, round(143.7995 * 0.004927 / 0.000002058))
        self.assertEqual(q, 344_266)  # ~344k

    def test_token_as_quote_uses_the_base_side(self):
        p = load("dexscreener_fresh")[0]
        p["baseToken"], p["quoteToken"] = {"address": base.WETH, "symbol": "WETH"}, {"address": FRESH, "symbol": "X"}
        p["priceUsd"], p["priceNative"] = "2400", "9800000"  # base = WETH at 2,400 USD
        p["liquidity"] = {"usd": 9_600, "base": 2.0, "quote": 19_600_000}
        self.assertEqual(base.quote_liq(p, FRESH), 4_800)  # liquidity.base x priceUsd

    def test_na_never_zero(self):
        p = load("dexscreener_fresh")[0]
        for change in ({"liquidity": {"usd": 1}}, {"liquidity": {"usd": 1, "quote": 0, "base": 5}},
                       {"priceUsd": None}, {"priceNative": "0"}, {"priceUsd": "abc"}, {"liquidity": "x"},
                       {"baseToken": {"address": "0x" + "1" * 40}}):
            with self.subTest(change=change):
                self.assertEqual(base.quote_liq(dict(p, **change), FRESH), "na")
        for odd in (None, "x", 3, [], {"baseToken": "x"}):
            self.assertEqual(base.quote_liq(odd, FRESH), "na")
        self.assertEqual(base.trade_facts([], None, FRESH)["qliq"], "na")
        tiny = dict(p, liquidity={"usd": 90_000, "quote": 21.66}, priceUsd="0.00009118", priceNative="0.0153")
        self.assertEqual(base.quote_liq(tiny, FRESH), 0)  # a real side worth 0.13 USD: measured, so 0, not "na"

    def test_no_request_added(self):
        c = FakeClient({"token-pairs": load("dexscreener_fresh")})
        f = base.facts(FRESH, now=T0, client=c)
        self.assertEqual((f["trade"]["qliq"], len(c.calls)), (4648, 1))


class RowsTest(unittest.TestCase):
    def test_after_dex_on_rows_with_the_keys_only(self):
        with tempfile.TemporaryDirectory() as d:
            leads = leads_mod.Leads(Path(d))
            leads.record(addr(1), dict(GOOD, trade=dict(test_trade.TRADE)), T0)  # PASS
            leads.record(addr(2), dict(GOOD, liq_usd=1, goplus_skipped=True, trade=dict(test_trade.TRADE)), T0)
            rows = test_trade.tick_rows(Path(d))
        self.assertTrue(rows[0]["detail"].endswith("dex=uniswap:v4 qliq=4648"))
        self.assertNotIn("qliq", rows[1]["detail"])  # gate kill unchanged


class OutcomesHeaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def label(self, minutes, sights=()):
        p = label_pair(addr(1))
        p["liquidity"].update(base=40_000_000, quote=16.0)
        p["priceNative"] = "0.0008"
        with mock.patch.object(base, "token_batch", Batch({addr(1): [p], addr(2): [p]})):
            labels.run(self.root, T0 + timedelta(minutes=minutes), list(sights), FakeClient({}))

    def read(self, d):
        with (self.root / f"data/daily/{d}/outcomes.csv").open() as fh:
            return list(csv.reader(fh))

    def test_old_header_file_keeps_its_columns_new_day_gets_qliq(self):
        old = labels.FIELDS[:-1]
        path = self.root / "data/daily/2026-10-23/outcomes.csv"
        path.parent.mkdir(parents=True)
        path.write_text(",".join(old) + "\n")  # written before BL-28 today
        self.label(0, [labels.sight(addr(1), {"symbol": "A", "price": 2.0}, ts(T0), T0)])
        self.label(60)  # +1 h, same UTC day: old columns
        rows = self.read("2026-10-23")
        self.assertEqual(rows[0], old)
        self.assertEqual({len(r) for r in rows}, {len(old)})
        self.label(12 * 60)  # +6 h, 2026-10-24: a new file with qliq last
        new = self.read("2026-10-24")
        self.assertEqual(new[0], labels.FIELDS)
        self.assertEqual(new[0][-1], "qliq")
        self.assertEqual(dict(zip(new[0], new[1]))["qliq"], str(round(16.0 * 2.0 / 0.0008)))  # 40,000 USD
        self.assertEqual({len(r) for r in new}, {len(labels.FIELDS)})

    def test_new_file_has_qliq_and_gone_is_na(self):
        self.label(0, [labels.sight(addr(3), {"symbol": "C", "price": 1.0}, ts(T0), T0)])
        self.label(60)
        r = dict(zip(*self.read("2026-10-23")))
        self.assertEqual((r["gone"], r["qliq"]), ("1", "na"))


class DeskUnchangedTest(unittest.TestCase):
    """The BL-25 three-tick run with qliq and without it (the 6 BL-25 keys only): same rows once qliq is stripped."""

    def runs(self, with_qliq):
        patches = [] if with_qliq else [mock.patch.object(verify, "TRADE_KEYS", OLD_KEYS)]
        for p in patches:
            p.start()
        try:
            return test_trade.DeskUnchangedWithTradeKeysTest().runs(True)
        finally:
            for p in patches:
                p.stop()

    def test_only_qliq_differs(self):
        (hb_on, rows_on, st_on, err_on), (hb_off, rows_off, st_off, err_off) = self.runs(True), self.runs(False)
        self.assertEqual((err_on, err_off), ([], []))
        tagged = [r for r in rows_on if " qliq=" in r["detail"]]
        self.assertTrue(any(r["stage"] == "VERIFY" for r in tagged))
        self.assertTrue(any(r["book"] == "baseline" and r["action"] == "BUY" for r in tagged))
        self.assertTrue(all(r["detail"].split()[-1].startswith("qliq=") for r in tagged))  # always last, after dex
        self.assertFalse([r for r in tagged if "goplus=skipped" in r["detail"]])
        strip = lambda d: " ".join(x for x in d.split() if not x.startswith("qliq="))
        self.assertEqual([dict(r, detail=strip(r["detail"])) for r in rows_on], rows_off)
        self.assertEqual(st_on, st_off)  # desk, baseline, leads, report and labels state
        drop = {"secs"}
        self.assertEqual([{k: v for k, v in h.items() if k not in drop} for h in hb_on],
                         [{k: v for k, v in h.items() if k not in drop} for h in hb_off])


if __name__ == "__main__":
    unittest.main()
