"""Offline tests for BL-31: the random twin (same universe, asset exclusion, fixed ticket, cooldown, refills,
restart, per-trade report and Gate B from the restart). No network."""
import copy
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from adapters import base
from core import baseline, config, desk, report, verify
from core.book import Book, week
from core.leads import Leads
from tests import test_tick
from tests.test_adapter_base import FRESH, FakeClient, fresh_pairs, load
from tests.test_tick import T0, addr, good, tick_rows
from tests.test_verify import GOOD

ZORA = "0x1111111111166b7fe7bd91427724b487980afc69"


def dex(i, **kw):
    d = {k: GOOD[k] for k in verify.DEX_FIELDS}
    return {**d, "pair": f"0x{i:064x}", "launchpad": "other", "fetch_errors": [], "asset": "", **kw}


def facts(symbol="TEST", liq=150_000, price=1.0, qliq=150_000):
    return {"symbol": symbol, "price": price, "liq_usd": liq, "launchpad": "uniswap", "qliq": qliq}


class AssetKindTest(unittest.TestCase):
    def kind(self, symbol, price=0.001, address="0x2", base_pairs=1, quote_pairs=0):
        return verify.asset_kind(symbol, price, address, base_pairs, quote_pairs)

    def test_stables_seen_by_the_twin(self):
        for s in ["msUSD", "USDF", "USDL", "USDR", "USDC", "usdbc", "tGBP", "EURC", "XSGD", "DAI", "GHO"]:
            self.assertEqual(self.kind(s, 1.0), "stable", s)
        self.assertEqual(self.kind("USDF", 0.02), "stable")  # a stable name at any price (clones too)

    def test_wrapped(self):
        for s in ["cbETH", "msETH", "wstETH", "weETH", "rETH", "cbBTC", "WBTC", "superOETHb", "ETH"]:
            self.assertEqual(self.kind(s, 0.5), "wrapped", s)
        self.assertEqual(self.kind("wtCOIN", 148.0), "wrapped")      # by price: tokenized stock
        self.assertEqual(self.kind("ANY", 100.0), "wrapped")         # the edge: 100 USD
        self.assertEqual(self.kind("ANY", 99.99), "")

    def test_quote(self):
        self.assertEqual(self.kind("Z", 0.01, ZORA), "quote")
        self.assertEqual(self.kind("Z", 0.01, ZORA.upper().replace("0X", "0x")), "quote")
        self.assertEqual(self.kind("X", 0.01, "0x2", 1, 2), "quote")  # quote side in most of its Base pairs
        self.assertEqual(self.kind("X", 0.01, "0x2", 1, 1), "")       # a tie is not
        self.assertEqual(self.kind("X", 0.01, "0x2", 0, 1), "quote")

    def test_launch_names_stay_in(self):
        for s in ["NAFF", "I O F", "TRUMP2028", "EUREKA", "AUDIO", "CADDY", "BETH", "ONDO", "TOSHI", "USOE", "猫"]:
            self.assertEqual(self.kind(s), "", s)

    def test_never_raises(self):
        self.assertEqual(verify.asset_kind(None, None, None, None, None), "")
        self.assertEqual(verify.asset_kind("X", "1000", "0x2"), "")  # a non-number price is no price
        self.assertEqual(verify.asset_kind("X", True, "0x2"), "")

    def test_norm_symbol(self):
        self.assertEqual(verify.norm_symbol("I O F"), "IOF")
        self.assertEqual(verify.norm_symbol("i.o-f"), "IOF")
        self.assertEqual(verify.norm_symbol("U.S OIL EXCHANGE"), "USOILEXCHANGE")
        self.assertEqual(verify.norm_symbol("猫"), "猫")
        self.assertEqual(verify.norm_symbol("🚀"), "")
        self.assertEqual(verify.norm_symbol(None), "")


class UniverseTest(unittest.TestCase):
    def test_rules_and_order(self):
        self.assertEqual(verify.universe_skip(dex(1)), "")
        self.assertEqual(verify.universe_skip(dex(1, fetch_errors=["ds_pairs"])), "fetch_error")
        self.assertEqual(verify.universe_skip({"fetch_errors": []}), "missing:symbol")
        self.assertEqual(verify.universe_skip(dex(1, qliq=1, asset="stable")), "real_depth")  # BL-32, before asset
        self.assertEqual(verify.universe_skip(dex(1, liq_usd=1, asset="stable")), "asset:stable")  # total liq: no rule
        self.assertEqual(verify.universe_skip(dex(1, qliq=None, sells_1h=0)), "missing:qliq")
        self.assertEqual(verify.universe_skip(dex(1, asset="stable", sells_1h=0)), "asset:stable")
        self.assertEqual(verify.universe_skip(dex(1, asset="wrapped")), "asset:wrapped")
        self.assertEqual(verify.universe_skip(dex(1, asset="quote")), "asset:quote")
        self.assertEqual(verify.universe_skip(dex(1, sells_1h=0, age_min=1)), "nobody_can_sell")
        self.assertEqual(verify.universe_skip(dex(1, age_min=14.9)), "too_young")
        self.assertEqual(verify.universe_skip(dex(1, age_min=15)), "")
        self.assertEqual(verify.universe_skip(dex(1, age_min=48 * 60)), "")
        self.assertEqual(verify.universe_skip(dex(1, age_min=48 * 60 + 0.1)), "too_old")
        self.assertEqual(verify.universe_skip(dex(1, asset=None)), "")  # no asset key: a classification, not a number

    def test_same_function_as_the_desk(self):
        """Called, not copied: a threshold change moves the desk's hard_skip() and the twin's universe together."""
        young = dict(GOOD, age_min=30)
        self.assertEqual((verify.hard_skip(young), verify.universe_skip(young)), ("", ""))
        with mock.patch.dict(config.SKIP_IF, {"min_age_min": 45}):
            self.assertEqual((verify.hard_skip(young), verify.universe_skip(young)), ("too_young", "too_young"))
        with mock.patch.dict(config.SKIP_IF, {"min_qliq_usd": 90_000}):  # BL-32: the floor moves both books
            self.assertEqual((verify.hard_skip(young), verify.universe_skip(young)), ("real_depth", "real_depth"))
        with mock.patch.dict(config.SKIP_IF, {"min_liquidity_usd": 90_000}):  # the logging floor moves neither
            self.assertEqual((verify.hard_skip(young), verify.universe_skip(young)), ("", ""))
        with mock.patch.object(verify, "sells_skip", return_value="nobody_can_sell"):
            self.assertEqual((verify.hard_skip(young), verify.universe_skip(young)),
                             ("nobody_can_sell", "nobody_can_sell"))

    def test_desk_labels_unchanged(self):
        # GoPlus rules still come first for the desk, exactly as before BL-31
        self.assertEqual(verify.hard_skip(dict(GOOD, honeypot=True, sells_1h=0, age_min=1)), "honeypot")
        self.assertEqual(verify.hard_skip(dict(GOOD, dev_pct=50, age_min=1)), "dev_pct")
        self.assertEqual(verify.hard_skip(dict(GOOD, sells_1h=0)), "nobody_can_sell")
        self.assertEqual(verify.hard_skip(dict(GOOD, age_min=49 * 60)), "too_old")

    def test_eligible_uses_the_universe(self):
        seen = {addr(1): dex(1), addr(2): dex(2, age_min=5), addr(3): dex(3, age_min=3000),
                addr(4): dex(4, asset="stable"), addr(5): dex(5, asset="wrapped"), addr(6): dex(6, asset="quote"),
                addr(7): dex(7, sells_1h=0), addr(8): dex(8)}
        self.assertEqual(baseline.eligible(seen, held={addr(8)}), [addr(1)])
        with mock.patch.object(baseline, "universe_skip", return_value=""):
            self.assertEqual(len(baseline.eligible(seen, set())), 8)


class FactsAssetTest(unittest.TestCase):
    """The real facts(): asset from the pairs already fetched, a gate skip before GoPlus, no extra request."""

    def run_facts(self, pairs):
        d, c = {}, FakeClient({f"token-pairs/v1/base/{FRESH}": pairs,
                               f"contract_addresses={FRESH}": load("goplus_fresh")})
        f = base.facts(FRESH, now=T0, client=c, dex=d)
        return f, d, c

    def test_launch_token_is_no_asset(self):
        f, d, _ = self.run_facts(fresh_pairs())
        self.assertEqual((f["asset"], d["asset"]), ("", ""))

    def test_stable_symbol_is_a_gate_skip_without_goplus(self):
        pairs = fresh_pairs()
        pairs[0]["baseToken"]["symbol"] = "msUSD"
        f, d, c = self.run_facts(pairs)
        self.assertTrue(f["goplus_skipped"])
        self.assertEqual(verify.hard_skip(f), "asset:stable")
        self.assertEqual(d["asset"], "stable")
        self.assertFalse(any("gopluslabs" in url for _, url in c.calls))
        self.assertEqual(len(c.calls), 1)

    def test_quote_side_counts(self):
        pairs = fresh_pairs()
        other = copy.deepcopy(pairs[0])
        other["pairAddress"] = "0x" + "ab" * 32
        other["baseToken"], other["quoteToken"] = ({"address": "0x" + "cd" * 20, "symbol": "X"},
                                                   copy.deepcopy(pairs[0]["baseToken"]))
        other["liquidity"]["usd"] = 1_000
        third = copy.deepcopy(other)
        third["pairAddress"] = "0x" + "ef" * 32
        self.assertEqual(base.side_counts(pairs + [other, third], FRESH), (1, 2))
        f, _, _ = self.run_facts(pairs + [other, third])
        self.assertEqual(verify.hard_skip(f), "asset:quote")
        other["chainId"] = third["chainId"] = "ethereum"  # only Base pairs count
        self.assertEqual(base.side_counts(pairs + [other, third], FRESH), (1, 0))

    def test_price_over_100_is_never_picked_by_the_twin(self):
        """The twin's path end to end: real facts() -> dex dict -> eligible(), at and above the 100 USD edge."""
        for price, picked in (("99.99", True), ("100", False), ("2120.92", False)):
            pairs = fresh_pairs()
            pairs[0]["priceUsd"] = price
            pairs[0]["baseToken"]["symbol"] = "AODF"
            _, d, _ = self.run_facts(pairs)
            self.assertEqual(d["asset"], "" if picked else "wrapped", price)
            self.assertEqual(baseline.eligible({FRESH: d}, set()), [FRESH] if picked else [], price)

    def test_desk_row_is_skip_asset_with_goplus_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            pairs = fresh_pairs()
            pairs[0]["baseToken"]["symbol"] = "cbETH"
            f, _, _ = self.run_facts(pairs)
            leads = Leads(Path(tmp))
            self.assertEqual(leads.record(FRESH, f, T0), "asset:wrapped")
            row = tick_rows(Path(tmp))[-1]
            self.assertEqual((row["action"], row["reason"]), ("SKIP", "asset:wrapped"))
            self.assertIn("goplus=skipped", row["detail"].split())
            self.assertTrue(leads.skipped(FRESH, T0 + timedelta(hours=23)))  # final for 24 h, like any skip


class TicketCooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self):
        return tick_rows(self.root)[-1]

    def test_fixed_ticket_whatever_the_cash(self):
        self.book.s["cash"] = 400.0
        self.assertEqual(desk.buy(self.book, "0xa", facts("A"), T0)["cost_usd"], 5.0)
        self.book.s.update(cash=4.99, positions={}, day_start_value=4.99)  # no loss halt in the way
        self.assertIsNone(desk.buy(self.book, "0xb", facts("B"), T0))
        self.assertEqual((self.last()["action"], self.last()["reason"]), ("SKIP", "size"))

    def test_cooldown_by_address_and_symbol_after_a_full_exit(self):
        desk.buy(self.book, "0xa", facts("IOF"), T0)
        desk.sell(self.book, "0xa", 1.0, None, "time_stop", 150_000, T0 + timedelta(minutes=45))
        t = T0 + timedelta(minutes=50)
        self.assertIsNone(desk.buy(self.book, "0xa", facts("IOF"), t))
        self.assertEqual((self.last()["stage"], self.last()["action"], self.last()["reason"]),
                         ("EXECUTE", "SKIP", "reentry_cooldown"))
        self.assertIsNone(desk.buy(self.book, "0xb", facts("I O F"), t))  # same normalized symbol, new address
        self.assertEqual(self.last()["reason"], "reentry_cooldown")
        self.assertIsNotNone(desk.buy(self.book, "0xc", facts("NAFF"), t))
        end = T0 + timedelta(minutes=45, hours=24)
        self.assertIsNone(desk.buy(self.book, "0xb", facts("iof"), end - timedelta(seconds=1)))
        self.assertIsNotNone(desk.buy(self.book, "0xb", facts("iof"), end))

    def test_a_ladder_sell_starts_it_too(self):
        desk.buy(self.book, "0xa", facts("A"), T0)
        desk.sell(self.book, "0xa", 1.5, self.book.positions["0xa"]["qty"] * 0.33, "ladder_1", 150_000,
                  T0 + timedelta(minutes=10))
        desk.sell(self.book, "0xa", 1.5, None, "trail", 150_000, T0 + timedelta(minutes=20))
        self.assertTrue(desk.cooling(self.book, "0xa", "A", T0 + timedelta(hours=24, minutes=19)))
        self.assertFalse(desk.cooling(self.book, "0xa", "A", T0 + timedelta(hours=24, minutes=20)))

    def test_the_window_ends_24_h_after_the_last_sale(self):
        desk.buy(self.book, "0xa", facts("A"), T0)
        desk.sell(self.book, "0xa", 1.5, self.book.positions["0xa"]["qty"] * 0.33, "ladder_1", 150_000, T0)
        self.assertEqual(self.book.s["cooldown"]["a:0xa"], "2026-10-24T12:00:00Z")  # 24 h after the ladder sale
        final = T0 + timedelta(hours=30)  # held past the ladder's window, then the final exit
        desk.sell(self.book, "0xa", 1.5, None, "trail", 150_000, final)
        self.assertEqual(self.book.s["cooldown"], {"a:0xa": "2026-10-25T18:00:00Z", "s:A": "2026-10-25T18:00:00Z"})
        self.assertTrue(desk.cooling(self.book, "0xa", "A", final + timedelta(hours=23, minutes=59)))
        self.assertIsNone(desk.buy(self.book, "0xb", facts("A"), final + timedelta(hours=23, minutes=59)))
        self.assertEqual(self.last()["reason"], "reentry_cooldown")
        self.assertIsNotNone(desk.buy(self.book, "0xa", facts("A"), final + timedelta(hours=24)))

    def test_old_keys_pruned_on_the_next_sell(self):
        desk.buy(self.book, "0xa", facts("A"), T0)
        desk.sell(self.book, "0xa", 1.0, None, "time_stop", 150_000, T0)
        later = T0 + timedelta(hours=25)
        desk.buy(self.book, "0xb", facts("B"), later)
        desk.sell(self.book, "0xb", 1.0, None, "time_stop", 150_000, later)
        self.assertEqual(set(self.book.s["cooldown"]), {"a:0xb", "s:B"})

    def test_each_book_keeps_its_own(self):
        desk.buy(self.book, "0xa", facts("A"), T0)
        desk.sell(self.book, "0xa", 1.0, None, "time_stop", 150_000, T0)
        twin = Book(baseline.NAME, root=self.root)
        self.assertIsNotNone(desk.buy(twin, "0xa", facts("A"), T0 + timedelta(minutes=5)))

    def test_symbol_without_letters_keys_on_the_address_only(self):
        desk.buy(self.book, "0xa", facts("🚀"), T0)
        desk.sell(self.book, "0xa", 1.0, None, "time_stop", 150_000, T0)
        self.assertEqual(set(self.book.s["cooldown"]), {"a:0xa"})
        self.assertIsNotNone(desk.buy(self.book, "0xb", facts("🚀"), T0 + timedelta(minutes=5)))

    def test_same_symbol_while_held_is_holding(self):
        desk.buy(self.book, "0xa", facts("NAFF"), T0)
        self.assertIsNone(desk.buy(self.book, "0xb", facts("naff"), T0))
        self.assertEqual((self.last()["action"], self.last()["reason"]), ("SKIP", "holding"))
        self.assertIsNotNone(desk.buy(self.book, "0xc", facts("OTHER"), T0))
        twin = Book(baseline.NAME, root=self.root)
        self.assertIsNotNone(desk.buy(twin, "0xb", facts("NAFF"), T0))  # the other book's position does not count


class RefillTest(unittest.TestCase):
    MON = datetime(2026, 11, 9, 0, 3, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_week(self):
        self.assertEqual(week(self.MON - timedelta(minutes=4)), "2026-W45")  # Sun 23:59
        self.assertEqual(week(self.MON), "2026-W46")

    def test_due_only_with_a_week_and_a_new_one(self):
        self.assertFalse(self.book.refill_due(self.MON))  # no refill_week yet (set by the restart)
        self.book.s["refill_week"] = "2026-W45"
        self.assertFalse(self.book.refill_due(self.MON - timedelta(minutes=4)))
        self.assertTrue(self.book.refill_due(self.MON))

    def test_amount_halt_and_row(self):
        self.book.s.update(cash=3.19, day=self.MON.strftime("%Y-%m-%d"), day_start_value=3.19)
        self.assertAlmostEqual(self.book.refill(self.MON, "weekly"), 46.81)
        self.assertEqual(self.book.cash, 50.0)
        self.assertAlmostEqual(self.book.s["day_start_value"], 50.0)  # the loss halt never sees it as a gain
        self.assertAlmostEqual(self.book.s["refills_usd"], 46.81)
        self.assertEqual(self.book.s["refill_week"], "2026-W46")
        self.assertFalse(self.book.halted(self.MON))
        self.book.s["cash"] = 34.0  # -32% of the refilled start: the halt still works
        self.assertTrue(self.book.halted(self.MON))
        r = [x for x in tick_rows(self.root) if x["action"] == "REFILL"][0]
        self.assertEqual((r["book"], r["stage"], r["reason"], r["detail"]),
                         ("desk", "BOOK", "weekly", "amount=46.81 cash_before=3.19 cash_after=50.00 week=2026-W46"))

    def test_nothing_to_add_still_logged(self):
        self.book.s["cash"] = 61.0
        self.assertEqual(self.book.refill(self.MON, "weekly"), 0.0)
        self.assertEqual(self.book.cash, 61.0)
        self.assertIn("amount=0.00", tick_rows(self.root)[-1]["detail"])


class TwinTickTest(unittest.TestCase):
    """The tick with the adapter faked (tests/test_tick.py TickTest's fakes)."""

    T = test_tick.TickTest
    setUp, tearDown, run_tick = T.setUp, T.tearDown, T.run_tick

    def old_state(self, twin_cash=3.19, desk_cash=47.61, ticks=752):
        for name, cash in (("desk", desk_cash), (baseline.NAME, twin_cash)):
            b = Book(name, self.root)
            b.s.update(cash=cash, day="2026-10-23", day_start_value=cash)
            if name == baseline.NAME:
                b.s["ticks_since_buy"] = ticks
            b.save()

    def refills(self):
        return [r for r in tick_rows(self.root) if r["action"] == "REFILL"]

    def rs(self):
        return json.loads((self.root / config.STATE).read_text())["report"]

    def test_restart_once(self):
        self.old_state()
        self.run_tick(0)
        self.run_tick(5)
        rows = self.refills()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["book"], rows[0]["reason"]), ("baseline", "restart"))
        self.assertIn("amount=46.81 cash_before=3.19 cash_after=50.00", rows[0]["detail"])
        self.assertEqual(Book(baseline.NAME, self.root).cash, 50.0)
        self.assertEqual(Book("desk", self.root).cash, 47.61)  # the desk is not restarted
        self.assertEqual(self.rs()["per_trade_since"], "2026-10-23T12:00:00Z")
        for name in ("desk", baseline.NAME):
            self.assertEqual(Book(name, self.root).s["refill_week"], "2026-W43")

    def test_restart_once_across_reloads_a_day_roll_and_a_monday(self):
        self.old_state()
        self.run_tick(0)
        self.assertEqual(self.rs()["per_trade_since"], "2026-10-23T12:00:00Z")  # the flag
        # a reloaded state: only data/state.json carried to a fresh root, as a new Actions checkout would
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        (Path(fresh.name) / "data").mkdir()
        (Path(fresh.name) / config.STATE).write_text((self.root / config.STATE).read_text())
        first_root, self.root = self.root, Path(fresh.name)
        self.run_tick(5)
        self.run_tick(13 * 60)           # Sat 01:00: day roll
        self.run_tick(60 * 60 + 3)       # Mon 2026-10-26 00:03: weekly refills, still no second restart
        restarts = [r for r in tick_rows(first_root) + tick_rows(self.root)
                    if r["action"] == "REFILL" and r["reason"] == "restart"]
        self.assertEqual(len(restarts), 1)
        self.assertEqual([r["reason"] for r in self.refills()], ["weekly", "weekly"])
        self.assertEqual(self.rs()["per_trade_since"], "2026-10-23T12:00:00Z")  # never moved

    def test_restarted_twin_buys_again(self):
        self.old_state()
        self.found = [addr(1)]
        self.facts[addr(1)] = dict(good(1), honeypot=True)  # the desk skips it, the twin may pick it
        self.run_tick(0)
        buys = [r for r in tick_rows(self.root) if r["book"] == "baseline" and r["action"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertIn("usd=5.00", buys[0]["detail"])

    def test_twin_skips_an_asset_and_a_young_pool(self):
        self.old_state()
        self.found = [addr(1), addr(2)]
        self.facts[addr(1)] = dict(good(1), asset="stable")
        self.facts[addr(2)] = dict(good(2), age_min=5, honeypot=True)
        self.run_tick(0)
        rows = tick_rows(self.root)
        desk_v = {r["address"]: r["reason"] for r in rows if r["book"] == "desk" and r["stage"] == "VERIFY"}
        self.assertEqual(desk_v[addr(1)], "asset:stable")
        bl = [r for r in rows if r["book"] == "baseline" and r["stage"] == "EXECUTE"]
        self.assertEqual([(r["action"], r["reason"]) for r in bl], [("SKIP", "no_eligible")])

    def test_weekly_refill_on_monday_both_books_after_the_sunday_snapshot(self):
        sun = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(test_tick, "T0", sun):
            self.old_state()
            for name in ("desk", baseline.NAME):
                b = Book(name, self.root)
                b.s["day"] = "2026-11-08"
                b.save()
            self.run_tick(0)       # Sun: the twin's restart (W45)
            self.run_tick(11 * 60)  # Sun 23:00: no weekly refill
            self.assertEqual([r["reason"] for r in self.refills()], ["restart"])
            self.run_tick(12 * 60 + 3)  # Mon 00:03: day roll, Sunday's report, then both refills
        rows = self.refills()
        self.assertEqual([(r["book"], r["reason"]) for r in rows],
                         [("baseline", "restart"), ("desk", "weekly"), ("baseline", "weekly")])
        self.assertIn("amount=2.39", rows[1]["detail"])
        self.assertIn("amount=0.00", rows[2]["detail"])
        self.assertIn("week=2026-W46", rows[1]["detail"])
        r = json.loads((self.root / report.REPORT).read_text())
        self.assertEqual(r["day"], "2026-11-08")
        self.assertEqual(r["desk"]["value_usd"], 47.61)    # Sunday's close, before Monday's refill
        self.assertEqual(r["desk"]["refills_usd"], 0.0)
        self.assertEqual(r["baseline"]["refills_usd"], 46.81)
        self.assertEqual(r["baseline"]["net_pnl_usd"], -46.81)  # the restart is never P&L
        self.assertEqual(r["desk_lead_usd"], round(-2.39 + 46.81, 2))
        self.assertEqual(Book("desk", self.root).cash, 50.0)
        self.assertAlmostEqual(Book("desk", self.root).s["refills_usd"], 2.39)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def trade(self, ts, book, side, address, usd, pnl="", reason="entry"):
        p = self.root / f"data/daily/{ts[:10]}/trades.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["ts", "book", "side", "address", "usd", "pnl_usd", "reason",
                                               "intended_price", "fill_price", "friction_pct"])
            if new:
                w.writeheader()
            w.writerow({"ts": ts, "book": book, "side": side, "address": address, "usd": usd, "pnl_usd": pnl,
                        "reason": reason, "intended_price": 1, "fill_price": 1, "friction_pct": 3})

    def test_per_trade_from_the_restart_only(self):
        self.trade("2026-11-05T10:00:00Z", "desk", "BUY", "0xold", 5)
        self.trade("2026-11-06T18:00:00Z", "desk", "SELL", "0xold", 4, -1.0, "time_stop")  # bought before: out
        self.trade("2026-11-06T18:05:00Z", "desk", "BUY", "0xa", 5)
        self.trade("2026-11-06T18:20:00Z", "desk", "SELL", "0xa", 2, 0.6, "ladder_1")
        self.trade("2026-11-06T19:00:00Z", "desk", "SELL", "0xa", 4, 0.4, "trail")
        self.trade("2026-11-06T19:05:00Z", "desk", "BUY", "0xb", 5)
        self.trade("2026-11-07T01:00:00Z", "desk", "SELL", "0xb", 3, -2.0, "hard_stop")
        self.trade("2026-11-06T19:10:00Z", "desk", "BUY", "0xopen", 5)
        rows = report.trades(self.root)
        since = "2026-11-06T17:00:00Z"
        self.assertEqual(report.per_trade(rows, "desk", since, "2026-11-06"),
                         {"since": since, "n": 1, "net_usd": 1.0, "net_per_trade_usd": 1.0, "net_per_trade_pct": 20.0})
        self.assertEqual(report.per_trade(rows, "desk", since, "2026-11-07"),
                         {"since": since, "n": 2, "net_usd": -1.0, "net_per_trade_usd": -0.5,
                          "net_per_trade_pct": -10.0})
        self.assertEqual(report.per_trade(rows, "desk", "", "2026-11-07")["n"], 0)  # before the restart: nothing
        self.assertEqual(report.per_trade(rows, "baseline", since, "2026-11-07")["n"], 0)

    def test_refills_never_pnl_and_gate_b_from_the_restart(self):
        now = datetime(2026, 11, 9, 0, 3, tzinfo=timezone.utc)
        books = [Book("desk", self.root), Book(baseline.NAME, self.root)]
        books[0].s.update(cash=50.0, refills_usd=2.39)
        books[1].s.update(cash=50.0, refills_usd=46.81)
        self.trade("2026-11-05T10:00:00Z", "desk", "BUY", "0xsi", 5)
        self.trade("2026-11-05T12:00:00Z", "desk", "SELL", "0xsi", 2.61, -2.39, "liq_pull")
        s = report.load(self.root, now)
        s.update(since="2026-10-29", per_trade_since="2026-11-06T17:00:00Z")
        r = report.build(self.root, now, "2026-11-09", s, books)
        self.assertEqual((r["desk"]["value_usd"], r["desk"]["net_pnl_usd"]), (50.0, -2.39))
        self.assertEqual(r["desk"]["multiple"], round(50 / 52.39, 4))
        self.assertEqual(r["baseline"]["net_pnl_usd"], -46.81)
        self.assertEqual(r["desk_lead_usd"], 44.42)
        self.assertEqual(r["gate_b"], {"since": "2026-11-06T17:00:00Z", "desk_closes": 0, "baseline_closes": 0,
                                       "target": 100, "kill": 200})
        self.assertEqual(r["desk"]["closes"], 1)  # SI stays in history
        self.assertIn("Gate B: 0/100", r["summary"])
        s["peak"], s["max_dd_pct"] = {}, {}
        report.track(s, books)
        self.assertAlmostEqual(s["max_dd_pct"]["desk"], 2.39 / 50 * 100)  # on value net of refills

    def test_old_equity_entries_have_no_refills(self):
        now = datetime(2026, 11, 7, 0, 3, tzinfo=timezone.utc)
        books = [Book("desk", self.root), Book(baseline.NAME, self.root)]
        books[1].s.update(cash=50.0, refills_usd=46.81)
        s = report.load(self.root, now)
        s["equity"] = {"desk": [{"day": "2026-11-06", "value_usd": 47.61, "cash_usd": 47.61, "open": 0}],
                       "baseline": [{"day": "2026-11-06", "value_usd": 3.19, "cash_usd": 3.19, "open": 0}]}
        r = report.build(self.root, now, "2026-11-06", s, books)
        self.assertEqual((r["baseline"]["refills_usd"], r["baseline"]["net_pnl_usd"]), (0.0, -46.81))
        report.on_roll(s, books, "2026-11-07")
        self.assertEqual(s["equity"]["baseline"][-1]["refills_usd"], 46.81)


if __name__ == "__main__":
    unittest.main()
