"""Offline tests for BL-26: outcome labels (+1 h / +6 h / +24 h) in outcomes.csv. No network."""
import csv
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from adapters import base
from core import baseline, config, labels, tick
from core.book import Book, ts
from tests import test_baseline
from tests.test_adapter_base import FakeClient
from tests import test_tick
from tests.test_tick import T0, addr, detail, good, tick_rows

OK = object()

WETH = base.WETH


def pair(a, price="2.0", liq=80_000, h1=(5, 3), h24=(40, 30), side="baseToken", chain="base", dex="uniswap",
         labels_=("v3",), native="0.001"):
    """One DexScreener pair with token `a` on `side` (buys, sells) as DexScreener counts them for the base token."""
    other = {"address": WETH, "symbol": "WETH"}
    me = {"address": a, "symbol": "TOK"}
    p = {"chainId": chain, "dexId": dex, "labels": list(labels_), "pairAddress": "0x" + a[-8:] * 5,
         "baseToken": me if side == "baseToken" else other, "quoteToken": other if side == "baseToken" else me,
         "priceUsd": price, "priceNative": native, "liquidity": {"usd": liq},
         "txns": {"h1": {"buys": h1[0], "sells": h1[1]}, "h24": {"buys": h24[0], "sells": h24[1]}}}
    return p


class Batch:
    """A fake token_batch(): answers from `pairs` (address -> list of pairs), or a fixed failure."""

    def __init__(self, pairs=None, fail=OK):
        self.pairs, self.fail, self.asked = pairs or {}, fail, []

    def __call__(self, addresses, client=None):
        self.asked.append(list(addresses))
        if client is not None:
            client.calls.append(("ds_pairs", "tokens/v1/base/" + ",".join(addresses)))
        if self.fail is not OK:
            return self.fail
        return [p for a in addresses for p in self.pairs.get(a, [])]


class Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sight(self, i, first_seen=T0, price=1.0):
        return labels.sight(addr(i), {"symbol": f"T{i}", "price": price}, ts(first_seen))

    def run_labels(self, minutes, sights=(), batch=None):
        c = FakeClient({})
        with mock.patch.object(base, "token_batch", batch or Batch()):
            n = labels.run(self.root, T0 + timedelta(minutes=minutes), list(sights), c)
        return n, c

    def rows(self):
        out = []
        for p in sorted((self.root / "data/daily").glob("*/outcomes.csv")):
            with p.open() as fh:
                out += list(csv.DictReader(fh))
        return out

    def pending(self):
        return labels.load(self.root)["pending"]


class DueTest(Root):
    def test_not_before_one_hour(self):
        b = Batch({addr(1): [pair(addr(1))]})
        n, _ = self.run_labels(0, [self.sight(1)], b)
        self.assertEqual(n, {"due": 0, "done": 0, "calls": 0})
        n, _ = self.run_labels(59, batch=b)
        self.assertEqual((n["due"], self.rows()), (0, []))
        n, _ = self.run_labels(60, batch=b)
        self.assertEqual(n, {"due": 1, "done": 1, "calls": 1})
        r = self.rows()[0]
        self.assertEqual((r["horizon"], r["age_min"], r["first_seen"], r["address"]), ("1h", "60", ts(T0), addr(1)))
        self.assertEqual(list(r), labels.FIELDS)

    def test_late_labels_keep_the_real_age_and_each_horizon(self):
        self.run_labels(0, [self.sight(1)])
        n, c = self.run_labels(7 * 60 + 3, batch=Batch({addr(1): [pair(addr(1))]}))
        self.assertEqual((n["due"], n["done"], n["calls"]), (2, 2, 1))  # +1 h late and +6 h in one request
        self.assertEqual([(r["horizon"], r["age_min"]) for r in self.rows()], [("1h", "423"), ("6h", "423")])
        self.assertEqual(self.pending()[addr(1)]["done"], [1, 6])

    def test_each_label_once_then_the_entry_leaves(self):
        b = Batch({addr(1): [pair(addr(1))]})
        self.run_labels(0, [self.sight(1)])
        for m in (60, 65, 6 * 60, 6 * 60 + 5, 24 * 60):
            self.run_labels(m, [self.sight(1, T0 + timedelta(minutes=m))], b)  # seen again: first sight kept
        self.assertEqual([r["horizon"] for r in self.rows()], ["1h", "6h", "24h"])
        self.assertEqual({r["first_seen"] for r in self.rows()}, {ts(T0)})
        self.assertEqual(self.pending(), {})  # gone after its +24 h label

    def test_first_price_and_change(self):
        self.run_labels(0, [self.sight(1, price=1.6)])
        self.run_labels(60, batch=Batch({addr(1): [pair(addr(1), price="2.0")]}))
        r = self.rows()[0]
        self.assertEqual((r["first_price"], r["price"], r["chg_pct"]), ("1.6", "2.0", "25.0"))


class BatchTest(Root):
    def test_two_calls_of_thirty_oldest_first_rest_wait(self):
        sights = [self.sight(i, T0 + timedelta(seconds=i)) for i in range(1, 71)]
        self.run_labels(0, sights[::-1])  # added in reverse: order must come from first_seen
        b = Batch({addr(i): [pair(addr(i))] for i in range(1, 71)})
        n, c = self.run_labels(62, batch=b)
        self.assertEqual(n, {"due": 70, "done": 60, "calls": 2})
        self.assertEqual(b.asked, [[addr(i) for i in range(1, 31)], [addr(i) for i in range(31, 61)]])
        self.assertEqual(len(c.calls), 2)
        n, _ = self.run_labels(67, batch=b)
        self.assertEqual(n, {"due": 10, "done": 10, "calls": 1})
        self.assertEqual(b.asked[-1], [addr(i) for i in range(61, 71)])

    def test_failed_request_stays_due(self):
        self.run_labels(0, [self.sight(1)])
        for fail in (base.FETCH_ERROR, None, {"pairs": []}):
            n, _ = self.run_labels(60, batch=Batch(fail=fail))
            self.assertEqual(n, {"due": 1, "done": 0, "calls": 1})
        self.assertEqual(self.rows(), [])
        n, _ = self.run_labels(65, batch=Batch({addr(1): [pair(addr(1))]}))
        self.assertEqual(n["done"], 1)

    def test_real_batch_url(self):
        c = FakeClient({"tokens/v1/base/": [pair(addr(1))]})
        self.assertEqual(len(base.token_batch([addr(1), addr(2)], c)), 1)
        self.assertEqual(c.calls, [("ds_pairs", f"{base.DS}/tokens/v1/base/{addr(1)},{addr(2)}")])


class RowTest(Root):
    def label(self, pairs, price=1.0):
        self.run_labels(0, [self.sight(1, price=price)])
        self.run_labels(60, batch=Batch({addr(1): pairs}))
        return self.rows()[0]

    def test_gone(self):
        for pairs in ([], [pair(addr(1), chain="ethereum")]):
            with self.subTest(pairs=len(pairs)):
                self.tearDown(); self.setUp()
                r = self.label(pairs)
                self.assertEqual(r["gone"], "1")
                self.assertEqual({r[k] for k in labels.FIELDS[7:-1]}, {"na"})
                self.assertEqual(r["first_price"], "1.0")

    def test_na_is_not_zero(self):
        p = pair(addr(1), h1=(0, 0))
        del p["txns"]["h24"], p["liquidity"]
        r = self.label([p], price=None)
        self.assertEqual((r["pair_b1"], r["pair_s1"]), ("0", "0"))  # a real 0 stays 0
        self.assertEqual((r["pair_b24"], r["pair_s24"], r["liq"]), ("na", "na", "na"))
        self.assertEqual((r["first_price"], r["chg_pct"], r["gone"]), ("na", "na", "0"))
        self.assertEqual(r["price"], "2.0")

    def test_one_pair_mirrored_like_bl25(self):
        p = pair(addr(1), side="quoteToken", price="3000", native="1500", h1=(7, 2), h24=(70, 20))
        r = self.label([p])
        t = base.trade_facts([p], p, addr(1))  # BL-25's helper on that same pair
        self.assertEqual((r["pair_b1"], r["pair_s1"], r["pair_b24"], r["pair_s24"]), ("2", "7", "20", "70"))
        self.assertEqual([r[k] for k in ("pair_b1", "pair_s1", "pair_b24", "pair_s24", "liq", "dex")],
                         [str(t[k]) for k in ("b1", "s1", "b24", "s24", "liq", "dex")])
        self.assertEqual(r["price"], "2.0")  # quote side priced like facts(): priceUsd / priceNative

    def test_price_from_pick_pair_and_dex_names_the_pool(self):
        deep, thin = pair(addr(1), price="2.0", liq=90_000), pair(addr(1), price="9.0", liq=10, dex="aerodrome")
        r = self.label([thin, deep])
        self.assertEqual((r["price"], r["liq"], r["dex"]), ("2.0", "90000", "uniswap:v3"))


class PruneTest(Root):
    def test_dropped_at_26_hours_without_its_24h_label(self):
        self.run_labels(0, [self.sight(1)])
        self.run_labels(60, batch=Batch({addr(1): [pair(addr(1))]}))
        self.run_labels(24 * 60, batch=Batch(fail=base.FETCH_ERROR))  # the +24 h request fails
        self.assertIn(addr(1), self.pending())
        n, _ = self.run_labels(26 * 60, batch=Batch({addr(1): [pair(addr(1))]}))
        self.assertEqual((n["due"], self.pending()), (0, {}))
        self.assertEqual([r["horizon"] for r in self.rows()], ["1h"])

    def test_state_keeps_only_what_is_needed_and_is_bounded(self):
        for m in range(0, 30 * 60, 60):  # 30 h, one new token an hour, every label answered
            self.run_labels(m, [self.sight(m, T0 + timedelta(minutes=m))],
                            Batch({addr(i): [pair(addr(i))] for i in range(0, 30 * 60, 60)}))
        p = self.pending()
        self.assertLessEqual(len(p), labels.KEEP_H + 1)
        self.assertEqual(set(next(iter(p.values()))), {"symbol", "first_seen", "first_price", "done"})
        self.assertEqual(set(json.loads((self.root / config.STATE).read_text())), {"labels"})


def watched(i):
    """Facts GoPlus has not filled yet: the lead goes on the watchlist (WATCH goplus_not_ready)."""
    f = dict(good(i), goplus_pending=["mint_open"])
    del f["mint_open"]
    return f


class TickLabelsTest(unittest.TestCase):
    """The labels step inside core/tick.py with the BL-7 fake adapter."""

    setUp, tearDown, run_tick = test_tick.TickTest.setUp, test_tick.TickTest.tearDown, test_tick.TickTest.run_tick

    def outcomes(self):
        p = self.root / "data/daily/2026-10-23/outcomes.csv"
        if not p.exists():
            return []
        with p.open() as fh:
            return list(csv.DictReader(fh))

    def test_past_gate_checks_only(self):
        self.found = [addr(i) for i in range(1, 5)]
        self.facts.update({addr(1): good(1), addr(2): dict(good(2), honeypot=True),
                           addr(3): dict(good(3), liq_usd=1), addr(4): watched(4)})
        with mock.patch.object(base, "token_batch", Batch()):
            self.run_tick(0)
        v = {r["address"]: (r["action"], r["reason"]) for r in tick_rows(self.root) if r["stage"] == "VERIFY"}
        self.assertEqual(v, {addr(1): ("PASS", ""), addr(2): ("SKIP", "honeypot"), addr(3): ("SKIP", "liquidity"),
                             addr(4): ("WATCH", "goplus_not_ready")})
        pend = labels.load(self.root)["pending"]
        self.assertEqual(set(pend), {addr(1), addr(2), addr(4)})  # PASS, safety SKIP, WATCH; not the gate kill
        self.assertEqual(pend[addr(1)]["first_price"], good(1)["price"])
        self.assertEqual(pend[addr(4)]["first_seen"], ts(T0))

    def test_watched_token_keeps_its_first_seen(self):
        self.found = [addr(4)]
        self.facts[addr(4)] = watched(4)
        with mock.patch.object(base, "token_batch", Batch()):
            self.run_tick(0)
            labels.save(self.root, {"pending": {}})  # as if the entry had left: the next check rebuilds it
            self.run_tick(15)
        p = labels.load(self.root)["pending"][addr(4)]
        self.assertEqual((p["first_seen"], p["first_price"]), (ts(T0), "na"))  # a later check has no first price

    def test_first_price_only_at_first_sight(self):
        self.found = [addr(4)]
        self.facts[addr(4)] = watched(4)
        with mock.patch.object(base, "token_batch", Batch()):
            self.run_tick(0)
        self.assertEqual(labels.load(self.root)["pending"][addr(4)]["first_price"], good(4)["price"])

    def test_tick_keys_and_rows(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        b = Batch({addr(1): [pair(addr(1))]})
        with mock.patch.object(base, "token_batch", b):
            self.run_tick(0)
            n = self.run_tick(60)
        hb = [detail(r) for r in tick_rows(self.root) if r["stage"] == "TICK"]
        self.assertEqual([h["labels"] for h in hb], ["none", "ok"])
        self.assertEqual({k: hb[1][k] for k in ("labels_due", "labels_done", "labels_calls")},
                         {"labels_due": "1", "labels_done": "1", "labels_calls": "1"})
        self.assertEqual(list(hb[1])[-4:], ["labels", "labels_due", "labels_done", "labels_calls"])
        self.assertEqual(n["requests"], int(hb[1]["requests"]))  # the label request is counted
        self.assertEqual([r["horizon"] for r in self.outcomes()], ["1h"])

    def test_labels_error_never_stops_the_desk(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        with mock.patch.object(labels, "run", side_effect=RuntimeError("labels broke")):
            with self.assertRaises(RuntimeError):  # red, but only after the trade and the TICK row
                self.run_tick(0)
        rows = tick_rows(self.root)
        self.assertEqual([(r["stage"], r["action"]) for r in rows],
                         [("VERIFY", "PASS"), ("EXECUTE", "BUY"), ("TICK", "HEARTBEAT")])
        self.assertEqual(detail(rows[-1])["labels"], "error:RuntimeError")
        self.assertEqual(rows[-1]["reason"], "")
        self.assertIn(addr(1), Book("desk", self.root).positions)

    def test_failed_step_is_retried_next_tick(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = watched(1)  # watched, so labels get a pending entry
        b = Batch({addr(1): [pair(addr(1))]})
        with mock.patch.object(base, "token_batch", b):
            self.run_tick(0)
            with mock.patch.object(base, "token_batch", side_effect=RuntimeError("batch broke")):
                with self.assertRaises(RuntimeError):
                    self.run_tick(60)
            self.run_tick(65)
        hb = [detail(r) for r in tick_rows(self.root) if r["stage"] == "TICK"]
        self.assertEqual([h["labels"] for h in hb], ["none", "error:RuntimeError", "ok"])
        self.assertEqual(hb[1]["labels_calls"], "0")  # the fake raised before any request was counted
        self.assertEqual((hb[2]["labels_done"], [r["horizon"] for r in self.outcomes()]), ("1", ["1h"]))


class DeskUnchangedTest(unittest.TestCase):
    """The same ticks with labels on, off or broken: desk and baseline rows and state equal."""

    T = test_baseline.DeskUnchangedTest
    run_tick, ticks, assert_desk_same = T.run_tick, T.ticks, T.assert_desk_same
    keys = tick.COUNTS + ["goplus"] + tick.BL_COUNTS

    def compare(self, on, off):
        self.assert_desk_same(on, off, self.keys)
        bl = lambda rows: [r for r in rows if r["book"] == baseline.NAME]
        self.assertTrue(bl(on[1]))
        self.assertEqual(bl(on[1]), bl(off[1]))
        self.assertEqual(on[2][baseline.NAME], off[2][baseline.NAME])

    def test_labels_on_off_and_broken(self):
        batch = Batch({addr(i): [pair(addr(i))] for i in range(1, 15)})
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                tempfile.TemporaryDirectory() as c:
            on = self.ticks(Path(a), True, [mock.patch.object(base, "token_batch", batch)])
            off = self.ticks(Path(b), True, [mock.patch.object(labels, "run",
                                                               return_value={"due": 0, "done": 0, "calls": 0})])
            broken = self.ticks(Path(c), True, [mock.patch.object(labels, "run", side_effect=RuntimeError("x"))])
            self.assertTrue((Path(a) / "data/daily/2026-10-24/outcomes.csv").exists())
        self.assertEqual([h["labels"] for h in on[0]], ["none", "none", "ok"])
        self.assertEqual(on[3] + off[3], [])
        self.compare(on, off)
        self.assertEqual([type(e).__name__ for e in broken[3]], ["RuntimeError"] * 3)
        self.compare(broken, off)


if __name__ == "__main__":
    unittest.main()
