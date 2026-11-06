"""Offline tests for BL-32 (R4): the real-depth floor, the qliq ticket cap, impact friction, the logging set and the
one-time Gate B restart. No network."""
import csv
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from adapters import base
from core import baseline, config, desk, exits, friction, labels, report, verify
from core.book import Book
from core.leads import Leads
from tests import test_tick
from tests.test_adapter_base import FRESH, FakeClient, fresh_pairs, load
from tests.test_labels import Batch
from tests.test_tick import T0, addr, detail, good, tick_rows
from tests.test_verify import GOOD

FLOOR = 10_000


def trades(root):
    out = []
    for p in sorted((Path(root) / "data/daily").glob("*/trades.csv")):
        with p.open() as fh:
            out += list(csv.DictReader(fh))
    return out


def thin_pairs(qliq):
    """The asci fixture at 80k total liquidity with exactly `qliq` USD on the ETH side (qliq 0 = a real empty side)."""
    pairs = fresh_pairs(liq=80_000)
    p = pairs[0]
    eth = float(p["priceUsd"]) / float(p["priceNative"])
    p["liquidity"]["quote"] = qliq / eth if qliq else 0.0001  # ~0.24 USD of ETH: a real side that rounds to qliq 0
    return pairs


class FloorTest(unittest.TestCase):
    def test_config(self):
        self.assertEqual(config.SKIP_IF["min_qliq_usd"], FLOOR)
        self.assertEqual(config.SKIP_IF["min_liquidity_usd"], 50_000)  # kept: the logging floor only
        self.assertEqual(verify.DEX_FIELDS[-1], "qliq")

    def test_edges(self):
        cases = {9_999: "real_depth", 10_000: "", 0: "real_depth", None: "missing:qliq"}
        for q, want in cases.items():
            f = dict(GOOD, qliq=q)
            self.assertEqual(verify.dex_skip(f), want, q)
            self.assertEqual(verify.universe_skip(f), want, q)
            self.assertEqual(verify.hard_skip(f), want, q)
        no_key = {k: v for k, v in GOOD.items() if k != "qliq"}
        self.assertEqual(verify.dex_skip(no_key), "missing:qliq")
        self.assertEqual(verify.dex_skip(dict(GOOD, qliq=10_000.0)), "")
        self.assertEqual(verify.dex_skip(dict(GOOD, qliq=9_999.99)), "real_depth")

    def test_total_liquidity_no_longer_a_rule(self):
        for liq in (1, 49_999, 50_000, 10_000_000):
            self.assertEqual(verify.hard_skip(dict(GOOD, liq_usd=liq)), "", liq)
        self.assertEqual(verify.dex_skip(dict(GOOD, liq_usd=None)), "missing:liq_usd")  # still a required number

    def test_order(self):
        """real_depth sits where liquidity sat: after every missing field, before asset, before sells and age."""
        self.assertEqual(verify.dex_skip(dict(GOOD, qliq=None, sells_1h=None)), "missing:sells_1h")
        self.assertEqual(verify.dex_skip(dict(GOOD, qliq=5, asset="stable")), "real_depth")
        self.assertEqual(verify.universe_skip(dict(GOOD, qliq=5, sells_1h=0, age_min=1)), "real_depth")
        self.assertEqual(verify.dex_skip(dict(GOOD, qliq=5, fetch_errors=["ds_pairs"])), "fetch_error")

    def test_real_facts_floor_edges_no_goplus_call(self):
        for q, want, gp in ((9_999, "real_depth", 0), (10_000, "goplus_not_ready", 1), (0, "real_depth", 0)):
            c = FakeClient({"token-pairs": thin_pairs(q), "gopluslabs": load("goplus_fresh")})
            f = base.facts(FRESH, test_tick.T0, c)
            self.assertEqual(f["qliq"], q)
            self.assertEqual(verify.hard_skip(f), want, q)
            self.assertEqual(sum("gopluslabs" in u for _, u in c.calls), gp, q)
            self.assertEqual(bool(f.get("goplus_skipped")), gp == 0)
        pairs = thin_pairs(20_000)
        del pairs[0]["liquidity"]["quote"]  # qliq "na"
        c = FakeClient({"token-pairs": pairs, "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, test_tick.T0, c)
        self.assertNotIn("qliq", f)
        self.assertEqual((verify.hard_skip(f), sum("gopluslabs" in u for _, u in c.calls)), ("missing:qliq", 0))
        self.assertEqual(f["trade"]["qliq"], "na")

    def test_no_request_added(self):
        c = FakeClient({"token-pairs": thin_pairs(20_000), "gopluslabs": load("goplus_fresh")})
        base.facts(FRESH, test_tick.T0, c)
        self.assertEqual([b for b, _ in c.calls], ["ds_pairs", "goplus"])  # as before BL-32
        c = FakeClient({"token-pairs": thin_pairs(500)})
        base.facts(FRESH, test_tick.T0, c)
        self.assertEqual([b for b, _ in c.calls], ["ds_pairs"])


class SameUniverseTest(unittest.TestCase):
    def test_desk_and_twin_get_the_same_answer_from_the_real_facts(self):
        for q in (0, 500, 9_999, 10_000, 40_000, None):
            pairs = thin_pairs(q or 0)
            if q is None:
                del pairs[0]["liquidity"]["quote"]
            d = {}
            c = FakeClient({"token-pairs": pairs, "gopluslabs": load("goplus_fresh")})
            f = base.facts(FRESH, test_tick.T0, c, dex=d)
            self.assertEqual(d.get("qliq"), q)
            desk_gate = verify.dex_skip(f)
            self.assertEqual(verify.universe_skip(d) or "", verify.universe_skip(f) or "", q)
            self.assertEqual(desk_gate, verify.dex_skip(d), q)
            self.assertEqual(FRESH in baseline.eligible({FRESH: d}, set()), not verify.universe_skip(d), q)
        with mock.patch.dict(config.SKIP_IF, {"min_qliq_usd": 50_000}):  # one number moves both books
            self.assertEqual(verify.universe_skip(dict(GOOD, qliq=40_000)), "real_depth")
            self.assertEqual(verify.hard_skip(dict(GOOD, qliq=40_000)), "real_depth")


class FinalSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_depth_is_a_final_24h_gate_skip(self):
        c = FakeClient({"token-pairs": thin_pairs(4_000), "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, T0, c)
        leads = Leads(self.root)
        self.assertEqual(leads.record(FRESH, f, T0), "real_depth")
        row = tick_rows(self.root)[-1]
        self.assertEqual((row["stage"], row["action"], row["reason"]), ("VERIFY", "SKIP", "real_depth"))
        self.assertIn("goplus=skipped", row["detail"].split())
        self.assertEqual(detail(row)["until"], "2026-10-24T12:00:00Z")
        self.assertFalse(any("gopluslabs" in u for _, u in c.calls))
        self.assertEqual(leads.skipped(FRESH, T0 + timedelta(hours=23, minutes=59)), "real_depth")
        self.assertFalse(leads.due(FRESH, T0 + timedelta(hours=23, minutes=59)))
        self.assertTrue(leads.due(FRESH, T0 + timedelta(hours=24, minutes=1)))
        self.assertIn("real_depth", report.GATE_KILLS)
        self.assertEqual(report.kill_stage(row), "gate")

    def test_watched_lead_under_the_floor_ends_without_goplus(self):
        leads = Leads(self.root)
        wait = {k: v for k, v in GOOD.items() if k != "honeypot"}
        self.assertEqual(leads.record(FRESH, dict(wait, goplus_pending=["honeypot"]), T0), "goplus_not_ready")
        c = FakeClient({"token-pairs": thin_pairs(9_999), "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, T0 + timedelta(minutes=15), c)
        self.assertEqual(leads.record(FRESH, f, T0 + timedelta(minutes=15)), "real_depth")
        self.assertNotIn(FRESH, leads.s["watch"])
        self.assertFalse(any("gopluslabs" in u for _, u in c.calls))


class TicketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def f(self, liq, qliq, symbol):
        return {"symbol": symbol, "price": 1.0, "liq_usd": liq, "qliq": qliq, "launchpad": "other"}

    def test_cap_on_qliq_not_liq(self):
        self.assertEqual(desk.size(50, 499), (0.0, "size"))      # 0.01 x 499 = 4.99 < MIN_TICKET
        self.assertEqual(desk.size(50, 500), (5.0, ""))
        self.assertEqual(desk.size(50, 10_000), (5.0, ""))       # never binds above the floor
        self.assertIsNone(desk.buy(self.book, "0xa", self.f(10_000_000, 400, "A"), T0))  # deep total, thin real
        self.assertEqual(tick_rows(self.root)[-1]["reason"], "size")
        pos = desk.buy(self.book, "0xb", self.f(400, 10_000, "B"), T0)  # thin total, real depth at the floor
        self.assertEqual(pos["cost_usd"], 5.0)
        self.assertEqual(config.SKIP_IF["max_ticket_share"], 0.01)


class FrictionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self, qliq=20_000, launchpad="clanker", price=1.0):
        return desk.buy(self.book, "0xa", {"symbol": "A", "price": price, "liq_usd": 900_000, "qliq": qliq,
                                           "launchpad": launchpad}, T0)

    def test_side_pct_parts(self):
        self.assertAlmostEqual(friction.side_pct(5, 10_000, "other"), 3.05)
        self.assertAlmostEqual(friction.side_pct(5, 10_000, "clanker"), 4.05)
        self.assertAlmostEqual(friction.side_pct(100, 1_000), 13.0)
        self.assertEqual(friction.side_pct(5, None, "other"), 8.0)     # no qliq: base + thin_pct
        self.assertEqual(friction.side_pct(5, None, "clanker"), 9.0)
        self.assertEqual(friction.impact_pct(5, 0), 100.0)              # a real empty side: the cap
        self.assertEqual(friction.impact_pct(5_000, 10), 100.0)
        self.assertIsNone(friction.impact_pct(5, "na"))

    def test_entry(self):
        pos = self.buy()
        pct = 3 + 1 + 100 * 5 / 20_000
        self.assertAlmostEqual(pos["entry_fill"], 1 + pct / 100)
        self.assertEqual(pos["entry_qliq"], 20_000)
        t = trades(self.root)[-1]
        self.assertAlmostEqual(float(t["friction_pct"]), pct)
        self.assertEqual(t["note"], "impact_pct=0.0250 qliq=20000 qliq_src=entry")

    def sell_row(self, quote_qliq, price=1.2, minutes=5):
        exits.check(self.book, lambda a: {"price": price, "liq_usd": 900_000, "source": "pool", "qliq": quote_qliq},
                    T0 + timedelta(minutes=minutes))
        return trades(self.root)[-1]

    def test_exit_uses_this_ticks_qliq(self):
        pos = self.buy()
        qty = pos["qty"]
        t = self.sell_row(3_000, price=2.5)  # ladder_1 (+40%) on a pool that thinned to 3k real depth
        sold = qty * 0.33
        imp = 100 * sold * 2.5 / 3_000
        self.assertEqual(t["reason"], "ladder_1")
        self.assertAlmostEqual(float(t["friction_pct"]), 3 + 1 + imp)
        self.assertAlmostEqual(float(t["fill_price"]), 2.5 * (1 - (4 + imp) / 100))
        self.assertEqual(t["note"], f"impact_pct={imp:.4f} qliq=3000 qliq_src=exit")

    def test_exit_falls_back_to_the_entry_qliq(self):
        qty = self.buy()["qty"]
        t = self.sell_row(None, price=0.5)  # hard stop; this tick's quote has no qliq
        imp = 100 * qty * 0.5 / 20_000
        self.assertEqual(t["reason"], "hard_stop")
        self.assertAlmostEqual(float(t["friction_pct"]), 4 + imp)
        self.assertEqual(t["note"], f"impact_pct={imp:.4f} qliq=20000 qliq_src=entry")

    def test_no_qliq_anywhere_is_base_fee_and_thin_pct(self):
        self.buy()
        self.book.positions["0xa"]["entry_qliq"] = None  # a position from before BL-27
        t = self.sell_row(None, price=0.5)
        self.assertEqual(float(t["friction_pct"]), 3 + 1 + 5)
        self.assertEqual(t["note"], "impact_pct=na qliq=na qliq_src=none")

    def test_sell_friction_capped_at_100(self):
        cost = self.buy()["cost_usd"]
        t = self.sell_row(0, price=0.5)  # the non-token side is empty: nothing to sell into
        self.assertEqual((float(t["friction_pct"]), float(t["fill_price"]), float(t["usd"])), (100.0, 0.0, 0.0))
        self.assertAlmostEqual(float(t["pnl_usd"]), -cost)
        self.assertEqual(self.book.cash, config.START_BALANCE - 5.0)
        self.assertGreaterEqual(friction.sell_fill(1.0, 1e9, 1.0)[0], 0.0)

    def test_report_and_live_price_the_same_way(self):
        self.buy()
        exits.check(self.book, lambda a: {"price": 1.1, "liq_usd": 900_000, "source": "pool", "qliq": 7_000},
                    T0 + timedelta(minutes=5))
        p = self.book.positions["0xa"]
        want = p["qty"] * friction.sell_fill(1.1, p["qty"], 7_000, "clanker")[0]
        self.assertAlmostEqual(report.liq_value(self.book) - self.book.cash, want)

    def test_both_books_one_function(self):
        twin = Book(baseline.NAME, self.root)
        f = {"symbol": "A", "price": 1.0, "liq_usd": 60_000, "qliq": 12_000, "launchpad": "other"}
        a, b = desk.buy(self.book, "0xa", dict(f), T0), desk.buy(twin, "0xa", dict(f), T0)
        self.assertEqual((a["entry_fill"], a["qty"]), (b["entry_fill"], b["qty"]))


class LoggingSetTest(unittest.TestCase):
    """The owner's BL-32 rule A: the real-depth floor decides what the books trade, not what is logged."""

    T = test_tick.TickTest
    setUp, tearDown, run_tick = T.setUp, T.tearDown, T.run_tick

    @staticmethod
    def old_logged(f):
        """The pre-BL-32 logging set, written out: past the old dex_skip() (no fetch error, the five old DexScreener
        fields, total liq >= 50k, no asset kind)."""
        return (not f.get("fetch_errors") and all(f.get(k) is not None for k in
                                                  ("symbol", "price", "liq_usd", "age_min", "sells_1h"))
                and f["liq_usd"] >= 50_000 and not f.get("asset"))

    def leads(self):
        def t(i, liq, qliq, **kw):
            trade = {"b1": 1, "s1": 1, "b24": 9, "s24": 9, "liq": liq, "dex": "uniswap:v4",
                     "qliq": "na" if qliq is None else qliq, "pair_age": 60, "token_age": 60}
            d = dict(good(i), liq_usd=liq, trade=trade, **kw)
            if qliq is None:
                d.pop("qliq")
            else:
                d["qliq"] = qliq
            return d
        return {
            addr(1): t(1, 80_000, 40_000),                    # passes: logged before and after
            addr(2): t(2, 80_000, 4_000),                     # real_depth now; was past the gate: still logged
            addr(3): t(3, 80_000, None),                      # missing:qliq now; was past the gate: still logged
            addr(4): t(4, 20_000, 15_000, honeypot=True),     # new to the universe: logged now
            addr(5): t(5, 20_000, 4_000),                     # under both floors: never logged
            addr(6): t(6, 80_000, 4_000, asset="stable"),     # an asset: never logged
            addr(7): t(7, 80_000, 40_000, honeypot=True),     # safety skip: logged before and after
            addr(8): t(8, 80_000, 0),                         # real 0: real_depth, still logged
        }

    def test_label_set_is_todays_plus_new_tokens_only(self):
        facts = self.leads()
        self.found, self.facts = list(facts), facts
        with mock.patch.object(base, "token_batch", Batch()):
            n = self.run_tick(0)
        before = {a for a, f in facts.items() if self.old_logged(f)}
        pend = set(labels.load(self.root)["pending"])
        self.assertEqual(before, {addr(1), addr(2), addr(3), addr(7), addr(8)})
        self.assertEqual(pend, before | {addr(4)})
        self.assertTrue(before <= pend)
        rows = {r["address"]: r for r in tick_rows(self.root) if r["stage"] == "VERIFY"}
        keyed = {a for a, r in rows.items() if "qliq=" in r["detail"]}
        self.assertEqual(keyed, pend)  # trade keys on exactly the logged checks
        r2 = rows[addr(2)]
        self.assertEqual((r2["action"], r2["reason"]), ("SKIP", "real_depth"))
        self.assertIn("goplus=skipped", r2["detail"].split())
        d2 = detail(r2)
        self.assertEqual((d2["liq"], d2["qliq"], d2["s1"], d2["pair_age"]), ("80000", "4000", "1", "60"))
        self.assertEqual((rows[addr(3)]["reason"], detail(rows[addr(3)])["qliq"]), ("missing:qliq", "na"))
        self.assertEqual(rows[addr(5)]["reason"], "real_depth")
        self.assertNotIn("qliq=", rows[addr(5)]["detail"])
        self.assertEqual(rows[addr(6)]["reason"], "real_depth")
        self.assertNotIn("qliq=", rows[addr(6)]["detail"])
        self.assertEqual(rows[addr(4)]["reason"], "honeypot")  # past the gate now, GoPlus decides
        self.assertEqual(rows[addr(8)]["reason"], "real_depth")
        self.assertEqual(labels.load(self.root)["pending"][addr(2)]["sight_qliq"], 4000)
        self.assertLessEqual(n["labels_calls"], 2)
        trading = {a for a, r in rows.items() if r["reason"] in ("", "honeypot", "goplus_not_ready")}
        self.assertEqual(trading, {addr(1), addr(4), addr(7)})  # only qliq >= 10k reaches GoPlus or a buy

    def test_labels_follow_a_real_depth_skip(self):
        facts = self.leads()
        self.found, self.facts = [addr(2)], {addr(2): facts[addr(2)]}
        self.run_tick(0)
        b = Batch({addr(2): [test_tick_pair(addr(2))]})
        with mock.patch.object(base, "token_batch", b):
            n = self.run_tick(61)
        self.assertEqual((n["labels_due"], n["labels_done"], n["labels_calls"]), (1, 1, 1))
        with open(self.root / "data/daily/2026-10-23/outcomes.csv") as fh:
            out = list(csv.DictReader(fh))
        self.assertEqual([(r["address"], r["horizon"]) for r in out], [(addr(2), "1h")])

    def test_logged_never_raises_and_is_not_a_rule(self):
        self.assertFalse(verify.logged(None))
        self.assertFalse(verify.logged({"goplus_skipped": True}))
        self.assertTrue(verify.logged(dict(GOOD)))  # GoPlus was called: logged, as before
        self.assertFalse(verify.logged(dict(GOOD, qliq=5, liq_usd=49_999, goplus_skipped=True)))
        self.assertTrue(verify.logged(dict(GOOD, qliq=5, liq_usd=50_000, goplus_skipped=True)))
        self.assertFalse(verify.logged(dict(GOOD, fetch_errors=["ds_pairs"], goplus_skipped=True)))


def test_tick_pair(a):
    from tests.test_labels import pair
    return pair(a)


class GateRestartTest(unittest.TestCase):
    """d) Gate B and per-trade stats count again from the first tick on the BL-32 code, once, both books, no refill."""

    T = test_tick.TickTest
    setUp, tearDown, run_tick = T.setUp, T.tearDown, T.run_tick

    def bl31_state(self):
        """The live state after BL-31: the twin restarted, per_trade_since set, no gate_restart flag yet."""
        for name, cash in (("desk", 47.61), (baseline.NAME, 45.0)):
            b = Book(name, self.root)
            b.s.update(cash=cash, day="2026-10-23", day_start_value=cash, refill_week="2026-W43")
            b.save()
        rs = report.load(self.root, T0)
        rs["per_trade_since"] = "2026-10-22T17:33:13Z"
        report.save(self.root, rs)

    def rs(self, root=None):
        return json.loads(((root or self.root) / config.STATE).read_text())["report"]

    def restarts(self, *roots):
        return [r for root in roots for r in tick_rows(root) if r["action"] == "RESTART"]

    def test_once_with_its_row_and_no_refill(self):
        self.bl31_state()
        self.run_tick(0)
        rows = self.restarts(self.root)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["book"], r["stage"], r["action"], r["reason"]), ("desk", "BOOK", "RESTART", "gate_b"))
        self.assertEqual(r["detail"], "card=BL-32 since=2026-10-23T12:00:00Z prev_since=2026-10-22T17:33:13Z "
                                      "books=desk,baseline refill=none")
        self.assertEqual((self.rs()["per_trade_since"], self.rs()["gate_restart"]), ("2026-10-23T12:00:00Z", "BL-32"))
        self.assertEqual((Book("desk", self.root).cash, Book(baseline.NAME, self.root).cash), (47.61, 45.0))
        self.assertEqual([x for x in tick_rows(self.root) if x["action"] == "REFILL"], [])  # no extra refill
        self.run_tick(5)
        self.assertEqual(len(self.restarts(self.root)), 1)

    def test_once_across_a_reload_a_day_roll_and_a_monday(self):
        self.bl31_state()
        self.run_tick(0)
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        (Path(fresh.name) / "data").mkdir()
        (Path(fresh.name) / config.STATE).write_text((self.root / config.STATE).read_text())
        first, self.root = self.root, Path(fresh.name)
        self.run_tick(5)
        self.run_tick(13 * 60)          # Sat 01:00: day roll
        self.run_tick(60 * 60 + 3)      # Mon 2026-10-26 00:03: the weekly refills run as built
        self.assertEqual(len(self.restarts(first, self.root)), 1)
        self.assertEqual(self.rs()["per_trade_since"], "2026-10-23T12:00:00Z")  # never moved again
        self.assertEqual([r["reason"] for r in tick_rows(self.root) if r["action"] == "REFILL"], ["weekly", "weekly"])
        rep = json.loads((self.root / report.REPORT).read_text())
        self.assertEqual(rep["gate_b"]["since"], "2026-10-23T12:00:00Z")
        for b in report.BOOKS:
            self.assertEqual(rep[b]["per_trade"]["since"], "2026-10-23T12:00:00Z")

    def test_old_closes_stay_history_not_gate_b(self):
        self.bl31_state()
        bl = Book(baseline.NAME, self.root)  # a twin trade opened and closed before the BL-32 deploy
        desk.buy(bl, "0xold", {"symbol": "OLD", "price": 1.0, "liq_usd": 90_000, "qliq": 50_000, "launchpad": "other"},
                 T0 - timedelta(hours=2))
        desk.sell(bl, "0xold", 1.0, None, "time_stop", 90_000, T0 - timedelta(hours=1))
        self.run_tick(0)
        self.run_tick(13 * 60)  # the day roll writes report.json
        rep = json.loads((self.root / report.REPORT).read_text())
        self.assertEqual((rep["gate_b"]["baseline_closes"], rep["baseline"]["per_trade"]["n"]), (0, 0))
        self.assertGreaterEqual(rep["baseline"]["closes"], 1)  # history keeps it

    def test_fresh_state_needs_no_second_restart(self):
        self.run_tick(0)  # BL-31 restart on an empty state already counts from now
        self.assertEqual(self.restarts(self.root), [])
        self.assertEqual(self.rs()["gate_restart"], "BL-32")
        self.assertEqual(len([r for r in tick_rows(self.root) if r["reason"] == "restart"]), 1)


if __name__ == "__main__":
    unittest.main()
