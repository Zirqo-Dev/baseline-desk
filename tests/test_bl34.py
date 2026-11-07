"""Offline tests for BL-33 part 2: the BL-34 spec files, the decision-spec run (decision at the +24 h label, candle and
ETH returns, the twin rule, hold-ETH, the pass tests), the ETH fetch. Fixtures only, no network."""
import contextlib
import fractions
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import friction
from core.book import TASK_FIELDS
from tools import backtest as bt
from tools import candles as cdl
from tests.test_backtest import OUT_FIELDS, FakeHTTP, Clock, Root, a, c, keys, label, verify

REPO = Path(__file__).resolve().parent.parent
SPEC_JSON = REPO / "backtest/specs/bl34-survivor-momentum-v2.json"  # the spec the holdout run uses (v2 replaces v1)
SPEC_MD = REPO / "backtest/specs/bl34-survivor-momentum-v2.md"
V1_JSON = REPO / "backtest/specs/bl34-survivor-momentum-v1.json"
V1_MD = REPO / "backtest/specs/bl34-survivor-momentum-v1.md"
H = 3600


def ts(iso):
    return cdl._ts(iso)


class SpecFilesTest(unittest.TestCase):
    def test_json_encodes_the_frozen_text(self):
        spec = bt.load_spec(SPEC_JSON)
        self.assertEqual(spec["sha256"], hashlib.sha256(SPEC_JSON.read_bytes()).hexdigest())
        md = SPEC_MD.read_text()
        self.assertTrue(md.startswith("BL-34 spec v2: survivor momentum (approved by the owner 2026-11-06, frozen; "
                                      "replaces v1)"))
        e = spec["exits"]
        self.assertEqual((e["hard_stop_pct"], e["trail_pct"], e["max_hold_h"]), (-25, 25, 72))
        self.assertIsNone(e["time_stop_min"])  # NO 45-min time stop
        self.assertEqual([g for g, _ in e["ladder"]], [40, 100])
        raw = json.loads(SPEC_JSON.read_text())
        self.assertEqual([x for _, x in raw["exits"]["ladder"]], ["1/3", "1/3"])  # exact, not 0.33
        self.assertEqual([fractions.Fraction(x) for _, x in raw["exits"]["ladder"]], [fractions.Fraction(1, 3)] * 2)
        self.assertEqual(spec["ticket_usd"], 5)
        self.assertEqual(spec["friction"]["fee_pct"], 1)
        self.assertEqual(spec["twin"]["window_h"], 6)
        self.assertEqual(spec["pass"], {"min_closed": 20, "book_usd": 50, "max_dd_pct": 30})
        self.assertEqual(spec["split"], {"dev_last_day": "2026-11-07", "holdout_days": ["2026-11-08", "2026-11-14"]})
        self.assertEqual(spec["universe"], [["l24_gone", "==", 0], ["l24_depth", ">=", 10000],
                                            ["l24_pair_s24", ">=", 30]])  # v2: no verdict rule, no pool-age rule
        self.assertIn("NO pool-age rule: any pool age.", md)
        self.assertNotIn("verdict", md.split("UNIVERSE")[1].split("ENTRY")[0])
        self.assertEqual(spec["report_pool_age"], [["24-72 h", 0, 72], ["72 h-30 d", 72, 720], ["> 30 d", 720, None],
                                                   ["unknown", None, None]])
        self.assertIn("results by pool age at the decision (24-72 h, 72 h-30 d, > 30 d, unknown)", md)
        self.assertIn("Only 5 fails = not passed: drawdown", md)
        self.assertEqual(spec["entry"], [["ret_6h_pct", ">", 0], ["ret_6h_pct", ">", {"field": "eth_ret_6h_pct"}],
                                         ["eth_ret_24h_pct", ">=", -3]])
        self.assertEqual(spec["match"], ["exact", "single", "age"])
        for needle in ("-25%", "+40% sell 1/3", "+100% sell 1/3", "trail 25%", "max hold 72 h", "10,000 USD",
                       "pair_s24 >= 30", ">= -3%", "TICKET 5 USD", "1% pool fee", "(t - 6 h, t]", ">= 20 closed",
                       "50 USD book < 30%", "NO 45-min time stop", "worse of stop and close"):
            self.assertIn(needle, md, needle)
        self.assertEqual(spec["stop_fill"], "worse")
        self.assertEqual(raw["text"], "backtest/specs/bl34-survivor-momentum-v2.md")

    def test_v2_keeps_every_other_v1_number_and_v1_stays_as_written(self):
        v1, v2 = json.loads(V1_JSON.read_text()), json.loads(SPEC_JSON.read_text())
        for k in ("decision", "ticket_usd", "match", "entry", "exits", "stop_fill", "friction", "twin", "hold_eth",
                  "pass", "split"):
            self.assertEqual(v1[k], v2[k], k)
        self.assertEqual(v2["universe"], [u for u in v1["universe"] if u[0] != "verdict"])
        self.assertEqual(hashlib.sha256(V1_JSON.read_bytes()).hexdigest(),
                         "a0533763e3c3122d17894db851999d3d9e3f84742bbaa72b8348c3b9101e4e1f")  # v1 untouched
        self.assertTrue(V1_MD.read_text().startswith("BL-34 spec v1: survivor momentum"))

    def test_spec_split_and_default(self):
        spec = bt.load_spec(SPEC_JSON)
        last, hold = bt.spec_days(spec)
        self.assertEqual((last, hold[0], hold[-1], len(hold)), ("2026-11-07", "2026-11-08", "2026-11-14", 7))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for day in ("2026-10-31", "2026-11-01", "2026-11-02", "2026-11-07", "2026-11-08", "2026-11-09"):
                (root / f"data/daily/{day}").mkdir(parents=True)
                (root / f"data/daily/{day}/tasks.csv").write_text(",".join(TASK_FIELDS) + "\n")
            now = ts("2026-11-10T01:00:00Z")
            dev, h = bt.split(root, now, spec)
            self.assertEqual((dev, h), (["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-07"], hold))
            self.assertEqual(bt.split(root, now)[1], ["2026-11-08", "2026-11-09"])  # default: last third of 6 days
            self.assertIn("holdout days not complete yet: 2026-11-10", bt.holdout_refusal(root, spec, now))


class CandleReturnTest(unittest.TestCase):
    T = ts("2026-11-02T10:00:00Z")

    def test_definition_and_edges(self):
        t = self.T
        cs = [c(t - 6 * H - 300, 1.0, 1.0, 1.0, 1.0),  # ends exactly at t - 6 h: price then
              c(t - 3600, 1.1, 1.1, 1.1, 1.1),
              c(t - 300, 1.2, 1.2, 1.2, 1.25),         # ends exactly at t: counts as price now
              c(t, 9.0, 9.0, 9.0, 9.0)]                # ends after t: never read
        self.assertAlmostEqual(bt.candle_return(cs, t, 6), 25.0)
        self.assertEqual(bt.candle_return(cs, t, 6), bt.candle_return(cs[:3], t, 6))  # identical without later candles
        gap = [c(t - 8 * H, 1.0, 1.0, 1.0, 2.0)]  # no trade in the last 8 h: now = then = 2.0 -> 0%, fails "> 0"
        self.assertEqual(bt.candle_return(gap, t, 6), 0.0)
        self.assertIsNone(bt.candle_return([c(t - 3600, 1, 1, 1, 1)], t, 6))  # no candle ended by t - 6 h: missing
        self.assertIsNone(bt.candle_return([], t, 6))
        self.assertIsNone(bt.candle_return([c(t, 1, 1, 1, 1)], t, 6))  # only a later candle: missing
        self.assertFalse(bt.holds([["ret_6h_pct", ">", 0]], {"ret_6h_pct": 0.0}))
        self.assertFalse(bt.holds([["ret_6h_pct", ">", {"field": "eth_ret_6h_pct"}]], {"ret_6h_pct": 5.0}))
        self.assertTrue(bt.holds([["ret_6h_pct", ">", {"field": "eth_ret_6h_pct"}]],
                                 {"ret_6h_pct": 5.0, "eth_ret_6h_pct": 1.0}))
        self.assertTrue(bt.holds([["verdict", "not_in", ["too_young", "too_old"]]], {"verdict": "goplus_timeout"}))
        self.assertFalse(bt.holds([["verdict", "not_in", ["too_young", "too_old"]]], {"verdict": "too_old"}))


class FeeTest(unittest.TestCase):
    def test_fee_pct_never_stacks_with_the_clanker_fee(self):
        f = bt.SpecFees(1, 20_000)
        fill, pct = f.buy(1.0, 5.0, None)
        self.assertAlmostEqual(pct, 3.0 + 1.0 + 100 * 5 / 20_000)
        self.assertAlmostEqual(pct, friction.side_pct(5.0, 20_000, "other") + 1)
        _, spct = f.sell(2.0, 2.0, None)
        self.assertAlmostEqual(spct, 3.0 + 1.0 + 100 * 4 / 20_000)  # side USD = qty x quote, at the +24 h depth
        with self.assertRaises(ValueError):
            bt.SpecFees(1, 20_000, launchpad="clanker")  # both fees would apply: refused
        src = Path(bt.__file__).read_text()
        self.assertEqual(src.count("SpecFees(spec[\"friction\"][\"fee_pct\"], f.get(\"l24_depth\"))"), 1)  # no launchpad


class SimOffTest(unittest.TestCase):
    def test_time_stop_off_and_max_hold_72(self):
        t = ts("2026-11-02T10:00:00Z")
        spec = bt.load_spec(SPEC_JSON)
        flat = [c(t, 1.0, 1.0, 1.0, 1.0)] + [c(t + k * 3600, 1.0, 1.0, 1.0, 1.0) for k in range(1, 80)]
        r = bt.simulate(flat, t + 80 * H, t, spec["exits"], 5.0, bt.FixedFees(0, 0))
        self.assertEqual((r["exit_rule"], r["exit_t"]), ("max_hold", t + 72 * H))  # flat for 3 days: no time stop
        r = bt.simulate([c(t, 1.0, 1.5, 1.0, 1.5), c(t + 300, 1.5, 2.1, 1.5, 2.0)], t + 600, t, spec["exits"], 5.0,
                        bt.FixedFees(0, 0))
        self.assertEqual([s["rule"] for s in r["sells"]], ["ladder_1", "ladder_2"])
        self.assertAlmostEqual(r["sells"][0]["qty"], 5.0 / 3)  # exactly a third of the original quantity


class PassTest(unittest.TestCase):
    def test_verdicts_and_drawdown(self):
        spec = bt.load_spec(SPEC_JSON)
        mk = lambda pnl, k, eth=0.0: {"status": "closed", "pnl": pnl, "ret": pnl / 5, "eth_ret": eth, "exit_t": k}
        good = [mk(0.5, k) for k in range(20)]
        twin = [mk(0.1, k) for k in range(20)]
        self.assertEqual(bt.pass_tests(good, twin, spec)["verdict"], "pass")
        self.assertEqual(bt.pass_tests(good[:19], twin, spec)["verdict"], "inconclusive")
        self.assertEqual(bt.pass_tests(good, [mk(0.6, k) for k in range(20)], spec)["verdict"], "retired")  # twin wins
        self.assertEqual(bt.pass_tests([mk(0.5, k, eth=0.2) for k in range(20)], twin, spec)["verdict"], "retired")
        self.assertEqual(bt.pass_tests([mk(-0.1, k) for k in range(20)], twin, spec)["verdict"], "retired")
        crash = [mk(-3.2, k) for k in range(5)] + [mk(2.0, 5 + k) for k in range(15)]  # net + but -16 USD of 50 first
        r = bt.pass_tests(crash, twin, spec)
        self.assertAlmostEqual(r["max_dd_pct"], 32.0)
        self.assertEqual((r["tests"]["5_max_dd"], r["verdict"]), (False, "not passed: drawdown"))
        self.assertAlmostEqual(bt.max_dd_pct([1, -2, 1], 50), 2 / 51 * 100)


DEV = ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03"]
HOLD = ["2026-11-04", "2026-11-05"]


class DecisionRunTest(Root):
    """A small world: tokens 1-3 seen Nov 1, their +24 h labels Nov 2; token 9 a holdout-day token."""

    def spec(self):
        raw = json.loads(SPEC_JSON.read_text())
        raw["split"] = {"dev_last_day": "2026-11-03", "holdout_days": HOLD}
        return bt.check_spec(dict(raw, sha256=hashlib.sha256(b"fixture").hexdigest()))

    def setUp(self):
        super().setUp()
        self.days(*(DEV + HOLD))
        sight, lab = "2026-11-01T10:00:00Z", "2026-11-02T10:00:30Z"
        self.t = ts(lab)
        rows = [verify(sight, a(i), keys(qliq=20000), reason="goplus_timeout") for i in (1, 2, 3)]
        rows.append(verify(sight, a(4), keys(qliq=20000), reason="too_old"))
        self.put("2026-11-01", "tasks", TASK_FIELDS, rows)
        labs = [dict(label(lab, a(i), sight, 24, qliq="20000"), pair_s24="50") for i in (1, 2, 3, 4)]
        labs[2]["pair_s24"] = "5"  # token 3: too few sellers, never in the universe
        self.put("2026-11-02", "outcomes", OUT_FIELDS, labs)
        t0 = ts(sight)
        lines = []
        for i, slope in ((1, 0.02), (2, -0.01), (3, 0.05), (4, 0.05)):
            cs = [c(t0 + k * 300, 1 + slope * k / 12, 1 + slope * k / 12, 1 + slope * k / 12, 1 + slope * (k + 1) / 12)
                  for k in range(12 * 60)]
            lines.append({"address": a(i), "symbol": f"T{i}", "sight_ts": sight, "pool": f"0xp{i}", "match": "single",
                          "pool_created": "2026-11-01T06:00:00Z", "start": t0, "end": t0 + 7 * 86400, "candles": cs})
        cdl.write(self.root / cdl.DIR / "2026-11-01.jsonl.gz", {"day": "2026-11-01"}, lines)
        for d in ["2026-10-31"] + DEV[1:] + HOLD:
            s0 = ts(f"{d}T00:00:00Z")
            cdl.write(self.root / cdl.DIR / f"eth-{d}.jsonl.gz", {"day": d}, [
                {"address": cdl.WETH, "symbol": "WETH", "sight_ts": f"{d}T00:00:00Z", "pool": "0xeth", "match": "exact",
                 "pool_created": None, "start": s0, "end": s0 + 86400,
                 "candles": [c(s0 + k * 300, 2000.0, 2000.0, 2000.0, 2000.0) for k in range(288)]}])
        self.now = ts("2026-11-06T12:00:00Z")

    def test_decision_at_the_24h_label_universe_entry_twin(self):
        res = bt.run_decision_spec(self.root, self.spec(), now=self.now)
        mine = [t for t in res["trades"] if t["book"] != "twin"]
        # v2: 2 falls (ret <= 0), 3 thin sellers; 4 (desk verdict too_old) is in: no verdict rule any more
        self.assertEqual([t["address"] for t in mine], [a(1), a(4)])
        self.assertEqual(mine[0]["decision_t"], self.t)
        self.assertEqual(res["counts"]["universe"], 3)  # tokens 1, 2 and 4
        tw = [t for t in res["trades"] if t["book"] == "twin"]
        self.assertEqual(len(tw), 2)
        self.assertTrue(all(t["address"] in (a(1), a(2), a(4)) for t in tw))  # universe rules only
        self.assertEqual(tw[0]["decision_t"], self.t)
        self.assertEqual(res["split"]["dev"], DEV)
        self.assertIsNone(res["summary"])
        self.assertIsNone(res["pass"])

    def test_twin_pool_uses_only_what_is_known_at_t(self):
        spec = self.spec()
        logs = bt.Logs(self.root, DEV, bt._day_start(HOLD[0]))
        full = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        cut = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        for ls in cut.lines.values():
            for x in ls:
                x["candles"] = [k for k in x["candles"] if k[0] + 300 <= self.t]  # drop every candle after t
        empty = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        for ls in empty.lines.values():
            for x in ls:
                x["candles"] = []
        p = bt.twin_pool(logs, full, spec, self.t, 6)
        self.assertEqual(p, [a(1), a(2), a(4)])
        self.assertEqual(bt.twin_pool(logs, cut, spec, self.t, 6), p)
        self.assertEqual(bt.twin_pool(logs, empty, spec, self.t, 6), p)  # never candles
        self.assertEqual(bt.twin_pool(logs, full, spec, self.t + 6 * H, 6), [])  # (t - 6 h, t]: the edge is out
        self.assertEqual(bt.twin_pool(logs, full, spec, self.t + 6 * H - 1, 6), p)

    def test_twin_skip_counted_and_same_no_fill_rule(self):
        spec = self.spec()
        with mock.patch.object(bt, "twin_pool", return_value=[]):
            res = bt.run_decision_spec(self.root, spec, now=self.now)
        tw = [t for t in res["trades"] if t["book"] == "twin"]
        self.assertEqual([t["status"] for t in tw], ["twin_skip", "twin_skip"])
        self.assertEqual(res["plumbing"]["twin"]["twin_skip"], 2)
        calls = []
        real = bt.simulate
        with mock.patch.object(bt, "simulate", lambda *a_, **k: calls.append((a_[2], a_[4])) or real(*a_, **k)):
            bt.run_decision_spec(self.root, spec, now=self.now)
        self.assertTrue(calls)
        self.assertEqual({x for x in calls}, {(self.t, 5)})  # both books: entered at t through the same simulate()

    def test_leak_entry_fields_identical_without_later_candles(self):
        spec = self.spec()
        logs = bt.Logs(self.root, DEV, bt._day_start(HOLD[0]))
        cs = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        eth = bt.EthSeries(self.root)
        f = bt.entry_facts(bt.base_facts(logs, cs, a(1), self.t), cs, eth, a(1), self.t)
        for ls in cs.lines.values():
            for x in ls:
                x["candles"] = [k for k in x["candles"] if k[0] + 300 <= self.t]
        eth.candles = [k for k in eth.candles if k[0] + 300 <= self.t]
        eth.starts = [k[0] for k in eth.candles]
        g = bt.entry_facts(bt.base_facts(logs, cs, a(1), self.t), cs, eth, a(1), self.t)
        self.assertEqual(f, g)
        self.assertGreater(f["ret_6h_pct"], 0)
        self.assertEqual((f["eth_ret_6h_pct"], f["eth_ret_24h_pct"]), (0.0, 0.0))
        self.assertEqual(f["l24_depth"], 20000)
        self.assertAlmostEqual(f["pool_age_gt_h"], (self.t - ts("2026-11-01T06:00:00Z")) / 3600)

    def test_l24_depth_best_qliq_first(self):
        v = {"l24_best_qliq": 500.0, "l24_qliq": 20000.0}
        self.assertEqual(v.get("l24_best_qliq", v.get("l24_qliq")), 500.0)  # the rule as built in View.facts()
        logs = bt.Logs(self.root, DEV)
        self.assertEqual(logs.asof(self.t).facts(a(1))["l24_depth"], 20000)  # no best_qliq column: qliq

    def test_hold_eth_and_spec_fees_in_the_trade(self):
        res = bt.run_decision_spec(self.root, self.spec(), holdout=False, now=self.now)
        tr = [t for t in res["trades"] if t["book"] != "twin"][0]
        self.assertAlmostEqual(tr["entry_pct"], 3 + 1 + 100 * 5 / 20000)
        if tr["status"] == "closed":
            self.assertEqual(tr["eth_ret"], 0.0)  # flat ETH

    def test_plumbing_cli_prints_no_pnl_and_writes_nothing(self):
        p = self.root / "spec.json"
        raw = json.loads(SPEC_JSON.read_text())
        raw["split"] = {"dev_last_day": "2026-11-03", "holdout_days": HOLD}
        p.write_text(json.dumps(raw))
        before = sorted(str(x) for x in self.root.rglob("*"))
        out = io.StringIO()
        read = []
        realr = bt.replay._read
        with contextlib.redirect_stdout(out), mock.patch.object(cdl, "_now", return_value=self.now), \
                mock.patch.object(bt.replay, "_read", lambda q: read.append(str(q)) or realr(q)):
            code = bt.main(["--spec", str(p), "--write", "--root", str(self.root)])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("plumbing only", text)
        self.assertIn("entries 2", text)
        self.assertNotIn("by pool age", text)  # the age breakdown is a holdout-run line only
        clean = text.replace("no P&L, no win rate, no pass tests", "")
        for bad in (r"\bnet\b", r"pnl", r"P&L", r"\bwins?\b", r"verdict", r"PASS", r"FAIL", r"USD"):
            self.assertNotRegex(clean, bad)
        self.assertEqual(sorted(str(x) for x in self.root.rglob("*")), before)  # no results file
        self.assertFalse([q for q in read if any(h in q for h in HOLD)])  # no holdout day read

    def test_holdout_refused_until_its_days_are_complete(self):
        spec = self.spec()
        with self.assertRaises(PermissionError):
            bt.run_decision_spec(self.root, spec, holdout=True, now=ts("2026-11-05T12:00:00Z"))
        res = bt.run_decision_spec(self.root, spec, holdout=True, now=self.now)
        self.assertIsNotNone(res["pass"])
        self.assertEqual(res["pass"]["verdict"], "inconclusive")  # 0 closed on the holdout days
        self.assertIn("verdict: inconclusive", bt.render_holdout(res))
        self.assertEqual(res["by_pool_age"], {})  # no holdout trades in this world
        dev = bt.run_decision_spec(self.root, spec, now=self.now)
        self.assertIsNone(dev["by_pool_age"])  # development runs never compute it

    def test_pool_age_buckets(self):
        spec = self.spec()
        cs = bt.Candles(bt.candle_paths(self.root), None, spec["match"])
        t0 = ts("2026-11-01T06:00:00Z")  # every fixture pool's created time
        mk = lambda addr, hours, pnl: {"book": "x", "address": addr, "decision_t": t0 + int(hours * H),
                                       "status": "closed", "pnl": pnl}
        cs.lines[a(3)][0]["pool_created"] = "2026-09-20T06:00:00Z"  # a pool 42 days old at the decision
        rep = bt.age_report([mk(a(1), 30, 1.0), mk(a(2), 10, 3.0), mk(a(1), 100, -1.0), mk(a(3), 30, 2.0),
                             {"book": "x", "address": a(7), "decision_t": t0, "status": "no_fill", "pnl": None}],
                            cs, spec)["x"]
        self.assertEqual((rep["24-72 h"]["n"], rep["24-72 h"]["under_24h"], rep["24-72 h"]["mean_net"]), (2, 1, 2.0))
        self.assertEqual((rep["72 h-30 d"]["n"], rep["> 30 d"]["n"], rep["unknown"]["n"]), (1, 1, 1))
        self.assertEqual(rep["unknown"]["mean_net"], None)


class EthFetchTest(Root):
    def test_deepest_weth_usdc_pool_and_one_file_per_complete_day(self):
        pools = {"data": [
            {"attributes": {"address": "0xA", "reserve_in_usd": "5", "pool_created_at": "2023-01-01T00:00:00Z"},
             "relationships": {"base_token": {"data": {"id": f"base_{cdl.WETH}"}},
                               "quote_token": {"data": {"id": f"base_{cdl.USDC}"}}, "dex": {"data": {"id": "x"}}}},
            {"attributes": {"address": "0xB", "reserve_in_usd": "9", "pool_created_at": "2023-01-01T00:00:00Z"},
             "relationships": {"base_token": {"data": {"id": f"base_{cdl.WETH}"}},
                               "quote_token": {"data": {"id": f"base_{cdl.USDC}"}}, "dex": {"data": {"id": "x"}}}},
            {"attributes": {"address": "0xC", "reserve_in_usd": "99", "pool_created_at": "2023-01-01T00:00:00Z"},
             "relationships": {"base_token": {"data": {"id": f"base_{cdl.WETH}"}},
                               "quote_token": {"data": {"id": "base_0xdead"}}, "dex": {"data": {"id": "x"}}}}]}
        d0 = ts("2026-11-01T00:00:00Z")
        day = {"data": {"attributes": {"ohlcv_list": [c(d0 + 300 * k, 1, 1, 1, 1) for k in range(288)][::-1]}}}
        clock = Clock()
        http = FakeHTTP([pools, day])
        cl = cdl.Client(urlopen=http, sleep=clock.sleep, clock=clock)
        paths = cdl.fetch_eth(cl, self.root, ["2026-11-01", "2026-11-02"], now=ts("2026-11-02T12:00:00Z"))
        self.assertEqual([p.name for p in paths], ["eth-2026-11-01.jsonl.gz"])  # Nov 2 not complete: skipped
        _, lines = cdl.read(paths[0])
        self.assertEqual((lines[0]["pool"], len(lines[0]["candles"])), ("0xb", 288))  # the deepest USDC pool
        self.assertIn(f"token={cdl.WETH}", http.urls[1])
        self.assertEqual(cdl.eth_days(self.root, ts("2026-11-02T12:00:00Z")), ["2026-10-30", "2026-10-31"])
        self.assertNotIn(paths[0].name, [p.name for p in bt.candle_paths(self.root)])  # never a token file

    def test_exact_pool_gets_its_created_time_from_the_batch(self):
        multi = {"data": [{"attributes": {"address": a(1)}, "relationships": {"top_pools": {"data": [{"id": "p1"}]}}}],
                 "included": [{"id": "p1", "type": "pool", "attributes": {"address": "0xEXACT",
                               "pool_created_at": "2026-11-01T03:00:00Z", "reserve_in_usd": "1"},
                               "relationships": {"dex": {"data": {"id": "uniswap-v4-base"}}}}]}
        clock = Clock()
        cl = cdl.Client(urlopen=FakeHTTP([multi]), sleep=clock.sleep, clock=clock)
        out = cdl.resolve(cl, {a(1): {"dex": "uniswap:v4", "created": None}}, known={a(1): "0xexact"})
        self.assertEqual(out[a(1)], ("0xexact", "exact", ts("2026-11-01T03:00:00Z")))


class WorkflowTest(unittest.TestCase):
    def test_eth_step_and_docs_say_open_days_are_refetched(self):
        wf = (REPO / ".github/workflows/backtest.yml").read_text()
        self.assertIn("python -m tools.candles --eth", wf)
        self.assertIn("EVERY later", wf)
        self.assertEqual(wf.count("git add -A backtest/candles/"), 1)
        self.assertNotRegex(wf, r"(?m)^\s+schedule:")


if __name__ == "__main__":
    unittest.main()
