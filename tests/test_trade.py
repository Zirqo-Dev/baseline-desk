"""Offline tests for BL-25: DexScreener trade keys on VERIFY and baseline BUY rows. Logging only. No network."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters import base
from core import baseline, config, leads as leads_mod, verify
from core import desk as desk_mod
from core.book import Book
from core.leads import Leads
from tests import test_baseline
from tests.test_adapter_base import FRESH, FakeClient, fresh_pairs, load
from tests.test_tick import T0, addr, detail, good, tick_rows
from tests.test_verify import GOOD

OTHER = "0x4200000000000000000000000000000000000006"  # WETH
TRADE = {"b1": 5, "s1": 3, "b24": 40, "s24": 30, "liq": 80000, "dex": "uniswap:v4"}
KEYS = "b1=5 s1=3 b24=40 s24=30 liq=80000 dex=uniswap:v4"


def pair(dex_id="uniswap", labels=("v4",), base_tok=FRESH, quote_tok=OTHER, h1=(10, 4), h24=(100, 40), liq=60_000.4,
         chain="base"):
    """A DexScreener pair; h1/h24 = (buys, sells) as DexScreener reports them for baseToken."""
    return {"chainId": chain, "dexId": dex_id, "labels": list(labels), "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": base_tok, "symbol": "X"}, "quoteToken": {"address": quote_tok, "symbol": "Y"},
            "txns": {"h1": {"buys": h1[0], "sells": h1[1]}, "h24": {"buys": h24[0], "sells": h24[1]}},
            "liquidity": {"usd": liq}}


def strip_trade(row_detail):
    """The detail without the BL-25 keys; asserts they are the last six tokens, in order."""
    parts = row_detail.split()
    if parts[-6:] and [p.split("=")[0] for p in parts[-6:]] == verify.TRADE_KEYS:
        return " ".join(parts[:-6])
    assert not any(p.split("=")[0] in verify.TRADE_KEYS for p in parts), row_detail
    return row_detail


class TradeFactsTest(unittest.TestCase):
    def test_sum_across_pairs_and_the_quote_side_mirror(self):
        pairs = [pair(h1=(10, 4), h24=(100, 40)),  # our token is baseToken: b = buys, s = sells
                 pair(base_tok=OTHER, quote_tok=FRESH, h1=(7, 2), h24=(70, 20)),  # quoteToken: b = sells, s = buys
                 pair(chain="ethereum", h1=(999, 999)),  # not Base: ignored
                 pair(base_tok=OTHER, quote_tok="0x" + "11" * 20, h1=(999, 999))]  # not our token: ignored
        t = base.trade_facts(pairs, pairs[0], FRESH)
        self.assertEqual(t, {"b1": 10 + 2, "s1": 4 + 7, "b24": 100 + 20, "s24": 40 + 70, "liq": 60000,
                             "dex": "uniswap:v4"})
        self.assertEqual(t["s1"], base.total_sells_1h(pairs, FRESH))  # s1 == sells_1h, quote side included

    def test_s1_equals_sells_1h_in_the_real_facts(self):
        p = pair(base_tok=OTHER, quote_tok=FRESH, h1=(9, 1), h24=(50, 5))
        p["pairAddress"] = "0x" + "cd" * 20
        p["liquidity"]["usd"] = 1_000
        pairs = fresh_pairs() + [p]
        d, c = {}, FakeClient({"token-pairs": pairs, "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, now=T0, client=c, dex=d)
        self.assertEqual(f["trade"]["s1"], f["sells_1h"])
        self.assertEqual(f["trade"], {"b1": 136 + 1, "s1": 92 + 9, "b24": 136 + 5, "s24": 92 + 50, "liq": 60000,
                                      "dex": "uniswap:v4"})
        self.assertEqual(d["trade"], f["trade"])
        self.assertEqual(len(c.calls), 2)  # DexScreener + GoPlus, no request added

    def test_gate_killed_facts_carry_it_but_rows_do_not(self):
        f = base.facts(FRESH, now=T0, client=FakeClient({"token-pairs": load("dexscreener_fresh")}))
        self.assertTrue(f["goplus_skipped"])
        self.assertEqual(f["trade"]["liq"], 9494)
        self.assertEqual(verify.trade_detail(f), [])

    def test_na_never_zero(self):
        self.assertEqual(base.trade_facts([], None, FRESH), dict.fromkeys(verify.TRADE_KEYS, "na"))
        p = pair()
        del p["txns"]["h1"]["sells"]  # one field missing
        p["txns"]["h24"] = None       # odd window
        del p["liquidity"]
        del p["dexId"]
        self.assertEqual(base.trade_facts([p], p, FRESH),
                         {"b1": 10, "s1": "na", "b24": "na", "s24": "na", "liq": "na", "dex": "na"})
        q = pair(h1=(0, 0))
        self.assertEqual(base.trade_facts([q], q, FRESH)["s1"], 0)  # a real 0 stays 0

    def test_odd_shapes_never_raise(self):
        odd = [None, "x", 3, {"chainId": "base", "baseToken": "x"}, {"chainId": "base", "baseToken": {"address": FRESH},
               "txns": ["h1"]}, {"chainId": "base", "baseToken": {"address": FRESH}, "txns": {"h1": {"buys": "abc"}}}]
        t = base.trade_facts(odd, {"liquidity": "x", "dexId": 5, "labels": "v4"}, FRESH)
        self.assertEqual(t, dict.fromkeys(verify.TRADE_KEYS, "na"))
        self.assertEqual(base.trade_facts("garbage", [1], FRESH), dict.fromkeys(verify.TRADE_KEYS, "na"))
        inf = pair(h1=("inf", 1))  # int(inf) raises inside: all "na", no exception
        self.assertEqual(base.trade_facts([inf], inf, FRESH), dict.fromkeys(verify.TRADE_KEYS, "na"))
        self.assertEqual(verify.trade_detail({"trade": "odd"}), [f"{k}=na" for k in verify.TRADE_KEYS])
        self.assertEqual(verify.trade_detail(None), [])

    def test_dex_label(self):
        cases = [(("uniswap", ["v2"]), "uniswap:v2"), (("uniswap", ["v3"]), "uniswap:v3"),
                 (("uniswap", ["v4"]), "uniswap:v4"), (("aerodrome", []), "aerodrome"),
                 (("aerodrome", ["v2"]), "aerodrome:v2"),
                 (("uniswap", ["v4"], base.ZORA), "uniswap:v4"),  # a Zora coin: the pool's dex, not the launchpad
                 (("zora", ["v4", "DLMM"]), "zora:v4"), (("baseswap", ["CL"]), "baseswap"),
                 (("odd dex", None), "odd_dex")]
        for args, want in cases:
            dex_id, labels = args[0], args[1]
            p = pair(dex_id=dex_id, labels=labels or (), quote_tok=args[2] if len(args) > 2 else OTHER)
            if labels is None:
                p["labels"] = None
            self.assertEqual(base.trade_facts([p], p, FRESH)["dex"], want, args)


class RowsTest(unittest.TestCase):
    """Which rows get the keys: every desk VERIFY row past dex_skip() and the baseline BUY row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.leads = Leads(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self):
        return tick_rows(self.root)[-1]

    def test_rows_past_the_gate_end_with_the_keys(self):
        wait = {k: v for k, v in GOOD.items() if k != "honeypot"}
        cases = [
            (addr(1), dict(wait, goplus_pending=["honeypot"]), "WATCH"),
            (addr(2), dict(GOOD), "PASS"),
            (addr(3), dict(GOOD, honeypot=True), "SKIP"),
            (addr(4), dict(GOOD, goplus_partial=True), "SKIP"),
            (addr(5), dict(GOOD, fetch_errors=["goplus"], goplus_code=4029), "RETRY"),  # GoPlus side, past the gate
        ]
        for a, f, action in cases:
            self.leads.record(a, dict(f, trade=TRADE), T0)
            row = self.last()
            self.assertEqual(row["action"], action)
            self.assertTrue(row["detail"].endswith(KEYS), row)
        # the last of three GoPlus-side fetch errors: fetch_error_x3 keeps them too, after until=
        for i in (1, 2, 3):
            self.leads.record(addr(6), dict(GOOD, fetch_errors=["goplus"], trade=TRADE), T0)
        row = self.last()
        self.assertEqual((row["action"], row["reason"]), ("SKIP", "fetch_error_x3"))
        self.assertTrue(row["detail"].endswith(f"until=2026-10-24T12:00:00Z {KEYS}"))

    def test_gate_killed_rows_unchanged(self):
        cases = [(addr(1), {"goplus_skipped": True, "symbol": "X", "price": 1, "liq_usd": 10, "age_min": 30,
                            "sells_1h": 3, "fetch_errors": []}, "liquidity"),
                 (addr(2), {"goplus_skipped": True, "fetch_errors": []}, "missing:symbol"),
                 (addr(3), {"goplus_skipped": True, "fetch_errors": ["ds_pairs"]}, "fetch_error")]
        for a, f, reason in cases:
            self.leads.record(a, dict(f, trade=TRADE), T0)
            plain = tick_rows(self.root)[-1]["detail"]
            self.assertEqual(self.last()["reason"], reason)
            self.assertNotIn("b1=", plain)
            self.assertIn("goplus=skipped", plain.split())

    def test_baseline_buy_row_only(self):
        bl = Book(baseline.NAME, self.root)
        seen = {addr(1): dict(good(1), trade=TRADE)}
        baseline.buy(bl, seen, {}, set(), T0)
        row = self.last()
        self.assertEqual(row["action"], "BUY")
        self.assertTrue(row["detail"].endswith(KEYS))
        self.assertEqual(detail(row)["pool"], addr(1)[2:8])  # BL-8's pool= is still the eligible-pool prefixes
        # a blocked buy (max_open) and nothing eligible: SKIP rows without the keys
        for i in range(2, config.MAX_OPEN + 1):
            desk_mod.buy(bl, addr(i), good(i), T0)
        baseline.buy(bl, {addr(9): dict(good(9), trade=TRADE)}, {}, set(), T0)
        self.assertEqual((self.last()["action"], self.last()["reason"]), ("SKIP", "max_open"))
        self.assertNotIn("b1=", self.last()["detail"])
        baseline.buy(bl, {}, {}, set(), T0)
        self.assertEqual(self.last()["detail"], "eligible=0")


def good_with_trade(i):
    return dict(good(i), trade=dict(TRADE, b1=i))


class DeskUnchangedWithTradeKeysTest(unittest.TestCase):
    """BL-25 changes nothing but the trailing keys: same ticks with the keys on and off."""

    def runs(self, on):
        t = test_baseline.DeskUnchangedTest()  # module attribute: not collected twice
        patches = [mock.patch.object(test_baseline, "good", good_with_trade)]
        if not on:
            patches += [mock.patch.object(leads_mod, "trade_detail", lambda f: []),
                        mock.patch.object(baseline, "trade_detail", lambda f: [])]
        with tempfile.TemporaryDirectory() as d:
            for p in patches:
                p.start()
            try:
                return t.ticks(Path(d), True)
            finally:
                for p in patches:
                    p.stop()

    def test_only_the_trailing_keys_differ(self):
        (hb_on, rows_on, st_on, err_on), (hb_off, rows_off, st_off, err_off) = self.runs(True), self.runs(False)
        self.assertEqual((err_on, err_off), ([], []))
        with_keys = [r for r in rows_on if verify.TRADE_KEYS[0] + "=" in r["detail"]]
        self.assertTrue(any(r["stage"] == "VERIFY" for r in with_keys))
        self.assertTrue(any(r["book"] == "baseline" and r["action"] == "BUY" for r in with_keys))
        self.assertTrue(any("goplus=skipped" in r["detail"] for r in rows_on))  # gate rows are in the run
        self.assertFalse([r for r in with_keys if "goplus=skipped" in r["detail"]])
        self.assertEqual(len(rows_on), len(rows_off))
        self.assertEqual([dict(r, detail=strip_trade(r["detail"])) for r in rows_on], rows_off)
        self.assertEqual(st_on, st_off)  # desk, baseline, leads and report state
        drop = {"secs"}
        self.assertEqual([{k: v for k, v in h.items() if k not in drop} for h in hb_on],
                         [{k: v for k, v in h.items() if k not in drop} for h in hb_off])  # requests included


if __name__ == "__main__":
    unittest.main()
