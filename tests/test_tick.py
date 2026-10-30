"""Offline tests for BL-7: quote(), the fallback note and core/tick.py. No network."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from adapters import base
from core import config, desk, exits, tick, verify
from core.book import Book
from core.leads import Leads
from tests.test_adapter_base import FRESH, OLDER, FakeClient, load
from tests.test_verify import GOOD

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
FRESH_PAIR = "0xd69e052b412840909dda5717dc3f7e0b5fa1a79dcaa1050717a6a51897707771"


def addr(i):
    return f"0x{i:040x}"


def good(i):
    return dict(GOOD, symbol=f"T{i}", pair=f"0x{i:064x}", goplus_pending=[], fetch_errors=[])


def tick_rows(root):
    out = []
    for p in sorted((root / "data/daily").glob("*/tasks.csv")):
        with p.open() as fh:
            out += list(csv.DictReader(fh))
    return out


def detail(row):
    return dict(kv.split("=", 1) for kv in row["detail"].split())


class QuoteTest(unittest.TestCase):
    def test_own_pool(self):
        c = FakeClient({"latest/dex/pairs": {"schemaVersion": "1.0.0", "pairs": load("dexscreener_fresh")}})
        q = base.quote(FRESH, FRESH_PAIR, c)
        self.assertEqual(q, {"price": 0.0002443, "liq_usd": 9493.9, "source": "pool"})
        self.assertEqual(len(c.calls), 1)
        self.assertIn(f"/latest/dex/pairs/base/{FRESH_PAIR}", c.calls[0][1])

    def test_pool_gone_falls_back_to_deepest_pair(self):
        c = FakeClient({"latest/dex/pairs": {"schemaVersion": "1.0.0", "pairs": None},
                        "token-pairs": load("dexscreener_fresh")})
        q = base.quote(FRESH, "0x" + "ab" * 32, c)
        self.assertEqual(q["source"], "fallback")
        self.assertEqual(q["price"], 0.0002443)
        self.assertEqual(len(c.calls), 2)

    def test_answer_for_another_pool_is_gone(self):
        c = FakeClient({"latest/dex/pairs": {"pairs": load("dexscreener_older")},
                        "token-pairs": load("dexscreener_fresh")})
        self.assertEqual(base.quote(FRESH, FRESH_PAIR, c)["source"], "fallback")

    def test_no_saved_pool_is_a_fallback(self):
        c = FakeClient({"token-pairs": load("dexscreener_fresh")})
        self.assertEqual(base.quote(FRESH, "", c)["source"], "fallback")

    def test_fetch_error_is_no_quote(self):
        self.assertIsNone(base.quote(FRESH, FRESH_PAIR, FakeClient({})))

    def test_missing_liquidity_stays_missing_no_fallback(self):
        pairs = load("dexscreener_fresh")
        del pairs[0]["liquidity"]
        c = FakeClient({"latest/dex/pairs": {"pairs": pairs}, "token-pairs": load("dexscreener_older")})
        q = base.quote(FRESH, FRESH_PAIR, c)
        self.assertNotIn("liq_usd", q)
        self.assertEqual((q["source"], len(c.calls)), ("pool", 1))

    def test_quote_side_token_is_inverted(self):
        p = load("dexscreener_older")[0]
        p["quoteToken"], p["baseToken"] = p["baseToken"], p["quoteToken"]
        p["priceUsd"], p["priceNative"] = "2000", "4"
        c = FakeClient({"latest/dex/pairs": {"pairs": [p]}})
        self.assertEqual(base.quote(OLDER, p["pairAddress"], c)["price"], 500)


class FallbackNoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book("desk", root=self.root)
        desk.buy(self.book, "0xa", dict(good(1), price=1.0, liq_usd=150_000), T0)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, name):
        with (self.root / f"data/daily/2026-10-23/{name}.csv").open() as fh:
            return list(csv.DictReader(fh))

    def test_buy_saves_the_pair(self):
        self.assertEqual(self.book.positions["0xa"]["pair"], f"0x{1:064x}")

    def test_hold_on_fallback_says_so(self):
        exits.check(self.book, lambda a: {"price": 1.0, "liq_usd": 150_000, "source": "fallback"},
                    T0 + timedelta(minutes=5))
        self.assertEqual(self.rows("tasks")[-1]["detail"], "pct=-3.85 quote=fallback")

    def test_sell_on_fallback_says_so_in_both_rows(self):
        exits.check(self.book, lambda a: {"price": 0.5, "liq_usd": 150_000, "source": "fallback"},
                    T0 + timedelta(minutes=5))
        self.assertTrue(self.rows("tasks")[-1]["detail"].endswith("quote=fallback"))
        sell = self.rows("trades")[-1]
        self.assertEqual((sell["side"], sell["reason"], sell["note"]), ("SELL", "hard_stop", "quote=fallback"))

    def test_own_pool_adds_nothing(self):
        exits.check(self.book, lambda a: {"price": 0.5, "liq_usd": 150_000, "source": "pool"},
                    T0 + timedelta(minutes=5))
        self.assertNotIn("fallback", self.rows("tasks")[-1]["detail"])
        self.assertEqual(self.rows("trades")[-1]["note"], "")


class TickTest(unittest.TestCase):
    """The adapter is replaced by fakes: scan() returns `found`, facts() returns `facts[address]`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.found, self.facts, self.quotes, self.order = [], {}, {}, []

    def tearDown(self):
        self.tmp.cleanup()

    def run_tick(self, minutes=0, **patches):
        c = FakeClient({})

        def scan(client):
            self.order.append("scan")
            client.calls += [("x", "scan")] * 3
            return [{"address": a, "symbol": "", "source": "t", "pool": ""} for a in self.found]

        def facts(address, now=None, client=None, goplus_ok=None, dex=None):
            # like the real facts(): DexScreener first, GoPlus only when the gate can't decide (BL-20)
            self.order.append(address)
            client.calls.append(("x", address))
            f = self.facts.get(address, dict(good(0), honeypot=True))  # unknown lead: needs GoPlus, then skip
            dex = dex if dex is not None else {}
            dex.update({k: f[k] for k in verify.DEX_FIELDS + ["pair", "launchpad", "trade"] if f.get(k) is not None},
                       fetch_errors=[e for e in f.get("fetch_errors", []) if e != "goplus"])
            dex = dict(f, **dex)
            if verify.dex_skip(dex):
                return dict(dex, goplus_skipped=True)
            if goplus_ok is not None and not goplus_ok():
                return None
            client.calls.append((base.GOPLUS, 200))
            return f

        def quote(address, pair="", client=None):
            self.order.append("quote:" + address)
            client.calls.append(("x", "quote"))
            return self.quotes.get(address)

        with mock.patch.object(base, "scan", scan), mock.patch.object(base, "facts", facts), \
                mock.patch.object(base, "quote", quote):
            for k, v in patches.items():
                stack = mock.patch.object(config, k, v)
                stack.start()
                self.addCleanup(stack.stop)
            return tick.run(self.root, T0 + timedelta(minutes=minutes), c, clock=lambda: 0.0, log=lambda s: None)

    def heartbeats(self):
        return [r for r in tick_rows(self.root) if r["stage"] == "TICK"]

    def test_one_heartbeat_per_tick(self):
        self.found = [addr(1)]
        self.run_tick(0)
        self.run_tick(5)
        hb = self.heartbeats()
        self.assertEqual(len(hb), 2)
        self.assertEqual((hb[0]["action"], hb[0]["reason"]), ("HEARTBEAT", ""))
        desk_keys = {"positions": "0", "seen": "1", "rechecks": "0", "new": "1",
                     "checked": "1", "passed": "0", "bought": "0", "held": "0",
                     "deferred": "0", "dex_only": "0", "goplus": "1", "requests": "5", "secs": "0.0"}
        # BL-8: the desk keys unchanged and first, the baseline keys appended after them
        self.assertEqual(list(detail(hb[0]))[:len(desk_keys)], list(desk_keys))
        self.assertEqual(detail(hb[0]), {**desk_keys, "bl_positions": "0", "bl_due": "0", "bl_eligible": "1",
                                         "bl_bought": "0", "report": "none", "machine": "none", "bl_error": "none",
                                         "labels": "none", "labels_due": "0", "labels_done": "0", "labels_calls": "0"})
        self.assertEqual(list(detail(hb[0]))[-5:], ["bl_error", "labels", "labels_due", "labels_done", "labels_calls"])
        self.assertEqual(detail(hb[1])["checked"], "0")  # the 24h skip holds: no re-check, no row

    def test_pass_buys_and_saves_pair(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        n = self.run_tick()
        self.assertEqual((n["passed"], n["bought"]), (1, 1))
        pos = Book("desk", self.root).positions[addr(1)]
        self.assertEqual(pos["pair"], f"0x{1:064x}")
        stages = [(r["stage"], r["action"]) for r in tick_rows(self.root)]
        self.assertEqual(stages, [("VERIFY", "PASS"), ("EXECUTE", "BUY"), ("TICK", "HEARTBEAT")])

    def test_exits_first_and_held_not_rechecked(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        self.run_tick(0)
        self.order.clear()
        self.quotes[addr(1)] = {"price": GOOD["price"], "liq_usd": GOOD["liq_usd"], "source": "pool"}
        n = self.run_tick(5)
        self.assertEqual(self.order, ["quote:" + addr(1), "scan"])  # exits before scan, no facts()
        self.assertEqual((n["positions"], n["held"], n["checked"]), (1, 1, 0))
        last = [(r["stage"], r["action"]) for r in tick_rows(self.root)][-2:]
        self.assertEqual(last, [("EXIT", "HOLD"), ("TICK", "HEARTBEAT")])

    def test_sold_this_tick_is_not_rebought_this_tick(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        self.run_tick(0)
        self.quotes[addr(1)] = {"price": GOOD["price"] / 2, "liq_usd": GOOD["liq_usd"], "source": "pool"}
        n = self.run_tick(5)
        self.assertEqual((n["held"], n["checked"], n["bought"]), (1, 0, 0))
        self.assertNotIn(addr(1), Book("desk", self.root).positions)

    def test_cap_order_watch_first_then_new(self):
        # two watched leads, due again at +15 min; the older one first
        pending = dict(good(0), goplus_pending=["mint_open"])
        del pending["mint_open"]
        leads = Leads(self.root)
        leads.record(addr(91), pending, T0 - timedelta(minutes=10))
        leads.record(addr(90), pending, T0 - timedelta(minutes=5))
        self.facts.update({addr(90): pending, addr(91): pending})
        self.found = [addr(i) for i in range(1, 26)]  # 25 new leads
        n = self.run_tick(15, GOPLUS_MAX_PER_TICK=99)  # MAX_CHECKS alone
        self.assertEqual(self.order[:3], ["scan", addr(91), addr(90)])
        self.assertEqual(self.order[3], addr(1))
        self.assertEqual((n["rechecks"], n["new"], n["checked"], n["deferred"]),
                         (2, 25, tick.MAX_CHECKS, 2 + 25 - tick.MAX_CHECKS))

    def test_watch_rechecked_even_when_scan_misses_it(self):
        pending = dict(good(0), goplus_pending=["mint_open"])
        del pending["mint_open"]
        Leads(self.root).record(addr(90), pending, T0)
        self.facts[addr(90)] = good(90)
        self.assertEqual(self.run_tick(10)["checked"], 0)  # not due before 15 min
        n = self.run_tick(15)
        self.assertEqual((n["seen"], n["rechecks"], n["passed"]), (0, 1, 1))

    def test_fetch_error_retried_next_tick_without_rediscovery(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = dict(good(1), fetch_errors=["goplus"])
        self.run_tick(0)
        self.found = []
        n = self.run_tick(5)
        self.assertEqual((n["rechecks"], n["checked"]), (1, 1))

    def test_goplus_cap_defers_the_10th_lead_with_no_row(self):
        self.found = [addr(i) for i in range(1, 13)]  # 12 new leads
        n = self.run_tick()
        self.assertEqual((n["checked"], n["deferred"], n["goplus"]), (9, 3, 9))
        verify = [r["address"] for r in tick_rows(self.root) if r["stage"] == "VERIFY"]
        self.assertEqual(verify, [addr(i) for i in range(1, 10)])
        self.assertEqual(detail(self.heartbeats()[0])["goplus"], "9")
        self.assertEqual(Leads(self.root).s["retry"], {})  # deferred is not a retry try
        n = self.run_tick(5)  # same queue order next tick, from the 10th lead on
        self.assertEqual(self.order[-3:], [addr(10), addr(11), addr(12)])
        self.assertEqual((n["checked"], n["deferred"]), (3, 0))

    def test_goplus_cap_counts_watch_rechecks_and_retries(self):
        pending = dict(good(0), goplus_pending=["mint_open"])
        del pending["mint_open"]
        leads = Leads(self.root)
        for i in range(90, 95):  # 5 watched, due at +15 min
            leads.record(addr(i), pending, T0 - timedelta(minutes=1))
        for i in range(80, 85):  # 5 fetch_error retries
            leads.record(addr(i), dict(good(i), fetch_errors=["goplus"]), T0 - timedelta(minutes=1))
        self.facts.update({addr(i): pending for i in range(90, 95)})
        self.facts.update({addr(i): dict(good(i), fetch_errors=["goplus"]) for i in range(80, 85)})
        self.found = [addr(1)]
        n = self.run_tick(15)
        self.assertEqual((n["rechecks"], n["new"], n["checked"], n["deferred"]), (10, 1, 9, 2))
        verify_rows = [r["address"] for r in tick_rows(self.root) if r["stage"] == "VERIFY"]
        self.assertNotIn(addr(1), verify_rows)  # deferred: the last retry and the new lead
        self.assertNotIn(addr(84), verify_rows[-9:])
        retries = Leads(self.root).s["retry"]
        self.assertEqual(retries[addr(84)]["tries"], 1)  # the deferred retry kept its try count

    def test_cap_then_continue_dex_only_leads_still_get_their_row(self):
        # BL-20: 10 leads need GoPlus, 5 low-liquidity leads after them settle on DexScreener alone
        self.found = [addr(i) for i in range(1, 16)]
        self.facts.update({addr(i): dict(good(i), liq_usd=1) for i in range(11, 16)})
        n = self.run_tick()
        self.assertEqual((n["checked"], n["deferred"], n["dex_only"], n["goplus"]), (14, 1, 5, 9))
        rows = [r for r in tick_rows(self.root) if r["stage"] == "VERIFY"]
        self.assertEqual([r["address"] for r in rows], [addr(i) for i in list(range(1, 10)) + list(range(11, 16))])
        self.assertTrue(all("goplus=skipped" in r["detail"] for r in rows[9:]))
        self.assertFalse(any("goplus=skipped" in r["detail"] for r in rows[:9]))
        self.assertEqual(Leads(self.root).s["retry"], {})  # the deferred lead is not a retry try
        hb = detail(self.heartbeats()[0])
        self.assertEqual((hb["dex_only"], hb["deferred"], hb["goplus"]), ("5", "1", "9"))
        n = self.run_tick(5)  # the deferred lead comes back next tick
        self.assertEqual((n["checked"], n["deferred"], n["goplus"]), (1, 0, 1))
        self.assertEqual([a for a in self.order if a.startswith("0x")][-1], addr(10))

    def test_cap_then_continue_keeps_queue_order(self):
        # a deferred watched lead stays first in line: watch, then retries, then new
        pending = dict(good(0), goplus_pending=["mint_open"])
        del pending["mint_open"]
        Leads(self.root).record(addr(90), pending, T0 - timedelta(minutes=1))
        self.facts[addr(90)] = pending
        self.found = [addr(i) for i in range(1, 12)]
        n = self.run_tick(15, GOPLUS_MAX_PER_TICK=0)  # no GoPlus at all this tick
        self.assertEqual(self.order[1:3], [addr(90), addr(1)])
        self.assertEqual((n["checked"], n["deferred"], n["dex_only"]), (0, 12, 0))
        self.assertEqual(Leads(self.root).s["watch"][addr(90)]["checks"], 1)  # untouched, still due
        self.order.clear()
        self.run_tick(20)
        self.assertEqual(self.order[1:3], [addr(90), addr(1)])

    def test_max_checks_still_bounds_dex_only_checks(self):
        self.found = [addr(i) for i in range(1, 26)]
        self.facts.update({addr(i): dict(good(i), liq_usd=1) for i in range(1, 26)})
        n = self.run_tick()
        self.assertEqual((n["checked"], n["dex_only"], n["deferred"], n["goplus"]), (tick.MAX_CHECKS, 20, 5, 0))

    def test_paused_runs_exits_but_no_scan(self):
        self.found = [addr(1)]
        n = self.run_tick(PAUSED=True)
        self.assertNotIn("scan", self.order)
        self.assertEqual(n["seen"], 0)
        self.assertEqual(self.heartbeats()[-1]["reason"], "paused")

    def test_crash_still_writes_heartbeat_then_raises(self):
        self.found = [addr(1)]
        self.facts[addr(1)] = good(1)
        with mock.patch.object(desk, "buy", side_effect=ZeroDivisionError("boom")):
            with self.assertRaises(ZeroDivisionError):
                self.run_tick()
        hb = self.heartbeats()
        self.assertEqual(len(hb), 1)
        self.assertEqual(hb[0]["reason"], "error:ZeroDivisionError")
        self.assertEqual(detail(hb[0])["passed"], "1")
        self.assertEqual(detail(hb[0])["requests"], "5")

    def test_broken_state_file_still_writes_heartbeat(self):
        (self.root / "data").mkdir()
        (self.root / "data/state.json").write_text("{not json")
        with self.assertRaises(ValueError):
            self.run_tick()
        self.assertEqual(self.heartbeats()[0]["reason"], "error:JSONDecodeError")


class TickWithFixturesTest(unittest.TestCase):
    """The real adapter on the BL-6 fixtures: request count and rows of one tick."""

    def test_fixture_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = FakeClient({"new_pools": load("geckoterminal_new_pools"), "token-profiles": [],
                            "token-boosts": [],
                            f"token-pairs/v1/base/{FRESH}": load("dexscreener_fresh"),
                            f"contract_addresses={FRESH}": load("goplus_fresh")})
            n = tick.run(root, datetime(2026, 10, 23, 11, 30, tzinfo=timezone.utc), c, clock=lambda: 0.0,
                         log=lambda s: None)
            # BL-20: 19 leads have no DexScreener route (fetch_error) and asci is under the liquidity
            # minimum, so the gate settles all 20 and GoPlus is never called
            self.assertEqual((n["seen"], n["checked"], n["deferred"], n["dex_only"], n["goplus"]),
                             (20, 20, 0, 20, 0))
            self.assertEqual(n["requests"], len(c.calls))
            self.assertFalse(any("gopluslabs" in url for _, url in c.calls))
            rows = tick_rows(root)
            self.assertEqual(rows[-1]["stage"], "TICK")
            asci = [r for r in rows if r["address"] == FRESH][0]
            self.assertEqual((asci["action"], asci["reason"]), ("SKIP", "liquidity"))
            self.assertIn("goplus=skipped", asci["detail"])


if __name__ == "__main__":
    unittest.main()
