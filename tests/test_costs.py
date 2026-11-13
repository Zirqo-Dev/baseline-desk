"""BL-45 cost meter: read-only, percentages only, sealed from the launch universe, core blind to it."""
import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import costs as C

ROOT = Path(__file__).resolve().parents[1]
PROBE_SHA256 = "7b55d75b3339a657c36b36b7460b76756b156796d87f0e46387f7b18c28da8e7"  # docs/NOTES.md "BL-45"
Q96 = 1 << 96


def hexw(*vals):
    return "0x" + "".join(C._word(v) for v in vals)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeChain:
    """Answers eth_blockNumber, the probe's mids() and trip(): every pool at mid 1:1 (sqrtPriceX96 = 2^96 or equal
    reserves); a trip loses `fee` per side; per-token stage overrides; records every request."""

    def __init__(self, fee=0.003, stages=None, tax=None):
        self.fee, self.stages, self.tax = fee, stages or {}, tax or {}
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.requests.append(body)
        m, p = body["method"], body["params"]
        if m == "eth_blockNumber":
            return FakeResp(json.dumps({"result": "0x10"}).encode())
        assert m == "eth_call", m
        data = p[0]["data"][2:]
        sel, args = data[:8], C.words(data[8:])
        if sel == C.selector("mids(address[],uint8[])"):
            n = args[2]
            pairs = []
            for i in range(n):
                kind = args[3 + n + 1 + i]
                pairs += [Q96, 0] if kind == C.V3 else [10 ** 24, 10 ** 24]
            res = hexw(32, 2 * n, *pairs)
        elif sel == C.selector("trip(address,uint8,address,address,uint256)"):
            kind, other, amt = args[1], C.addr_of(args[3]), args[4]
            a, b = (Q96, 0) if kind == C.V3 else (10 ** 24, 10 ** 24)
            stage = self.stages.get(other, 0)
            pout_t = int(amt * (1 - self.fee))
            recv_t = int(pout_t * (1 - self.tax.get(other, 0.0)))
            pout_s = int(recv_t * (1 - self.fee))
            r = [a, b, amt, pout_t, recv_t, a, b, recv_t, recv_t, pout_s, pout_s]
            if stage == 2:
                r = [a, b] + [0] * 9
            if stage == 3:
                r = r[:7] + [recv_t, 0, 0, 0]
            res = hexw(stage, *r)
        else:
            raise AssertionError(sel)
        return FakeResp(json.dumps({"result": res}).encode())


def world(tmp, n_tokens=3):
    """A frozen tokens.json with one USDC-quoted v3 token, one WETH-quoted v2 token and a third via WETH."""
    root = Path(tmp)
    (root / "tools").mkdir()
    (root / C.PROBE_HEX).write_text("6080\n")
    usdc, weth = C.USDC, C.WETH
    toks = []
    for i in range(n_tokens):
        t = "0x" + f"{0xa0 + i:040x}"
        kind = C.V3 if i % 2 == 0 else C.V2
        quote = usdc if i == 0 else weth
        toks.append({"token": t, "symbol": f"T{i}", "pool": "0x" + f"{0xb0 + i:040x}", "dex": "uniswap-v3-base"
                     if kind == C.V3 else "aerodrome-base", "kind": kind, "token0": t, "fee_pct": 0.3,
                     "factory": "0x" + "f" * 40, "quote": quote, "hops": [] if quote == usdc else [weth]})
    doc = {"tokens": toks, "slots": {usdc: {"layout": "solidity:9", "key": "0x" + "1" * 64},
                                     weth: {"layout": "solidity:3", "key": "0x" + "2" * 64}},
           "hops": {weth: {"pool": "0x" + "c" * 40, "dex": "uniswap-v3-base", "kind": C.V3, "token0": weth,
                           "fee_pct": 0.05, "start": usdc, "symbol": "WETH"}}}
    (root / "backtest/costs").mkdir(parents=True)
    (root / C.TOKENS).write_text(json.dumps(doc))
    return root, doc


def rows_of(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fake_rpc(chain, cap=C.MAX_CALLS):
    return C.Rpc(urlopen=chain, sleep=lambda s: None, pace=0, max_calls=cap)


NOW = datetime(2026, 11, 13, 2, 17, tzinfo=timezone.utc)


class Keccak(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(C.keccak(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(C.keccak(b"abc").hex(), "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(len(C.keccak(b"x" * 300)), 32)  # multi-block

    def test_selectors_match_solc(self):
        # solc's methodIdentifiers for tools/costprobe.sol, and two ERC-20 views
        self.assertEqual(C.selector("trip(address,uint8,address,address,uint256)"), "b6aa0ebb")
        self.assertEqual(C.selector("mids(address[],uint8[])"), "2090afea")
        self.assertEqual(C.selector("uniswapV3SwapCallback(int256,int256,bytes)"), "fa461e33")
        self.assertEqual(C.selector("balanceOf(address)"), "70a08231")
        self.assertEqual(C.selector("transfer(address,uint256)"), "a9059cbb")


class Probe(unittest.TestCase):
    def test_hex_sha256_pinned(self):
        h = hashlib.sha256((ROOT / C.PROBE_HEX).read_bytes()).hexdigest()
        self.assertEqual(h, PROBE_SHA256)
        self.assertEqual(json.loads((ROOT / C.TOKENS).read_text())["probe"]["hex_sha256"], PROBE_SHA256)
        self.assertIn(PROBE_SHA256, (ROOT / "docs/NOTES.md").read_text())

    def test_solc_pinned(self):
        self.assertEqual(C.SOLC, "0.8.26")
        self.assertIn("pragma solidity 0.8.26;", (ROOT / C.PROBE_SOL).read_text())
        inp = C.probe_input(ROOT)
        self.assertEqual(inp["settings"]["optimizer"], {"enabled": True, "runs": 200})

    def test_probe_output_skips_notice(self):
        out = '>>> notice\n{"contracts":{"costprobe.sol":{"CostProbe":{"evm":{"deployedBytecode":{"object":"60ff"}}}}}}'
        self.assertEqual(C.probe_output(out), "60ff")


class Math(unittest.TestCase):
    def test_mid_orientation(self):
        # sqrtPriceX96 for token1/token0 = 4: start = token1 -> 4 start per other; start = token0 -> 0.25
        s = 2 * Q96
        self.assertAlmostEqual(C.mid_price(C.V3, s, 0, False), 4.0)
        self.assertAlmostEqual(C.mid_price(C.V3, s, 0, True), 0.25)
        self.assertAlmostEqual(C.mid_price(C.V2, 100, 400, False), 4.0)
        self.assertIsNone(C.mid_price(C.V3, 0, 0, True))

    def test_costs_held_round_trip(self):
        # mid 1:1, buy 1,000,000 -> 990,000 (1%), sell 990,000 -> 975,150 at mid 1 (1.5%)
        r = [Q96, 0, 1_000_000, 990_000, 990_000, Q96, 0, 990_000, 990_000, 975_150, 975_150]
        c = C.trip_costs(C.V3, True, r)
        self.assertEqual(c["buy_pct"], 1.0)
        self.assertEqual(c["sell_pct"], 1.5)
        self.assertEqual(c["rt_pct"], round(100 * (1 - 0.99 * 0.985), 4))  # chained sides, not recv/spent
        self.assertEqual((c["short_buy_pct"], c["short_sell_pct"]), (0.0, 0.0))

    def test_instant_flip_is_not_the_round_trip(self):
        # the sell gets the impact back (mid after the buy is higher): recv/spent would show ~0
        r = [Q96, 0, 1_000_000, 990_000, 990_000, int(Q96 * 0.99 ** -0.5), 0, 990_000, 990_000, 1_000_000,
             1_000_000]
        c = C.trip_costs(C.V3, False, r)
        self.assertGreater(c["rt_pct"], 0.9)

    def test_tax_like(self):
        r = [Q96, 0, 1_000_000, 990_000, 940_500, Q96, 0, 940_500, 940_500, 930_000, 930_000]  # 5% buy shortfall
        c = C.trip_costs(C.V3, True, r)
        self.assertEqual(c["short_buy_pct"], 5.0)
        self.assertEqual(C.verdict(0, c), ("fail", "tax_like"))
        c2 = dict(c, short_buy_pct=1.0)
        self.assertEqual(C.verdict(0, c2), ("pass", "pass"))  # 1.0 is not over the 1% line

    def test_stage_reasons(self):
        z = {"short_buy_pct": 0.0, "short_sell_pct": 0.0}
        self.assertEqual(C.verdict(1, z), ("fail", "no_route"))
        self.assertEqual(C.verdict(2, z), ("fail", "revert_buy"))
        self.assertEqual(C.verdict(3, z), ("fail", "revert_sell"))
        self.assertEqual(C.verdict(4, z), ("fail", "no_route"))

    def test_combine(self):
        self.assertEqual(C.combine([1.0, 1.0]), round(100 * (1 - 0.99 * 0.99), 4))
        self.assertIsNone(C.combine([1.0, None]))
        self.assertEqual(C.combine([0.5]), 0.5)

    def test_p90(self):
        self.assertEqual(C.p90(list(range(1, 11))), 9)
        self.assertEqual(C.p90([3]), 3)


class Run(unittest.TestCase):
    def test_run_rows_and_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, doc = world(tmp)
            out = Path(tmp) / "out"
            chain = FakeChain(fee=0.003)
            res = C.run(fake_rpc(chain), root, out, now=NOW)
            self.assertEqual(res["rows"], 2 + 3 * 2)  # WETH hop x 2 sizes + 3 tokens x 2 sizes
            self.assertEqual(res["calls"], 1 + 1 + 8)  # block, mids, 8 trips
            path = out / "2026-11-13.csv"
            with open(path, newline="") as f:
                rd = csv.DictReader(f)
                self.assertEqual(rd.fieldnames, C.COLUMNS)
                rows = list(rd)
            self.assertTrue(all(r["sim"] == "pass" for r in rows))
            t0 = [r for r in rows if r["leg"] == "token" and r["symbol"] == "T0"]
            self.assertEqual(t0[0]["usdc_rt_pct"], t0[0]["rt_pct"])  # USDC-quoted: no hop
            t1 = [r for r in rows if r["leg"] == "token" and r["symbol"] == "T1"][0]
            hop = [r for r in rows if r["leg"] == "hop" and r["size_usd"] == t1["size_usd"]][0]
            self.assertAlmostEqual(float(t1["usdc_rt_pct"]),
                                   C.combine([float(t1["rt_pct"]), float(hop["rt_pct"])]), places=4)
            self.assertAlmostEqual(float(t0[0]["buy_pct"]), 0.3, places=3)
            # every call is an eth_call or eth_blockNumber; every eth_call goes to the probe or a token view
            self.assertEqual({b["method"] for b in chain.requests}, {"eth_call", "eth_blockNumber"})
            for b in chain.requests:
                if b["method"] == "eth_call":
                    self.assertEqual(b["params"][0]["to"], C.PROBE)
                    self.assertEqual(b["params"][1], "0x10")  # pinned to the run's block
                    self.assertIn(C.PROBE, b["params"][2])
                    self.assertNotIn("value", b["params"][0])

    def test_trip_amounts_from_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, doc = world(tmp, n_tokens=1)
            chain = FakeChain()
            C.run(fake_rpc(chain), root, Path(tmp) / "out", now=NOW)
            amts = [C.words(b["params"][0]["data"][10:])[4] for b in chain.requests
                    if b["method"] == "eth_call" and b["params"][0]["data"][2:10] == C.selector(
                        "trip(address,uint8,address,address,uint256)")]
            self.assertEqual(amts, [5_000_000, 25_000_000])  # USDC raw units for 5 and 25 USD

    def test_failure_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, doc = world(tmp)
            t0, t1, t2 = (x["token"] for x in doc["tokens"])
            chain = FakeChain(stages={t0: 3, t1: 2}, tax={t2: 0.05})
            C.run(fake_rpc(chain), root, Path(tmp) / "out", now=NOW)
            rows = rows_of(Path(tmp) / "out/2026-11-13.csv")
            by = {r["symbol"]: r for r in rows if r["leg"] == "token"}
            self.assertEqual((by["T0"]["sim"], by["T0"]["reason"]), ("fail", "revert_sell"))
            self.assertNotEqual(by["T0"]["buy_pct"], "")
            self.assertEqual(by["T0"]["rt_pct"], "")
            self.assertEqual((by["T1"]["sim"], by["T1"]["reason"]), ("fail", "revert_buy"))
            self.assertEqual(by["T1"]["buy_pct"], "")
            self.assertEqual((by["T2"]["sim"], by["T2"]["reason"]), ("fail", "tax_like"))
            self.assertEqual(by["T2"]["usdc_rt_pct"], "")  # a failed leg gives no combined round trip

    def test_cap_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, doc = world(tmp, n_tokens=3)
            rpc = fake_rpc(FakeChain(), cap=5)
            res = C.run(rpc, root, Path(tmp) / "out", now=NOW)
            self.assertEqual(res["calls"], 5)
            self.assertTrue(res["capped"])
            rows = rows_of(Path(tmp) / "out/2026-11-13.csv")
            self.assertEqual(sum(r["reason"] == "cap" for r in rows), 8 - 3)
            self.assertTrue(all(r["sim"] == "error" for r in rows if r["reason"] == "cap"))
            self.assertEqual(C.MAX_CALLS, 100)

    def test_window_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(C.window_once(out, NOW))
            w = json.loads((out / "window.json").read_text())
            self.assertEqual((w["start"], w["end"]), ("2026-11-13T02:17:00Z", "2026-11-20T02:17:00Z"))
            self.assertFalse(C.window_once(out, datetime(2026, 11, 14, tzinfo=timezone.utc)))
            self.assertEqual(json.loads((out / "window.json").read_text()), w)

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, doc = world(tmp, n_tokens=1)
            out = Path(tmp) / "out"
            for h, stages in ((2, {}), (4, {}), (6, {doc["tokens"][0]["token"]: 3})):
                C.run(fake_rpc(FakeChain(stages=stages)), root, out, now=NOW.replace(hour=h))
            s = C.summary(out)
            self.assertEqual(s["runs"], 3)
            row = [r for r in s["table"] if r["leg"] == "token" and r["size_usd"] == 5][0]
            self.assertEqual((row["n"], row["errors"]), (3, 0))
            self.assertAlmostEqual(row["fail_rate"], 0.333, places=3)
            self.assertEqual(row["fail_reasons"], {"revert_sell": 1})
            self.assertIsNotNone(row["median_rt_pct"])
            self.assertIn("Evidence only", C.render_summary(s))
            # window: rows before the first run's start are left out unless --all
            later = C.summary(out, all_rows=True)
            self.assertEqual(later["rows"], s["rows"])


class StoresOnlyPercentages(unittest.TestCase):
    def test_columns(self):
        banned = re.compile(r"price|amount|qty|quantity|value|usd_value|balance|reserve|liq")
        for c in C.COLUMNS:
            if c == "size_usd":  # the ticket size (5 or 25), not a value of the token
                continue
            self.assertIsNone(banned.search(c), c)
        for c in ("buy_pct", "sell_pct", "rt_pct", "usdc_rt_pct", "short_buy_pct", "short_sell_pct", "sim",
                  "reason", "fee_pct", "pool", "source", "latency_ms", "ts", "token"):
            self.assertIn(c, C.COLUMNS)

    def test_location(self):
        self.assertEqual(C.DIR, "backtest/costs")
        self.assertFalse(C.DIR.startswith("data"))
        wf = (ROOT / ".github/workflows/costs.yml").read_text()
        self.assertIn("git add -A backtest/costs/", wf)
        adds = [ln.strip() for ln in wf.splitlines() if ln.strip().startswith("git add")]
        self.assertEqual(adds, ["git add -A backtest/costs/"])

    def test_committed_files_have_no_price(self):
        for p in (ROOT / C.DIR).glob("*.csv"):
            with open(p, newline="") as f:
                self.assertEqual(next(csv.reader(f)), C.COLUMNS)


class Isolation(unittest.TestCase):
    def test_core_never_imports_or_reads_costs(self):
        for p in (ROOT / "core").glob("*.py"):
            src = p.read_text()
            self.assertNotIn("tools.costs", src, p.name)
            self.assertNotIn("backtest/costs", src, p.name)
            self.assertNotIn("costprobe", src, p.name)

    def test_tick_workflow_untouched_by_costs(self):
        tick = (ROOT / ".github/workflows/tick.yml").read_text()
        self.assertNotIn("costs", tick)

    def test_workflow(self):
        wf = (ROOT / ".github/workflows/costs.yml").read_text()
        self.assertIn('cron: "17 */2 * * *"', wf)
        self.assertIn("workflow_dispatch:", wf)
        self.assertIn("group: costs", wf)
        self.assertIn("python -m tools.costs --run", wf)
        self.assertIn("git pull --rebase -q origin main && git push -q origin HEAD:main", wf)
        self.assertNotIn("secrets.", wf)
        self.assertNotIn("set -x", wf)
        self.assertIn("ABORT: something outside backtest/costs/ is staged", wf)


class Seal(unittest.TestCase):
    """Seal A: no launch-universe token is quoted before BL-34's holdout run."""

    def meter_addresses(self):
        doc = json.loads((ROOT / C.TOKENS).read_text())
        out = set()
        for x in doc["tokens"]:
            out |= {x["token"], x["pool"], x["quote"], *x["hops"]}
        for q, h in doc["hops"].items():
            out |= {q, h["pool"], h["start"]}
        out |= set(doc["slots"])
        return {a.lower() for a in out}

    def test_no_meter_address_in_tasks_csv(self):
        launch = C.launch_addresses(ROOT)  # the address column only
        self.assertTrue(launch)
        self.assertEqual(self.meter_addresses() & launch, set())

    def test_launch_addresses_reads_the_column_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data/daily/2026-11-12"
            d.mkdir(parents=True)
            (d / "tasks.csv").write_text("ts,book,stage,symbol,address,action,reason,detail\n"
                                         "t,desk,VERIFY,AAA,0xAbC1,SKIP,x,note 0xdef2\n")
            self.assertEqual(C.launch_addresses(tmp), {"0xabc1"})


class Frozen(unittest.TestCase):
    def test_tokens_json(self):
        doc = json.loads((ROOT / C.TOKENS).read_text())
        self.assertEqual(doc["card"], "BL-45")
        self.assertEqual(doc["counts"]["rule"], 39)
        self.assertEqual(doc["counts"]["metered"], 36)
        self.assertEqual(len(doc["tokens"]), 36)
        self.assertEqual(doc["counts"]["excluded_by_reason"], {"uniswap_v4": 3})
        self.assertEqual(doc["counts"]["rule"], doc["counts"]["metered"] + doc["counts"]["excluded"])
        self.assertEqual(doc["window"]["days"], 7)
        self.assertEqual(doc["sizes_usd"], [5, 25])
        for x in doc["tokens"]:
            self.assertIn(x["kind"], (C.V2, C.V3))
            self.assertIn(x["dex"], C.DEX_KIND)
            self.assertIn(x["quote"], doc["slots"])
            for c in x["hops"]:
                self.assertIn(doc["hops"][c]["start"], doc["slots"])
        self.assertEqual({e["reason"] for e in doc["excluded"]}, {"uniswap_v4"})
        self.assertTrue(all(e["dex"] == "uniswap-v4-base" for e in doc["excluded"]))

    def test_rule_reproduces_from_the_survey(self):
        cands, _ = C.rule_candidates(ROOT)
        doc = json.loads((ROOT / C.TOKENS).read_text())
        self.assertEqual(len(cands), doc["counts"]["rule"])
        self.assertEqual({x["token"] for x in cands},
                         {x["token"] for x in doc["tokens"]} | {x["token"] for x in doc["excluded"]})


class ReadOnly(unittest.TestCase):
    """No transaction, key or allowance path anywhere: whole words, code files only (*.py, *.sol, *.yml)."""

    def words(self):
        j = "".join
        return [j(["eth_", "send", "Transaction"]), j(["eth_", "send", "Raw", "Transaction"]),
                j(["eth_", "sign"]), j(["eth_", "sign", "Typed", "Data"]), j(["eth_", "sign", "Transaction"]),
                j(["personal_", "sign"]), j(["sign", "Transaction"]), j(["sign_", "transaction"]),
                j(["sign", "Message"]), j(["sign_", "message"]), j(["private", "_key"]), j(["private", "Key"]),
                j(["private", " key"]), j(["mne", "monic"]), j(["seed", "_phrase"]), j(["app", "rove"]),
                j(["send_", "raw", "_transaction"]), j(["key", "store"])]

    def test_forbidden_words(self):
        pat = re.compile(r"\b(" + "|".join(re.escape(w) for w in self.words()) + r")\b", re.IGNORECASE)
        files = [p for ext in ("*.py", "*.sol", "*.yml", "*.yaml") for p in ROOT.rglob(ext)
                 if ".git" not in p.parts]
        self.assertTrue(any(p.name == "costs.py" for p in files))
        self.assertTrue(any(p.name == "costprobe.sol" for p in files))
        hits = [f"{p.relative_to(ROOT)}:{i}" for p in files
                for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1) if pat.search(line)]
        self.assertEqual(hits, [])

    def test_whole_words_only(self):
        pat = re.compile(r"\b(" + "|".join(re.escape(w) for w in self.words()) + r")\b", re.IGNORECASE)
        self.assertIsNone(pat.search("design approved signal"))
        self.assertIsNotNone(pat.search("token." + "app" + "rove(x)"))

    def test_rpc_methods(self):
        src = (ROOT / "tools/costs.py").read_text()
        self.assertEqual(set(re.findall(r'call\("(\w+)"', src)), {"eth_call", "eth_blockNumber"})

    def test_no_secret_or_env_key(self):
        src = (ROOT / "tools/costs.py").read_text()
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)


if __name__ == "__main__":
    unittest.main()
