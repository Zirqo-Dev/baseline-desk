"""Offline tests for BL-43 (display only): the site's refills, per-trade stats, real-depth labels, gate set, BL-34 card
and Backtest lab card; backtest/status.json (tools/candles.py write_status()); the lock's pass key; the Telegram daily
summary with and without refills. No network."""
import ast
import csv
import gzip
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from core import alerts, config, report, verify
from core.book import save_key
from tests.test_site import PAGE, has, reads, script
from tools import backtest as bt
from tools import candles as cdl

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "backtest/specs/bl34-survivor-momentum-v2.json"


def page():
    return PAGE.read_text(encoding="utf-8")


def fn(name):
    """Source of one top-level function of the page's script, up to the next blank line + comment or function."""
    return re.search(rf"  function {name}\(.*?\n  }}\n", script(), re.S).group(0)


class UntouchedTest(unittest.TestCase):
    """The owner's hard rules: the tick.log terminal and the Paper equity card are byte-for-byte as before BL-43, and
    nothing was removed or moved."""

    # sha256 of each segment in site/index.html as of 466419d (before BL-43); tests/test_bl43.py pins() recomputes
    PINS = {"term_html": "5126c9dc776f9a679c0f79d2f27cdf9f5aaa7da26aa1277f897b47177602932f",
            "equity_html": "1f76fa9549af9a7e427e9fd923e4b291510334dfdbebd710cd3b1b2f74f055e3",
            "term_js": "9beaa4ef85bc108d6c501b779a52496093412e8f5114e18db2f180f2b248a59d",
            "curve_js": "3c34fa88b3f4f4cc5a77fe8f1d6dcb0a47487d6174b53a20eb9a56a56cb9ff30",
            "equity_js": "eaa7453168dd2e85b0bc1022a11269909e42baaae32ad959a01684cc4d89eff2",
            "term_css": "10640ce3683945259b643197735f1cefe8d22de5facd951195fb730427a2c76a",
            "equity_css": "0cb6920ec475c0b658c85d7e9549bf6ff18cce94f3194d7e1bb31a0f9f7e7288"}
    SEG = {"term_html": (r'<section class="term"', r'</section>'),
           "equity_html": (r'<section class="card" aria-label="Paper equity">', r'</section>'),
           "term_js": (r"  function terminal\(\) \{", r"  /\* ---- money cards ---- \*/"),
           "curve_js": (r"  function curve\(", r"  function money\(r\) \{"),
           "equity_js": (r"    put\('equity',", r"    /\* Desk vs random"),
           "term_css": (r"/\* Terminal \*/", r"/\* Header \*/"),
           "equity_css": (r"/\* Equity \*/", r"/\* Desk vs random \*/")}

    def test_terminal_and_equity_card_unchanged(self):
        text = page()
        for k, (a, b) in self.SEG.items():
            seg = re.search(a + ".*?" + b, text, re.S).group(0)
            self.assertEqual(hashlib.sha256(seg.encode()).hexdigest(), self.PINS[k], k)

    def test_every_card_kept_in_order_new_cards_last(self):
        labels = re.findall(r'<section class="[^"]*" aria-label="([^"]+)"', page())
        self.assertEqual(labels, ["Baseline", "Last tick", "Paper equity", "Desk vs random", "Open now", "Gate B",
                                  "How trades ended", "BL-34 survivor momentum",  # end of the left column
                                  "Kill funnel", "Why they died", "Kill log", "Is it running",
                                  "Backtest lab",  # end of the right column
                                  "Tokens, last 24 h"])

    def test_chips_kept_gate_kills_before_safety(self):
        chips = re.findall(r'data-f="(\w+)" aria-pressed="\w+">([^<]+)<', page())
        self.assertEqual(chips, [("all", "All"), ("WATCH", "Watching"), ("LP", "LP unknown"), ("GATE", "Gate kills"),
                                 ("SAFETY", "Safety kills"), ("SKIP", "Skipped")])

    def test_token_chips_exact_markup_and_order(self):
        """The owner, Nov 7: exactly these six chips in this order; hiding, reordering, adding or removing one fails."""
        box = re.search(r'<div class="chips" id="chips">\n(.*?)\n      </div>', page(), re.S).group(1)
        want = [("all", "All", "true"), ("WATCH", "Watching", "false"), ("LP", "LP unknown", "false"),
                ("GATE", "Gate kills", "false"), ("SAFETY", "Safety kills", "false"), ("SKIP", "Skipped", "false")]
        self.assertEqual(box.split("\n"), [f'        <button type="button" class="chip" data-f="{f}" aria-pressed="{p}">{t}</button>'
                                           for f, t, p in want])
        self.assertEqual(page().count('class="chip"'), 6)
        js = script()
        for bad in [".chip').forEach(c => { c.hidden", "c.hidden", "chip').remove", ".chip { display: none"]:
            self.assertNotIn(bad, page(), bad)
        self.assertIn("document.querySelectorAll('.chip').forEach(c => { c.textContent += ' ' + fmt.format(counts[c.dataset.f]); });", js)
        self.assertEqual(set(re.findall(r"\b(all|WATCH|LP|GATE|SAFETY|SKIP): ", fn("tokens"))), {"all", "WATCH", "LP", "GATE", "SAFETY", "SKIP"})

    def test_desk_vs_random_rows_kept_refills_under_net(self):
        rows = re.findall(r"\['([A-Z][\w ]+)', o =>", script()) + re.findall(r"\['(Win rate|Stop slippage)', ", script())
        self.assertEqual(rows[:3], ["Net after fees", "Refills", "Closes"])
        for k in ["Net after fees", "Closes", "Win rate", "Max drawdown", "Stop slippage"]:
            self.assertIn(f"['{k}'", script())
        self.assertNotIn("Net after refills", page())  # no duplicate row (the owner, Nov 7)

    def test_funnel_gate_subtitle_only(self):
        js = script()
        self.assertIn("['gate', 'Gate', 'Real depth ≥ 10k, DexScreener data present', 'orange'],", js)
        for k in ["['scan', 'Scan', 'New Base pools, profiles, boosts', 'blue', 'tokens'],",
                  "['safety', 'Safety', 'GoPlus answered, every rule passed', 'purple'],",
                  "['buy', 'Buy', 'Paper fill after friction', 'green']];"]:
            self.assertIn(k, js)


class LabelAndGateTest(unittest.TestCase):
    def label_map(self):
        body = re.search(r"const LABEL = \{(.*?)\n  \};", script(), re.S).group(1)
        return dict(re.findall(r"'([^']+)': '([^']+)'", body))

    def test_real_depth_labels(self):
        m = self.label_map()
        self.assertEqual(m["real_depth"], "Real depth under 10k")
        self.assertEqual(m["missing:qliq"], "No real-depth number")
        # every BL-13 label kept
        for k in ["liquidity", "fetch_error_x3", "missing:symbol", "goplus_timeout", "too_old", "missing:lp_locked_pct"]:
            self.assertIn(k, m)

    def test_labels_only_in_why_and_kill_log_token_table_raw(self):
        js = script()
        self.assertIn("label(k)", fn("machine"))          # Why they died
        self.assertIn("label(KL(k, 'reason'))", fn("machine"))  # Kill log
        tokens = fn("tokens")
        self.assertNotIn("label(", tokens)  # the token table keeps raw reason codes (the owner, Nov 7)
        self.assertIn("TK(x, 'reason') || ''", tokens)
        self.assertIn("[TK(x, 'symbol'), TK(x, 'reason'), TK(x, 'address')]", tokens)  # search unchanged
        self.assertIn("const label = r => LABEL[r] || r;", js)

    def test_asset_prefix_is_a_gate_kill_like_core_report(self):
        js = script()
        self.assertIn("const GATE_PREFIX = 'asset:';", js)
        self.assertIn("GATE.has(r) || r.startsWith(GATE_PREFIX)", js)
        # every asset kind core/verify.py can give becomes "asset:<kind>", and core/report.py counts that row as a gate
        # kill (goplus=skipped); none is in GATE_KILLS, which is why the site needs the prefix rule
        for kind in ["stable", "wrapped", "quote"]:
            self.assertEqual(verify.dex_skip({**{k: 1 for k in verify.DEX_FIELDS}, "symbol": "X", "qliq": 1e6,
                                              "asset": kind}), f"asset:{kind}")
            row = {"reason": f"asset:{kind}", "detail": "goplus=skipped until=x"}
            self.assertEqual(report.kill_stage(row), "gate")
            self.assertNotIn(f"asset:{kind}", report.GATE_KILLS)

    def test_token_table_gate_and_safety_split(self):
        tokens = fn("tokens")
        self.assertIn("!isGate(TK(x, 'reason'))", tokens)
        self.assertIn("state.f === 'GATE' ? gateKill(x)", tokens)
        self.assertIn("GATE: list.filter(gateKill).length", tokens)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        c = self.root / cdl.DIR
        line = {"address": "0x1", "symbol": "A", "sight_ts": "2026-11-01T00:00:00Z", "pool": "p", "match": "single",
                "pool_created": None, "start": 0, "end": 1, "candles": [[0, 1, 1, 1, 1, 1]]}
        cdl.write(c / "2026-11-01.jsonl.gz", {"day": "2026-11-01", "fetched_at": "2026-11-09T01:00:00Z"},
                  [line, dict(line, address="0x2", candles=[])])
        cdl.write(c / "2026-11-02.open.jsonl.gz", {"day": "2026-11-02", "fetched_at": "2026-11-09T02:00:00Z"}, [line])
        cdl.write(c / "2026-11-01.open.jsonl.gz", {"day": "2026-11-01", "fetched_at": "2026-11-01T00:00:00Z"}, [])
        cdl.write(c / "eth-2026-10-31.jsonl.gz", {"day": "2026-10-31", "fetched_at": "2026-11-09T03:00:00Z"}, [line])
        cdl.write(c / "twin-validation.jsonl.gz", {"day": "x"}, [line])
        r = self.root / cdl.RESULTS / "2026-11-06T212642Z-twin-validation"
        r.mkdir(parents=True)
        (r / "summary.json").write_text(json.dumps({"mode": "validate-twin", "split": {"dev": ["2026-10-31", "2026-11-03"]}}))
        rows = [{"status": "closed", "logged_pnl": "-1.5", "pnl_logged_fric": "-2.0", "pnl_logged_fric_level": "-1.0", "pnl_core": "-2.5"},
                {"status": "closed", "logged_pnl": "0.5", "pnl_logged_fric": "0.25", "pnl_logged_fric_level": "0.5", "pnl_core": "0.1"},
                {"status": "no_fill", "logged_pnl": "-0.3", "pnl_logged_fric": "", "pnl_logged_fric_level": "", "pnl_core": ""},
                {"status": "no_data", "logged_pnl": "-5", "pnl_logged_fric": "", "pnl_logged_fric_level": "", "pnl_core": ""}]
        with (r / "trades.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_shape(self):
        s = cdl.status(self.root)
        self.assertEqual(s["token_days"], [
            {"day": "2026-11-01", "state": "final", "tokens": 2, "with_candles": 1, "fetched_at": "2026-11-09T01:00:00Z"},
            {"day": "2026-11-02", "state": "open", "tokens": 1, "with_candles": 1, "fetched_at": "2026-11-09T02:00:00Z"}])
        self.assertEqual(s["eth_days"], ["2026-10-31"])
        self.assertEqual(s["as_of"], "2026-11-09T03:00:00Z")  # the newest fetch: no change, no new file content
        tv = s["twin_validation"]
        self.assertEqual((tv["closed_both"], tv["logged_usd"], tv["simulated_usd"], tv["no_fill"], tv["tried"]),
                         (2, -1.0, -1.75, 1, 3))
        self.assertEqual(tv["days"], ["2026-10-31", "2026-11-03"])

    def test_write_status_is_stable_and_outside_data(self):
        p = cdl.write_status(self.root)
        self.assertEqual(p, self.root / "backtest/status.json")
        first = p.read_text()
        cdl.write_status(self.root)
        self.assertEqual(p.read_text(), first)
        self.assertFalse((self.root / "data").exists())

    def test_status_cli_writes_only_status(self):
        before = sorted(x.relative_to(self.root) for x in self.root.rglob("*"))
        self.assertEqual(cdl.main(["--status", "--root", str(self.root)]), 0)
        after = sorted(x.relative_to(self.root) for x in self.root.rglob("*"))
        self.assertEqual(sorted(set(after) - set(before)), [Path("backtest/status.json")])

    def test_empty_root(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cdl.status(d), {"as_of": None, "card": "BL-43", "token_days": [], "eth_days": [],
                                             "twin_validation": None})

    def test_repo_status_matches_the_validation_notes(self):
        s = json.loads((ROOT / cdl.STATUS).read_text())
        tv = s["twin_validation"]
        self.assertEqual((tv["closed_both"], tv["logged_usd"], tv["simulated_usd"]), (40, -28.53, -35.37))  # NOTES BL-33
        self.assertEqual(cdl.status(ROOT)["twin_validation"], tv)

    def test_site_reads_only_status_keys_that_exist(self):
        s = cdl.status(self.root)
        self.assertEqual(sorted(k for k in reads("ST") if not has(s, k)), [])
        for x in s["token_days"]:
            self.assertEqual(sorted(k for k in reads("SD") if not has(x, k)), [])
        self.assertTrue({"token_days", "eth_days", "twin_validation.logged_usd", "twin_validation.simulated_usd"} <= reads("ST"))

    def test_workflow_stages_status_with_candles(self):
        wf = (ROOT / ".github/workflows/backtest.yml").read_text()
        self.assertIn("git add -A backtest/candles/ backtest/status.json\n", wf)
        self.assertIn("git diff --cached --name-only | grep -v '^backtest/'", wf)


class Bl34CardTest(unittest.TestCase):
    def setUp(self):
        self.spec = bt.load_spec(str(SPEC))

    def lock(self, passed):
        with tempfile.TemporaryDirectory() as d:
            p = bt.write_lock(d, self.spec, ["2026-11-01"], ["2026-11-08"], 1794873600, {"bl34": {"n": 1}}, passed)
            return json.loads(p.read_text())

    def test_spec_keys_exist(self):
        raw = json.loads(SPEC.read_text())
        self.assertEqual(sorted(k for k in reads("SP") if not has(raw, k)), [])
        self.assertEqual(raw["split"]["holdout_days"], ["2026-11-08", "2026-11-14"])  # verdict ~Nov 17 = +3 days
        self.assertIn("const VERDICT_AFTER_D = 3;", script())

    def test_lock_keys_exist_and_tests_match_pass_tests(self):
        mk = lambda p, k: {"status": "closed", "pnl": p, "ret": p / 5, "eth_ret": 0.0, "exit_t": k}
        res = bt.pass_tests([mk(0.5, k) for k in range(25)], [mk(0.1, k) for k in range(20)], self.spec)
        lock = self.lock(res)
        self.assertEqual(lock["pass"]["verdict"], "pass")
        self.assertEqual(sorted(k for k in reads("LK") if not has(lock, k)), [])
        page_tests = re.findall(r"\['(\d_\w+)', ", fn("bl34") + script())
        self.assertEqual(set(page_tests), set(res["tests"]))
        self.assertEqual(self.lock(None)["pass"], None)  # a spec without pass tests: the card says so

    def test_never_trades_or_pnl(self):
        """Before the lock: waiting, no trade or P&L. After it: the tests and the verdict only, never trades or P&L."""
        src = fn("bl34")
        self.assertEqual(reads("LK"), {"pass.verdict", "ran_at", "pass", "spec"})
        code = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", src)  # code only, string literals emptied
        for bad in ["result", "mean_net", "pnl", "trades", "net_", "summary", "by_pool_age"]:
            self.assertNotIn(bad, code, bad)
        self.assertEqual(re.findall(r"\block\b[.\[]", code), [])  # the lock is read only through LK()
        self.assertIn("lockRes.status === 404", src)
        self.assertIn("'waiting'", src)
        self.assertIn("Sealed: no trades or P&L shown", src)

    def test_lock_pass_key_changes_no_number(self):
        a = self.lock(None)
        b = self.lock({"tests": {}, "verdict": "x"})
        self.assertEqual({k: v for k, v in a.items() if k != "pass"}, {k: v for k, v in b.items() if k != "pass"})


class NoBacktestInCoreTest(unittest.TestCase):
    def test_core_never_reads_backtest(self):
        """The owner, Nov 7: core/ never reads backtest/ (no exception for alerts). No core string literal names it
        (docstrings aside) and no core module imports tools."""
        for p in sorted((ROOT / "core").glob("*.py")):
            tree = ast.parse(p.read_text())
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    self.assertNotIn("backtest", n.value, p.name)
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    names = [n.module or ""] if isinstance(n, ast.ImportFrom) else [a.name for a in n.names]
                    self.assertFalse(any(x.startswith("tools") for x in names), p.name)
        self.assertNotIn("BL-34", (ROOT / "core/alerts.py").read_text().split('"""', 2)[2])


REP = {"day": "2026-11-09", "desk": {"value_usd": 52.5, "closes_day": 1,
                                     "per_trade": {"n": 3, "net_per_trade_usd": 0.4167, "net_per_trade_pct": 8.33}},
       "baseline": {"value_usd": 50.0, "closes_day": 2,
                    "per_trade": {"n": 0, "net_per_trade_usd": 0.0, "net_per_trade_pct": 0.0}},
       "desk_lead_usd": 1.25, "gate_b": {"since": "2026-11-06T20:33:15Z", "desk_closes": 3, "baseline_closes": 0,
                                         "target": 100, "kill": 200}}


class DailySummaryTest(unittest.TestCase):
    def test_without_refills(self):
        self.assertEqual(alerts.daily_text(REP, 288, 288, 0, {"desk": 0.0, "baseline": 0.0}),
                         "Baseline daily · 2026-11-09\n"
                         "desk $52.50 · random $50.00 · desk lead +$1.25\n"
                         "Gate B 3/100 desk closes\n"
                         "Gate B since 2026-11-06 20:33 UTC · random 0 closes · kill at 200\n"
                         "desk per trade since 2026-11-06 20:33 UTC: 3 closes · +$0.42 (+8.33%) each\n"
                         "random per trade since 2026-11-06 20:33 UTC: 0 closes · —\n"
                         "closes that day: desk 1 · random 2\n"
                         "runs 288/288 · ERROR rows 0")
        self.assertEqual(alerts.daily_text(REP, 288, 288, 0), alerts.daily_text(REP, 288, 288, 0, {}))

    def test_with_refills(self):
        lines = alerts.daily_text(REP, 288, 288, 0, {"desk": 2.39, "baseline": 46.81}).splitlines()
        self.assertEqual(lines[2], "desk refilled +$2.39 (not P&L) · random refilled +$46.81 (not P&L)")
        self.assertEqual(lines[3], "Gate B 3/100 desk closes")  # every BL-3 line stays
        only = alerts.daily_text(REP, 288, 288, 0, {"desk": 0.0, "baseline": 46.81}).splitlines()
        self.assertEqual(only[2], "random refilled +$46.81 (not P&L)")
        self.assertNotIn("refilled", alerts.daily_text(REP, 288, 288, 0, {"baseline": 0.004}))

    def test_bl3_lines_kept(self):
        text = alerts.daily_text(REP, 287, 288, 2, {"baseline": 46.81})
        for line in ["Baseline daily · 2026-11-09", "desk $52.50 · random $50.00 · desk lead +$1.25",
                     "Gate B 3/100 desk closes", "closes that day: desk 1 · random 2", "runs 287/288 · ERROR rows 2"]:
            self.assertIn(line, text.splitlines())
        self.assertNotIn("BL-34", text)  # dropped (the owner, Nov 7): the site's card shows it
        self.assertNotIn("sealed", text)

    def test_refilled_on_from_equity_entries(self):
        with tempfile.TemporaryDirectory() as d:
            save_key(Path(d) / config.STATE, "report", {"equity": {
                "baseline": [{"day": "2026-11-05", "value_usd": 3.19},  # before BL-31: no refills_usd = 0
                             {"day": "2026-11-06", "value_usd": 49.1, "refills_usd": 46.81},
                             {"day": "2026-11-07", "value_usd": 48.0, "refills_usd": 46.81},
                             {"day": "2026-11-09", "value_usd": 50.0, "refills_usd": 48.81}],
                "desk": [{"day": "2026-11-09", "value_usd": 50.0, "refills_usd": 2.39}]}})
            f = lambda day: {k: round(v, 2) for k, v in alerts.refilled_on(d, day).items()}
            self.assertEqual(f("2026-11-06"), {"baseline": 46.81})
            self.assertEqual(f("2026-11-07"), {"baseline": 0.0})
            self.assertEqual(f("2026-11-09"), {"baseline": 2.0, "desk": 2.39})  # the change vs the entry before
            self.assertEqual(f("2026-11-08"), {})
            (Path(d) / config.STATE).write_text("{broken")
            self.assertIsNone(alerts.refilled_on(d, "2026-11-09"))
            self.assertNotIn("refilled", alerts.daily_text(REP, 1, 288, 0, None))

    def test_per_trade_formats(self):
        self.assertEqual(alerts._per_trade({"n": 1, "net_per_trade_usd": -0.41, "net_per_trade_pct": -8.2}),
                         "1 close · -$0.41 (-8.20%) each")
        self.assertEqual(alerts._per_trade(None), "na")
        self.assertEqual(alerts._since(""), "na")


if __name__ == "__main__":
    unittest.main()
