"""Offline tests for BL-39 part 1: tools/liquid.py (liquid Base token survey). Fake client only, no network."""
import ast
import builtins
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import verify
from tools import candles
from tools import liquid as lq

CUT = lq.cut_ts("2026-11-08")
D = lq.DAY_S
OLD = "2025-01-01T00:00:00Z"   # established
YOUNG = "2026-10-20T00:00:00Z"  # created after 2026-10-09


def a(i):
    return f"0x{i:040x}"


WETH, USDC = candles.WETH, candles.USDC


def pool(addr, base, quote, created=OLD, reserve=1e6, dex="uniswap-v3-base"):
    return {"id": f"base_{addr}", "type": "pool",
            "attributes": {"address": addr, "pool_created_at": created, "reserve_in_usd": str(reserve),
                           "base_token_price_usd": "999", "volume_usd": {"h24": "1"}},
            "relationships": {"base_token": {"data": {"id": f"base_{base}"}},
                              "quote_token": {"data": {"id": f"base_{quote}"}},
                              "dex": {"data": {"id": dex}}}}


class Fake:
    """GeckoTerminal answers by path; records every path. 'refuse' = {path prefix: http code}."""

    def __init__(self, pools, symbols, daily, refuse=None):
        self.pools, self.symbols, self.dly, self.refuse = pools, symbols, daily, refuse or {}
        self.paths, self.calls, self.refused = [], 0, 0

    def get(self, path):
        self.paths.append(path)
        self.calls += 1
        for pre, code in self.refuse.items():
            if pre in path:
                raise candles.FetchError(f"http {code}")
        if path.startswith("/networks/base/pools?") or "/dexes/" in path:
            return {"data": self.pools if path.endswith("page=1") else []}
        if "/tokens/multi/" in path:
            toks = path.split("/tokens/multi/")[1].split("?")[0].split(",")
            data = []
            for t in toks:
                ids = [p["id"] for p in self.pools if t in (p["relationships"]["base_token"]["data"]["id"][5:],
                                                            p["relationships"]["quote_token"]["data"]["id"][5:])]
                data.append({"attributes": {"address": t, "symbol": self.symbols.get(t, "TOK"), "price_usd": "5"},
                             "relationships": {"top_pools": {"data": [{"id": i} for i in ids]}}})
            return {"data": data, "included": self.pools}
        if "/ohlcv/day" in path:
            p = path.split("/pools/")[1].split("/")[0]
            before = int(path.split("before_timestamp=")[1].split("&")[0])
            rows = [c for c in self.dly.get(p, []) if c[0] <= before]
            return {"data": {"attributes": {"ohlcv_list": sorted(rows, reverse=True)}}}
        raise AssertionError(path)


def days(n, close=1.0, vol=50_000.0, end=CUT):
    """n daily candles ending at `end` (the last one starts end - 1 d)."""
    return [[end - D * k, close, close, close, close, vol] for k in range(n, 0, -1)]


def world(tmp, launch=()):
    """Five tokens: T1 established, T2 established + wrapped by price, T3 young, USDC (stable), WETH (wrapped by
    price; a quote side), L1 a launch-universe token."""
    p1, p2, p3, p4, p5 = a(101), a(102), a(103), a(104), a(105)
    t1, t2, t3, l1 = a(1), a(2), a(3), a(9)
    pools = [pool(p1, t1, WETH), pool(p2, t2, USDC), pool(p3, t3, WETH, created=YOUNG),
             pool(p4, WETH, USDC, reserve=5e7), pool(p5, l1, WETH)]
    syms = {t1: "AAA", t2: "BIGX", t3: "NEW", WETH: "WETH", USDC: "USDC", l1: "LAUNCH"}
    dly = {p1: days(60) + [[CUT, 1, 1, 1, 1, 9.0], [CUT + D, 1, 1, 1, 1, 9.0]],  # two at/after CUT
           p2: days(40, close=3000.0), p4: days(183, close=3000.0)}
    root = Path(tmp)
    d = root / "data/daily/2026-10-25"
    d.mkdir(parents=True)
    (d / "tasks.csv").write_text("ts,book,stage,symbol,address,action,reason,detail\n"
                                 f"2026-10-25T00:03:00Z,desk,VERIFY,LAUNCH,{l1},SKIP,real_depth,x=1\n"
                                 + "".join(f"2026-10-25T00:03:00Z,desk,VERIFY,S,{x},SKIP,r,\n" for x in launch))
    late = root / "data/daily/2026-11-08"
    late.mkdir(parents=True)
    (late / "tasks.csv").write_text("ts,book,stage,symbol,address,action,reason,detail\n"
                                    f"2026-11-08T00:03:00Z,desk,VERIFY,AAA,{t1},SKIP,r,\n")
    return Fake(pools, syms, dly), root, (t1, t2, t3, l1)


class TestCut(unittest.TestCase):
    def test_every_stored_candle_ends_by_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, (t1, *_rest) = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            lq.write(root, meta, uni, lines)
            _, back = candles.read(root / lq.DAILY)
            ts = [c[0] for x in back for c in x["candles"]]
            self.assertTrue(ts)
            self.assertTrue(all(t < CUT and t + D <= CUT for t in ts))
            self.assertEqual(lq.max_ts(back), CUT - D)
            for p in fake.paths:
                if "before_timestamp=" in p:
                    self.assertLessEqual(int(p.split("before_timestamp=")[1].split("&")[0]), CUT)

    def test_keep_drops_the_cut_day_and_any_unfinished_day(self):
        self.assertTrue(lq.keep([CUT - D, 1, 1, 1, 1, 1], CUT))
        self.assertFalse(lq.keep([CUT, 1, 1, 1, 1, 1], CUT))
        self.assertFalse(lq.keep([CUT - D + 1, 1, 1, 1, 1, 1], CUT))

    def test_write_refuses_a_candle_at_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"cut": candles._iso(CUT)}
            with self.assertRaises(AssertionError):
                lq.write(tmp, meta, [], [{"token": a(1), "candles": [[CUT, 1, 1, 1, 1, 1]]}])
            self.assertFalse((Path(tmp) / lq.DAILY).exists())

    def test_list_prices_never_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, _ = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            lq.write(root, meta, uni, lines)
            text = (root / lq.UNIVERSE).read_text()
            self.assertNotIn("999", text)  # base_token_price_usd of the list answer
            self.assertNotIn("price", text)
            self.assertEqual(set(uni[0]), {"pool", "token", "symbol", "dex", "pool_created_at", "reserve_usd",
                                           "quote_side_usd", "asset_kind", "candle_error"})


class TestSet(unittest.TestCase):
    def test_asset_kind_is_cores(self):
        self.assertIs(lq.asset_kind, verify.asset_kind)
        self.assertNotIn("def asset_kind", Path(lq.__file__).read_text())

    def test_asset_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, (t1, t2, t3, l1) = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            self.assertEqual([x["token"] for x in uni], [t1])
            kinds = meta["counts"]["asset_excluded"]
            self.assertEqual(kinds.get("stable"), 1)      # USDC, by symbol
            self.assertEqual(kinds.get("wrapped"), 2)     # WETH and BIGX, by the last close >= 100 USD
            self.assertNotIn(t2, [x["token"] for x in lines])

    def test_launch_addresses_column_only_and_never_at_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, (t1, t2, t3, l1) = world(tmp, launch=[a(77)])
            opened = []
            real = builtins.open

            def spy(p, *k, **kw):
                opened.append(str(p))
                return real(p, *k, **kw)

            with mock.patch("builtins.open", spy):
                got = lq.launch_addresses(root, "2026-11-08")
            self.assertEqual(got, {l1, a(77)})
            self.assertTrue(opened)
            self.assertFalse(any("2026-11-08" in p or "2026-11-09" in p for p in opened))
            with self.assertRaises(ValueError):
                lq.launch_addresses(root, "2026-11-08", ("2026-11-07", "2026-11-08"))

    def test_launch_tokens_dropped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, (t1, t2, t3, l1) = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            self.assertEqual(meta["counts"]["launch_dropped"], 1)
            self.assertNotIn(l1, json.dumps(meta))
            self.assertFalse(any(l1 in p for p in fake.paths if "/tokens/multi/" in p))

    def test_young_pool_counted_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, (t1, t2, t3, l1) = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            self.assertEqual(meta["counts"]["young"], 1)
            self.assertNotIn(t3, [x["token"] for x in uni])
            self.assertFalse(any(a(103) in p for p in fake.paths if "/ohlcv/" in p))
            lq.write(root, meta, uni, lines)
            self.assertNotIn(t3, (root / lq.UNIVERSE).read_text())

    def test_deepest_pool_per_token(self):
        t = a(5)
        pools = [pool(a(201), t, WETH, reserve=1e5), pool(a(202), t, USDC, reserve=3e6)]
        got = lq.resolve_tokens(Fake(pools, {t: "X"}, {}), {t: {a(201)}}, {lq.pool_info(p)["pool"]: lq.pool_info(p)
                                                                           for p in pools}, [])
        self.assertEqual(got[t]["pool"], a(202))
        self.assertEqual((got[t]["base_pairs"], got[t]["quote_pairs"]), (2, 0))

    def test_budget_cap(self):
        api = lq.Budget(Fake([], {}, {}), max_calls=2)
        api.get("/networks/base/pools?sort=x&page=1")
        api.get("/networks/base/pools?sort=x&page=1")
        with self.assertRaises(lq.BudgetError):
            api.get("/networks/base/pools?sort=x&page=1")


class TestHistory(unittest.TestCase):
    def test_history_check_api_limit_max_two_attempts(self):
        class Refusing(candles.Client):
            pass
        attempts = []

        def urlopen(req, timeout):
            attempts.append(req.full_url)
            raise candles.urllib.error.HTTPError(req.full_url, 429, "x", {}, None)

        c = Refusing(urlopen=urlopen, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertTrue(lq.history_check(c, a(1), a(2), CUT).startswith("error:"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(candles.RETRIES, 5)  # restored
        self.assertEqual(lq.history_check(Fake([], {}, {}, refuse={"/ohlcv/": 401}), a(1), a(2), CUT), "api_limit")
        self.assertEqual(lq.history_check(Fake([], {}, {a(1): days(5, end=CUT - 300 * D)}), a(1), a(2), CUT),
                         "deeper")
        self.assertEqual(lq.history_check(Fake([], {}, {a(1): days(5)}), a(1), a(2), CUT), "api_limit")

    def test_history_counts(self):
        lines = [{"token": a(i), "candles": days(n)} for i, n in enumerate([20, 30, 95, 183, 183], 1)]
        u = {"cut": candles._iso(CUT), "counts": {}, "history_check": {"result": "api_limit"},
             "tokens": [{"token": a(i), "reserve_usd": 1e6, "pool_created_at": OLD} for i in range(1, 6)]}
        s = lq.survey(u, lines)
        self.assertEqual(s["history"]["span_ge"], {30: 4, 90: 3, 180: 2, 365: 0})
        self.assertEqual(s["history"]["candles_ge"], {30: 4, 90: 3, 180: 2, 365: 0})
        self.assertEqual(s["history"]["pool_age_ge_365"], 5)
        self.assertEqual(s["history"]["max_span_days"], 183)

    def test_thin_pool_span_vs_candles_and_window_start(self):
        """A thin old pool reaches further back with gaps: span counts calendar days, candles_ge the candles held;
        the window starts where the daily-trading old pools start, not at the thin pool's first candle."""
        thin = [c for k, c in enumerate(days(400)) if k % 3 == 0]  # 134 candles over 400 days
        lines = [{"token": a(1), "candles": thin}, {"token": a(2), "candles": days(183)}]
        u = {"cut": candles._iso(CUT), "counts": {}, "history_check": {},
             "tokens": [{"token": a(i), "reserve_usd": 1e6, "pool_created_at": OLD} for i in (1, 2)]}
        s = lq.survey(u, lines)
        self.assertEqual(s["history"]["span_ge"][365], 1)
        self.assertEqual(s["history"]["candles_ge"][365], 0)
        self.assertEqual(s["history"]["window_start"], lq._day(CUT - 183 * D))
        first = s["weeks"][0]
        self.assertGreaterEqual(lq.cut_ts(first["monday"]) - 7 * D, CUT - 183 * D)

    def test_gaps(self):
        c = days(10)
        del c[3]
        c[5][5] = 0.0
        u = {"cut": candles._iso(CUT), "counts": {}, "history_check": {},
             "tokens": [{"token": a(1), "reserve_usd": 1e6, "pool_created_at": OLD}]}
        s = lq.survey(u, [{"token": a(1), "candles": c}])
        self.assertEqual(s["gaps"]["missing_days_total"], 1)
        self.assertEqual(s["gaps"]["zero_volume_days_total"], 1)
        self.assertEqual(s["gaps"]["missing_buckets"]["1-5"], 1)


class TestWeeks(unittest.TestCase):
    def test_full_weeks_only_and_group_sizes(self):
        # 21 tokens with 60 days, volumes 1k .. 21k... per day x 10; one with 10 days only
        lines = [{"token": a(i), "candles": days(60, vol=1000.0 * i * 10)} for i in range(1, 22)]
        lines.append({"token": a(99), "candles": days(10, vol=500.0)})
        ws = lq.weekly(lq._vols(lines), CUT, CUT - 60 * D)
        mons = [w["monday"] for w in ws]
        self.assertEqual(mons[-1], "2026-11-02")          # the last Monday at or before CUT (a Sunday)
        start = CUT - 60 * D
        for w in ws:
            self.assertGreaterEqual(lq.cut_ts(w["monday"]) - 7 * D, start)
        last = ws[-1]
        self.assertEqual(last["universe"], 22)
        self.assertEqual(last["max_eligible"], 21)        # a(99) lacks the 31 closes
        self.assertEqual(last["max_group"], 3)            # ceil(2.1)
        self.assertEqual(last["bottom"]["n"], 3)          # ceil(2.2)
        self.assertEqual(last["bottom"]["median_vol_day"], 10_000.0)
        self.assertEqual(last["bottom"]["ge"], {10_000: 2, 100_000: 0})
        self.assertEqual(last["ge"][100_000], 12)         # 100k .. 210k
        early = [w for w in ws if not w["lookback_in_window"]]
        self.assertTrue(early)
        self.assertTrue(all(w["max_eligible"] is None and w["max_group"] is None for w in early))

    def test_survivorship_floor(self):
        lines = [{"token": a(1), "candles": days(60, vol=200_000.0)},
                 {"token": a(2), "candles": days(60, vol=200_000.0)[:-20]},  # stopped trading 20 days before CUT
                 {"token": a(3), "candles": days(60, vol=200_000.0)[:-10] + days(10, vol=10.0)}]
        u = {"cut": candles._iso(CUT), "counts": {}, "history_check": {},
             "tokens": [{"token": a(i), "reserve_usd": r, "pool_created_at": OLD}
                        for i, r in ((1, 5e6), (2, 1e3), (3, 5e5))]}
        s = lq.survey(u, lines)["survivorship"]
        b = s["by_threshold"][100_000]
        self.assertEqual((b["n_start"], b["still"], b["below"], b["no_trade_last_week"]), (3, 1, 1, 1))
        self.assertEqual(s["ever_ge_100k_reserve_lt_100k"], 1)
        self.assertEqual(s["stopped_7d_before_cut"], 1)


BANNED = {"pnl", "ret", "rets", "return", "returns", "chg", "change", "win", "wins", "winner", "profit", "gain",
          "gains", "fwd", "forward", "max_ret"}


def words(name):
    """The lower-case parts of an identifier or key (snake_case, digits apart)."""
    import re
    return set(re.findall(r"[a-z]+", name.lower()))


class TestNoReturns(unittest.TestCase):
    SRC = Path(lq.__file__).read_text()

    def test_price_columns_read_only_in_last_close(self):
        tree = ast.parse(self.SRC)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name == "last_close":
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value in (1, 2, 3, 4):
                    self.fail(f"{fn.name} reads candle index {n.slice.value}")
        last = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "last_close")
        self.assertNotIn("/", ast.unparse(last))

    def test_no_return_names(self):
        tree = ast.parse(self.SRC)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        bad = [x for x in names if words(x) & BANNED]
        self.assertEqual(bad, [])

    def test_survey_output_has_no_return_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, root, _ = world(tmp)
            meta, uni, lines = lq.fetch(fake, root, CUT)
            out = lq.public(lq.survey(dict(meta, tokens=uni), lines))
            found = set()

            def walk(x):
                if isinstance(x, dict):
                    for k, v in x.items():
                        found.update(words(str(k)))
                        walk(v)
                elif isinstance(x, list):
                    for v in x:
                        walk(v)
            walk(out)
            self.assertTrue(found)
            self.assertEqual(found & (BANNED | {"close", "price"}), set())

    def test_core_never_imports_liquid(self):
        for p in Path(lq.__file__).resolve().parents[1].joinpath("core").glob("*.py"):
            src = p.read_text()
            self.assertNotIn("tools.liquid", src, p.name)
            self.assertNotIn("backtest/liquid", src, p.name)


if __name__ == "__main__":
    unittest.main()
