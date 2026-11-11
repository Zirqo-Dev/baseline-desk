"""Offline tests for BL-34 run hygiene and spec v3: the holdout refusals (candle files, candle lines to +72 h, ETH cover),
stale ETH = missing, the v3 entry fill (open within 5 min, else the last close; both books), tests 2 and 4 on the
close-only path, the always-reported extras, the v3 spec files, and two truth tests on made-up candles (a planted
edge passes the five tests; pure noise loses about the friction). Fixtures only, no network."""
import contextlib
import hashlib
import io
import json
import math
import random
import unittest
from pathlib import Path
from unittest import mock

from core.book import TASK_FIELDS
from tools import backtest as bt
from tools import candles as cdl
from tests.test_backtest import OUT_FIELDS, Root, a, c, keys, label, verify

REPO = Path(__file__).resolve().parent.parent
V3_JSON = REPO / "backtest/specs/bl34-survivor-momentum-v3.json"
V3_MD = REPO / "backtest/specs/bl34-survivor-momentum-v3.md"
V2_JSON = REPO / "backtest/specs/bl34-survivor-momentum-v2.json"
V2_MD = REPO / "backtest/specs/bl34-survivor-momentum-v2.md"
V3_SHA = "ae831edb992b33084fb1fc06cd6e63f89365462f7e22d64766021e9aa31ee913"
H = 3600
ts = cdl._ts


def spec_with(path, dev_last, hold, sha=b"fixture"):
    raw = json.loads(Path(path).read_text())
    raw["split"] = {"dev_last_day": dev_last, "holdout_days": hold}
    return bt.check_spec(dict(raw, sha256=hashlib.sha256(sha).hexdigest()))


def eth_file(root, day, price=2000.0, drop=()):
    s0 = ts(f"{day}T00:00:00Z")
    cdl.write(root / cdl.DIR / f"eth-{day}.jsonl.gz", {"day": day}, [
        {"address": cdl.WETH, "symbol": "WETH", "sight_ts": f"{day}T00:00:00Z", "pool": "0xeth", "match": "exact",
         "pool_created": None, "start": s0, "end": s0 + 86400,
         "candles": [c(s0 + k * 300, price, price, price, price) for k in range(288) if k not in drop]}])


def path_candles(t0, prices):
    """Candle j = from prices[j] to prices[j + 1] (o, max, min, c)."""
    return [c(t0 + j * 300, p, max(p, q), min(p, q), q) for j, (p, q) in enumerate(zip(prices, prices[1:]))]


DEV = ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03"]
HOLD = ["2026-11-04", "2026-11-05"]
SIGHT_DAYS = ["2026-11-03", "2026-11-04"]
J = 288  # 5-minute candles from the sighting to the +24 h decision
N_CANDLES = J + 74 * 12  # up to decision + 74 h: past the 72 h max hold


class World(Root):
    """Holdout decision days Nov 4-5: n tokens sighted every `step` minutes from Nov 3 00:00, each with its +24 h label
    30 s after sight + 24 h, real depth 20k, pair_s24 50, a single pool; candles from `prices(i)`; ETH flat."""

    def build(self, n, step_min, prices, gap=lambda i: False, eth_days=None, candle_days=SIGHT_DAYS, line_end=None):
        self.days(*(DEV + HOLD))
        base = ts("2026-11-03T00:00:00Z")
        tasks, labs, lines = {}, {}, {}
        self.toks = []
        for i in range(n):
            s = base + i * step_min * 60
            sight, lab = cdl._iso(s), cdl._iso(s + 86400 + 30)
            tasks.setdefault(sight[:10], []).append(verify(sight, a(i + 1), keys(qliq=20000), reason="goplus_timeout"))
            labs.setdefault(lab[:10], []).append(dict(label(lab, a(i + 1), sight, 24, qliq="20000"), pair_s24="50"))
            cs = path_candles(s, prices(i))
            if gap(i):  # no candle starting within 5 minutes of the decision: a last-close fill
                t = s + 86400 + 30
                cs = [x for x in cs if not t <= x[0] < t + 600]
            lines.setdefault(sight[:10], []).append(
                {"address": a(i + 1), "symbol": f"T{i}", "sight_ts": sight, "pool": f"0xp{i}", "match": "single",
                 "pool_created": cdl._iso(s - 3600), "start": s,
                 "end": s + (line_end if line_end is not None else 7 * 86400), "candles": cs})
            self.toks.append((a(i + 1), s + 86400 + 30))
        for d, rows in tasks.items():
            self.put(d, "tasks", TASK_FIELDS, rows)
        for d, rows in labs.items():
            self.put(d, "outcomes", OUT_FIELDS, rows)
        for d, ls in lines.items():
            if d in candle_days:
                cdl.write(self.root / cdl.DIR / f"{d}.jsonl.gz", {"day": d}, ls)
        for d in eth_days if eth_days is not None else ["2026-10-30"] + DEV + HOLD + ["2026-11-06", "2026-11-07",
                                                                                        "2026-11-08"]:
            eth_file(self.root, d)
        self.now = ts("2026-11-10T12:00:00Z")

    def spec(self, path=V3_JSON):
        return spec_with(path, "2026-11-03", HOLD)

    def holdout(self, spec=None):
        return bt.run_decision_spec(self.root, spec or self.spec(), holdout=True, now=self.now)


def winner_loser(i):
    """Even tokens rise into the decision and keep rising (x2.5 over 24 h, then flat); odd tokens fall before and after."""
    if i % 2 == 0:
        pre = [1 + 0.001 * j for j in range(J + 1)]
        return pre + [pre[-1] * (1 + 1.5 * min(m, 288) / 288) for m in range(1, N_CANDLES - J + 1)]
    pre = [1 - 0.0003 * j for j in range(J + 1)]
    return pre + [pre[-1] * max(0.01, 1 - 0.004 * m) for m in range(1, N_CANDLES - J + 1)]


def noise(i, sigma=0.002):
    rng = random.Random(1000 + i)
    p = [1.0]
    for _ in range(N_CANDLES):
        p.append(p[-1] * math.exp(sigma * rng.gauss(0, 1) - sigma * sigma / 2))  # a martingale: no drift
    return p


class TruthTest(World):
    def test_planted_edge_passes_the_five_tests(self):
        self.build(64, 45, winner_loser, gap=lambda i: i % 8 == 0)
        res = self.holdout()
        p = res["pass"]
        self.assertEqual(p["verdict"], "pass", p)
        self.assertTrue(all(p["tests"].values()))
        self.assertEqual(p["closed"], 32)
        self.assertEqual(p["paths"]["2_net_positive"], {"verdict": True, "close_only": True})
        self.assertEqual(p["paths"]["4_beats_hold_eth"], {"verdict": True, "close_only": True})
        self.assertGreater(p["mean_net"], p["twin_mean_net"])
        x = res["extra"]
        self.assertLess(x["expected_twin"]["mean_net"], p["mean_net"])
        self.assertEqual(x["twin_rank"]["draws"], 1000)
        self.assertLessEqual(x["twin_rank"]["rank"], 10)  # the planted edge sits at the top of the twin draws
        self.assertEqual(x["v2_no_fill"]["strategy"]["n"], 8)  # tokens 0, 8, ..., 56: last-close fills
        self.assertEqual(x["fill_age_min"]["strategy"]["n"], 8)
        self.assertAlmostEqual(x["fill_age_min"]["strategy"]["median"], 0.5)  # the candle ended 30 s before t
        text = bt.render_holdout(res)
        for want in ("verdict: pass", "close-only path", "expected twin", "rank among 1000", "v2 no_fill line",
                     "twin draws equal to the strategy's own token", "last-close fill age"):
            self.assertIn(want, text)

    def test_pure_noise_loses_about_the_friction(self):
        self.build(64, 45, noise)
        res = self.holdout()
        p = res["pass"]
        fees = bt.SpecFees(1, 20000)
        fill, _ = fees.buy(1.0, 5.0, 20000)
        out, _ = fees.sell(1.0, 5.0 / fill, 20000)
        round_trip = 5.0 / fill * out - 5.0  # a flat round trip: about -0.39 USD (8% friction)
        self.assertGreaterEqual(p["closed"], 20)
        self.assertAlmostEqual(p["mean_net"], round_trip, delta=0.12)
        self.assertAlmostEqual(p["twin_mean_net"], round_trip, delta=0.12)
        self.assertFalse(p["tests"]["2_net_positive"])
        self.assertEqual(p["verdict"], "retired")


class FillTest(unittest.TestCase):
    T = ts("2026-11-02T10:00:30Z")

    def sim(self, candles, entry_fill=bt.LAST_CLOSE, end=None):
        ex = bt.load_spec(V3_JSON)["exits"]
        return bt.simulate(candles, end or self.T + 80 * H, self.T, ex, 5.0, bt.FixedFees(0, 0), entry_fill=entry_fill)

    def test_last_close_when_no_candle_starts_within_5_minutes(self):
        t = self.T
        cs = [c(t - 930, 1.0, 1.1, 1.0, 1.1), c(t - 630, 1.1, 1.2, 1.1, 1.2), c(t + 570, 1.2, 1.2, 1.2, 1.2)]
        r = self.sim(cs)
        self.assertEqual((r["fill_how"], r["entry_t"], r["entry_price"], r["wait_s"]), ("last_close", t, 1.2, 0))
        self.assertEqual(r["last_age_s"], 330)  # the t - 630 candle ended 330 s before t
        r = self.sim([c(t + 60, 1.0, 1.0, 1.0, 1.0)] + cs[2:])
        self.assertEqual((r["fill_how"], r["entry_t"], r["entry_price"]), ("open", t + 60, 1.0))  # open rule first

    def test_no_fill_when_no_candle_ended_at_or_before_t(self):
        t = self.T
        self.assertEqual(self.sim([c(t + 570, 1.0, 1.0, 1.0, 1.0)])["status"], "no_fill")
        self.assertEqual(self.sim([c(t - 100, 1.0, 1.0, 1.0, 1.0), c(t + 570, 1.0, 1.0, 1.0, 1.0)])["status"],
                         "no_fill")  # a candle that started before t but ends after it is not a price at t

    def test_last_close_fill_never_checks_a_candle_that_started_before_t(self):
        t = self.T
        cs = [c(t - 930, 1.0, 1.0, 1.0, 1.0), c(t - 100, 1.0, 1.0, 0.5, 1.0), c(t + 570, 1.0, 1.0, 1.0, 1.0)]
        r = self.sim(cs)
        self.assertEqual(r["fill_how"], "last_close")
        self.assertEqual(r["exit_rule"], "max_hold")  # the t - 100 candle's low (-50%) is never checked
        self.assertTrue(all(s["t"] > t for s in r["sells"]))

    def test_open_fill_entry_candle_still_exits_on_that_candle(self):
        t = self.T
        cs = [c(t + 60, 1.0, 1.0, 0.5, 0.6), c(t + 360, 0.6, 0.6, 0.6, 0.6)]
        r3, r2 = self.sim(cs), self.sim(cs, entry_fill=None)
        self.assertEqual((r3["exit_rule"], r3["exit_t"]), ("hard_stop", t + 60))  # as in v2
        self.assertEqual(r3["sells"], r2["sells"])

    def test_v2_behaviour_unchanged_for_the_v2_spec(self):
        t = self.T
        cs = [c(t - 930, 1.0, 1.1, 1.0, 1.1), c(t - 630, 1.1, 1.2, 1.1, 1.2), c(t + 570, 1.2, 1.2, 1.2, 1.2)]
        r = self.sim(cs, entry_fill=None)
        self.assertEqual(r, {"status": "no_fill", "wait_s": 570})
        v2 = bt.load_spec(V2_JSON)
        self.assertNotIn("entry_fill", v2)
        ok = [c(t + 60, 1.0, 1.5, 1.0, 1.5), c(t + 360, 1.5, 2.1, 1.5, 2.0)]
        r = self.sim(ok, entry_fill=None, end=t + 660)
        self.assertNotIn("fill_how", r)
        self.assertEqual([s["rule"] for s in r["sells"]], ["ladder_1", "ladder_2"])
        with self.assertRaises(ValueError):
            bt.check_spec(dict(json.loads(V3_JSON.read_text()), entry_fill="open_or_whatever"))


class TwinSameRuleTest(World):
    def test_both_books_use_the_same_fill_function(self):
        self.build(16, 0, winner_loser, gap=lambda i: True)  # one decision time, every token gapped at it
        calls = []
        real = bt.simulate
        with mock.patch.object(bt, "simulate", lambda *a_, **k: calls.append(k.get("entry_fill")) or real(*a_, **k)):
            res = self.holdout()
        self.assertEqual(set(calls), {bt.LAST_CLOSE})
        tw = [x for x in res["trades"] if x["book"] == "twin" and x["status"] != "twin_skip"]
        mine = [x for x in res["trades"] if x["book"] != "twin"]
        self.assertTrue(tw and mine)
        self.assertEqual({x["fill_how"] for x in tw + mine}, {"last_close"})  # both books: the same rule
        v2 = bt.run_decision_spec(self.root, self.spec(V2_JSON), holdout=True, now=self.now)
        self.assertEqual({x["status"] for x in v2["trades"] if x["book"] != "twin"}, {"no_fill"})  # v2: dropped


class EthStaleTest(Root):
    def test_older_than_10_minutes_is_none(self):
        eth_file(self.root, "2026-11-02", drop=set(range(100, 110)))  # 50 minutes without an ETH candle
        eth = bt.EthSeries(self.root)
        s0 = ts("2026-11-02T00:00:00Z")
        self.assertEqual(eth.price(s0 + 100 * 300), 2000.0)  # the last candle ended right at t
        self.assertEqual(eth.price(s0 + 100 * 300 + 600), 2000.0)  # 10 minutes old: still a price
        self.assertIsNone(eth.price(s0 + 100 * 300 + 601))  # older than 10 minutes: missing
        self.assertIsNone(eth.ret(s0 + 104 * 300, 6))  # now is stale: no return, never 0.0%
        self.assertIsNone(eth.ret(s0 + 104 * 300 + 6 * H, 6))  # then is stale
        self.assertEqual(eth.ret(s0 + 200 * 300, 6), 0.0)
        self.assertIsNone(bt.EthSeries(self.root).price(s0 - 1))


class RefusalTest(World):
    def test_each_refusal_fires_and_clean_data_runs(self):
        self.build(8, 360, winner_loser)
        self.holdout()  # every file and line present: runs
        (self.root / cdl.DIR / "2026-11-04.jsonl.gz").unlink()
        with self.assertRaisesRegex(PermissionError, "no candle file for holdout sight day.*2026-11-04"):
            self.holdout()

    def test_short_candle_line_is_refused(self):
        self.build(8, 360, winner_loser, line_end=4 * 86400)  # lines end at sight + 96 h: short of t + 72 h + 5 min
        with self.assertRaisesRegex(PermissionError, r"candle line\(s\) end before decision \+ 72 h"):
            self.holdout()

    def test_eth_cover_is_refused(self):
        self.build(8, 360, winner_loser, eth_days=["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
                                                    "2026-11-07"])
        with self.assertRaisesRegex(PermissionError, "ETH series does not cover 2026-11-03 .. 2026-11-08: "
                                                     "missing eth-2026-11-08"):
            self.holdout()
        (self.root / cdl.DIR / "eth-2026-11-03.jsonl.gz").unlink()
        eth_file(self.root, "2026-11-08")
        with self.assertRaisesRegex(PermissionError, "missing eth-2026-11-03"):  # first holdout day - 24 h
            self.holdout()

    def test_safe_time(self):
        self.assertEqual(bt._iso(bt.safe_time(bt.load_spec(V3_JSON))), "2026-11-25T00:10:00Z")
        self.assertIn("earliest safe holdout run 2026-11-25T00:10:00Z", bt.safe_line(bt.load_spec(V3_JSON)))
        self.build(8, 360, winner_loser)
        with self.assertRaisesRegex(PermissionError, "earliest safe holdout run 2026-11-09T00:10:00Z"):
            bt.run_decision_spec(self.root, self.spec(), holdout=True, now=ts("2026-11-05T12:00:00Z"))

    def test_development_plumbing_prints_the_safe_time_and_no_pnl(self):
        self.build(8, 360, winner_loser)
        p = self.root / "spec.json"
        raw = json.loads(V3_JSON.read_text())
        raw["split"] = {"dev_last_day": "2026-11-03", "holdout_days": HOLD}
        p.write_text(json.dumps(raw))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(cdl, "_now", return_value=self.now):
            self.assertEqual(bt.main(["--spec", str(p), "--root", str(self.root)]), 0)
        text = out.getvalue()
        self.assertIn("earliest safe holdout run 2026-11-09T00:10:00Z", text)
        clean = text.replace("no P&L, no win rate, no pass tests", "")
        for bad in (r"\bnet\b", r"pnl", r"P&L", r"\bwins?\b", r"verdict", r"PASS", r"FAIL", r"USD"):
            self.assertNotRegex(clean, bad)


class BothPathsTest(unittest.TestCase):
    @staticmethod
    def mk(pnl, k, co=None, eth=0.0, co_ret=None, co_eth=0.0):
        co = pnl if co is None else co
        return {"status": "closed", "pnl": pnl, "ret": pnl / 5, "eth_ret": eth, "exit_t": k, "pnl_close_only": co,
                "ret_close_only": co / 5 if co_ret is None else co_ret, "eth_ret_close_only": co_eth}

    def test_a_close_only_failure_fails_the_test(self):
        v3, v2 = bt.load_spec(V3_JSON), bt.load_spec(V2_JSON)
        twin = [self.mk(0.1, k) for k in range(20)]
        good = [self.mk(0.5, k) for k in range(20)]
        self.assertEqual(bt.pass_tests(good, twin, v3)["verdict"], "pass")
        bad2 = [self.mk(0.5, k, co=-0.1, co_ret=0.5) for k in range(20)]  # wins on the candles, loses on closes
        r = bt.pass_tests(bad2, twin, v3)
        self.assertEqual(r["paths"]["2_net_positive"], {"verdict": True, "close_only": False})
        self.assertEqual((r["tests"]["2_net_positive"], r["verdict"]), (False, "retired"))
        self.assertEqual(bt.pass_tests(bad2, twin, v2)["verdict"], "pass")  # v2 never reads the close-only path
        bad4 = [self.mk(0.5, k, co_ret=0.01, co_eth=0.05) for k in range(20)]  # ETH beats it over its own windows
        r = bt.pass_tests(bad4, twin, v3)
        self.assertEqual(r["paths"]["4_beats_hold_eth"], {"verdict": True, "close_only": False})
        self.assertEqual(r["verdict"], "retired")
        unclosed = [dict(self.mk(0.5, k), pnl_close_only=None) if k == 0 else self.mk(0.5, k) for k in range(20)]
        self.assertFalse(bt.pass_tests(unclosed, twin, v3)["tests"]["2_net_positive"])  # must close on both paths

    def test_unpaired_cap_on_test_4(self):
        v3, v2 = bt.load_spec(V3_JSON), bt.load_spec(V2_JSON)
        twin = [self.mk(0.1, k) for k in range(10)]
        for n_unpaired, ok in ((1, True), (2, False)):  # 10% is allowed, more than 10% fails
            xs = [dict(self.mk(0.5, k), eth_ret=None) if k < n_unpaired else self.mk(0.5, k) for k in range(10)]
            r = bt.pass_tests(xs, twin, v3)
            self.assertEqual(r["unpaired"]["verdict"], n_unpaired)
            self.assertEqual(r["tests"]["4_beats_hold_eth"], ok, n_unpaired)
            self.assertTrue(bt.pass_tests(xs, twin, v2)["tests"]["4_beats_hold_eth"])  # v2: no cap
            co = [dict(self.mk(0.5, k), eth_ret_close_only=None) if k < n_unpaired else self.mk(0.5, k)
                  for k in range(10)]
            r = bt.pass_tests(co, twin, v3)
            self.assertEqual((r["unpaired"]["close_only"], r["paths"]["4_beats_hold_eth"]["close_only"]),
                             (n_unpaired, ok))  # the cap on the close-only path too
            self.assertEqual(r["tests"]["4_beats_hold_eth"], ok)


class EthEntryFillTest(World):
    def test_eth_missing_at_the_entry_fill_is_not_in(self):
        self.build(16, 0, winner_loser)  # one decision time, t = Nov 4 00:00:30; open fills at 00:04:30
        t = ts("2026-11-04T00:00:30Z")
        eth_file(self.root, "2026-11-03", drop={286, 287})  # no ETH candle from Nov 3 23:50 ...
        eth_file(self.root, "2026-11-04", drop=set(range(0, 3)))  # ... to Nov 4 00:15
        spec = self.spec()
        eth = bt.EthSeries(self.root)
        self.assertIsNone(eth.price(t + 270))  # at the entry fill (00:05): the last ETH candle ended 15 min before
        logs = bt.Logs(self.root, DEV + HOLD)
        cs = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        a0 = self.toks[0][0]
        f = bt.base_facts(logs, cs, a0, t)
        self.assertEqual(bt.sim_spec_trade("s", a0, t, f, spec, cs, eth)["status"], "no_eth")
        self.assertNotEqual(bt.sim_spec_trade("s", a0, t, f, self.spec(V2_JSON), cs, eth)["status"], "no_eth")


class SpecV3FilesTest(unittest.TestCase):
    def test_v3_is_v2_word_for_word_plus_the_listed_changes(self):
        v2 = V2_MD.read_text().splitlines()
        v3 = V3_MD.read_text().splitlines()
        new = {"BL-34 spec v3", "ENTRY FILL:", "ETH:", "CLOSE-ONLY PATH:", "TWIN:", "SPLIT:", "RUN:", "PASS (holdout",
               "Changes from v2:"}
        changed = [x for x in v3 if any(x.startswith(k) for k in new)]
        self.assertEqual(len(changed), len(new))
        kept = [x for x in v3 if x not in changed]
        self.assertEqual(kept, [x for x in v2 if not x.startswith(("BL-34 spec v2", "PASS (holdout", "TWIN:"))])
        text = V3_MD.read_text()
        for want in ("on the verdict path AND on the close-only path", "A test that fails on either path fails.",
                     "more than 10 minutes before it is needed is missing",
                     "test 4 FAILS if more than 10% of closed trades are unpaired (on either path)",
                     "refused unless the token candles and the ETH candles of every holdout decision reach decision + 72 h",
                     "the RUN coverage line", "decision days 2026-11-08 .. 2026-11-21",
                     "if it starts less than 300 seconds after t", "No candle that ended at or before t = no_fill",
                     "entered at t by the same ENTRY FILL rule, with the same exits, ticket and friction",
                     "the twin has no ETH entry condition", "is logged under reruns and never replaces the first result",
                     "On an open fill, exits are checked from the entry candle, as in v2",
                     "the v2 no_fill line", "the age of each last-close fill", "1,000 seeded twin draws"):
            self.assertIn(want, text)
        self.assertNotIn("fill-at-last-close line", text)

    def test_json_encodes_the_v3_text_and_old_specs_stay(self):
        raw = V3_JSON.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), V3_SHA)
        self.assertEqual(hashlib.sha256(V2_JSON.read_bytes()).hexdigest(),
                         "e9305116d1230e36319c54dd6cc62448e0d605205e39738d1e751d1a7b6f2e30")
        v3, v2 = json.loads(raw), json.loads(V2_JSON.read_text())
        self.assertEqual(v3["split"], {"dev_last_day": "2026-11-07", "holdout_days": ["2026-11-08", "2026-11-21"]})
        self.assertEqual(v3["entry_fill"], "open_within_5min_else_last_close")
        self.assertEqual((v3["eth_max_age_min"], v3["close_only_path"], v3["pass"]["both_paths"]), (10, True, [2, 4]))
        self.assertEqual(v3["report_extra"], {"expected_twin": True, "twin_rank_draws": 1000, "v2_no_fill": True,
                                              "twin_same_token": True, "fill_age": True})
        self.assertNotIn("fill_at_last_close", json.dumps(v3))
        same = ("decision", "ticket_usd", "universe", "match", "entry", "exits", "stop_fill", "friction", "twin",
                "hold_eth", "report_pool_age")
        self.assertEqual({k: v3[k] for k in same}, {k: v2[k] for k in same})
        self.assertEqual({k: v for k, v in v3["pass"].items() if k not in ("both_paths", "max_unpaired_pct")},
                         v2["pass"])
        self.assertEqual((v3["pass"]["max_unpaired_pct"], v3["eth_entry_fill_required"]), (10, True))
        self.assertEqual(bt.ETH_MAX_AGE_S, v3["eth_max_age_min"] * 60)
        notes = " ".join(v3["notes"])
        for want in ("entry_fill", "v2_no_fill", "fill_age", "both_paths", "eth_max_age_min", "close_only_path",
                     "max_unpaired_pct", "eth_entry_fill_required", "RUN"):
            self.assertIn(want, notes)
        spec = bt.load_spec(V3_JSON)
        self.assertEqual(bt.spec_days(spec), ("2026-11-07", [f"2026-11-{d:02d}" for d in range(8, 22)]))


if __name__ == "__main__":
    unittest.main()
