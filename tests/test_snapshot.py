"""Offline tests for BL-46: tools/snapshot.py (weekly point-in-time snapshot of liquid Base pools). Fake client only."""
import contextlib
import gzip
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from tools import candles
from tools import snapshot as S

ROOT = Path(__file__).resolve().parents[1]
NOW = candles._ts("2026-11-16T00:37:00Z")  # a Monday
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AAA = "0x" + "a" * 40
BBB = "0x" + "b" * 40
LAUNCH = "0x" + "c" * 40  # in a tasks.csv address column: sealed
NOTE = "0x" + "d" * 40    # only in a tasks.csv detail column: not sealed
TOPONLY = "0x" + "e" * 40
PRICE_MARK = 123.456789   # a price that must never reach the file
BANNED_KEY = re.compile(r"price|change|fdv|market_cap|marketcap|return|candle|ohlcv|pnl|transactions", re.I)


def pool_obj(addr, base, quote, dex="uniswap-v3-base", reserve="250000.5", vol="1000.25",
             created="2026-11-10T03:00:00.000Z"):
    return {"id": f"base_{addr}", "type": "pool",
            "attributes": {"address": addr, "name": "X / Y", "pool_created_at": created, "reserve_in_usd": reserve,
                           "volume_usd": {"m5": "1", "h1": "2", "h6": "3", "h24": vol},
                           "base_token_price_usd": str(PRICE_MARK), "quote_token_price_usd": str(PRICE_MARK),
                           "base_token_price_native_currency": "1", "base_token_price_quote_token": "1",
                           "quote_token_price_native_currency": "1", "quote_token_price_base_token": "1",
                           "fdv_usd": "999999", "market_cap_usd": "888888",
                           "price_change_percentage": {"h24": "55.5"}, "transactions": {"h24": {"buys": 3}}},
            "relationships": {"base_token": {"data": {"id": f"base_{base}"}},
                              "quote_token": {"data": {"id": f"base_{quote}"}},
                              "dex": {"data": {"id": dex}}}}


def tok_obj(addr, sym, price=PRICE_MARK, top=()):
    return {"id": f"base_{addr}", "type": "token",
            "attributes": {"address": addr, "symbol": sym, "name": sym, "price_usd": str(price),
                           "fdv_usd": "1", "market_cap_usd": "2", "volume_usd": {"h24": "3"}},
            "relationships": {"top_pools": {"data": [{"id": f"base_{p}", "type": "pool"} for p in top]}}}


P1 = "0x" + "1" * 40  # AAA / WETH, listed
P2 = "0x" + "2" * 40  # BBB / USDC, listed, young
P3 = "0x" + "3" * 40  # LAUNCH / WETH, listed: sealed
P4 = "0x" + "4" * 40  # AAA / TOPONLY, from top_pools only, deeper than P1
P5 = "0x" + "5" * 40  # NOTE / WETH, listed
P6 = "0x" + "6" * 40  # AAA / LAUNCH, from top_pools only: sealed, deepest of all
P7 = "0x" + "7" * 40  # LAUNCH / USDC, from LAUNCH's top pools only: sealed


class Fake:
    """candles.Client stand-in: same get() contract (body, None on 404, FetchError), calls / refused counters."""

    def __init__(self, fail=(), never_end=False):
        self.calls = self.refused = 0
        self.paths = []
        self.fail, self.never_end = fail, never_end

    def get(self, path):
        self.calls += 1
        self.paths.append(path)
        if any(f in path for f in self.fail):
            raise candles.FetchError("retries")
        if "/tokens/multi/" in path:
            asked = path.split("/tokens/multi/")[1].split("?")[0].split(",")
            data, inc = [], []
            for a in asked:
                if a == AAA:
                    data.append(tok_obj(AAA, "AAA", top=(P1, P4, P6)))
                    inc += [pool_obj(P4, AAA, TOPONLY, reserve="900000"),
                            pool_obj(P6, AAA, LAUNCH, reserve="5000000")]
                elif a == WETH:
                    data.append(tok_obj(WETH, "WETH", price=3000))
                elif a == USDC:
                    data.append(tok_obj(USDC, "USDC", price=1))
                elif a == BBB:
                    data.append(tok_obj(BBB, "BBB"))
                elif a == NOTE:
                    data.append(tok_obj(NOTE, "NOTE"))
                elif a == LAUNCH:
                    data.append(tok_obj(LAUNCH, "LNCH", price=0.5, top=(P3, P7)))
                    inc += [pool_obj(P7, LAUNCH, USDC, reserve="777777")]
            return {"data": data, "included": inc}
        if self.never_end:
            n = self.calls
            return {"data": [pool_obj("0x" + f"{n:040x}", AAA, WETH)] * 20}
        if "page=1&" in path and "/networks/base/pools?" in path:
            data = [pool_obj(P1, AAA, WETH), pool_obj(P2, BBB, USDC, created="2026-11-15T00:00:00Z"),
                    pool_obj(P3, LAUNCH, WETH), pool_obj(P5, NOTE, WETH)]
            inc = [tok_obj(AAA, "AAA"), tok_obj(WETH, "WETH"), tok_obj(BBB, "BBB"), tok_obj(USDC, "USDC"),
                   tok_obj(LAUNCH, "LNCH"), tok_obj(NOTE, "NOTE")]
            return {"data": data, "included": inc}
        if "/dexes/uniswap-v3-base/pools" in path and "page=1&" in path:
            return {"data": [pool_obj(P1, AAA, WETH)], "included": []}
        return {"data": []}


def quiet_main(*a, **k):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return S.main(*a, **k)


def make_root(tmp):
    d = Path(tmp) / "data/daily/2026-11-15"
    d.mkdir(parents=True)
    (d / "tasks.csv").write_text("ts,book,stage,symbol,address,action,reason,detail\n"
                                 f"t,desk,VERIFY,LNCH,{LAUNCH.upper().replace('0X', '0x')},SKIP,x,note {NOTE}\n")
    return Path(tmp)


def run_all(tmp, client=None, max_calls=S.MAX_CALLS):
    root = make_root(tmp) if not (Path(tmp) / "data").exists() else Path(tmp)
    api = S.Budget(client or Fake(), max_calls)
    return S.snapshot(api, root, NOW), api


def run(tmp, client=None, max_calls=S.MAX_CALLS):
    """The main file only, as (doc, complete)."""
    (doc, _sealed, complete), api = run_all(tmp, client, max_calls)
    return (doc, complete), api


def read_fake_sealed(p):
    """Tests only, fake data in a temp folder: S.read() refuses sealed files by design."""
    return json.loads(gzip.decompress(Path(p).read_bytes()))


def walk_keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk_keys(v)


class Fields(unittest.TestCase):
    def test_no_price_change_fdv_cap_or_return_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, complete), _ = run(tmp)
            self.assertTrue(complete)
            bad = [k for k in walk_keys(doc["pools"]) if BANNED_KEY.search(k)]
            bad += [k for k in walk_keys(doc["tokens"]) if BANNED_KEY.search(k)]
            self.assertEqual(bad, [])
            text = json.dumps({"pools": doc["pools"], "tokens": doc["tokens"]})
            for v in (str(PRICE_MARK), "999999", "888888", "55.5"):
                self.assertNotIn(v, text)
            for p in doc["pools"]:
                self.assertEqual(tuple(p), S.POOL_KEYS)
            for t in doc["tokens"]:
                self.assertEqual(tuple(t), S.TOKEN_KEYS)

    def test_written_file_has_no_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            self.assertEqual(quiet_main(["--write", "--root", tmp, "--out", str(out)], client=Fake(), now=NOW), 0)
            p = out / "2026-11-16.json.gz"
            raw = p.read_bytes()
            doc = S.read(p)
            self.assertEqual(sorted(doc), ["meta", "pools", "tokens"])
            body = json.dumps({"pools": doc["pools"], "tokens": doc["tokens"]})
            self.assertNotIn(str(PRICE_MARK), body)
            self.assertEqual(raw[4:8], b"\0\0\0\0")  # gzip mtime 0

    def test_pool_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            p1 = next(p for p in doc["pools"] if p["pool"] == P1)
            self.assertEqual(p1["dex"], "uniswap-v3-base")
            self.assertEqual((p1["base"], p1["quote"]), (AAA, WETH))
            self.assertEqual((p1["base_symbol"], p1["quote_symbol"]), ("AAA", "WETH"))
            self.assertEqual(p1["pool_created_at"], "2026-11-10T03:00:00Z")
            self.assertEqual(p1["reserve_usd"], 250000.5)
            self.assertEqual(p1["volume_h24_usd"], 1000.25)
            self.assertEqual(p1["lists"], ["dex:uniswap-v3-base", "pools:h24_tx_count_desc",
                                           "pools:h24_volume_usd_desc"])
            p4 = next(p for p in doc["pools"] if p["pool"] == P4)
            self.assertEqual(p4["lists"], [S.TOP_POOLS])
            m = doc["meta"]
            for k in ("as_of", "source", "params", "calls", "counts", "complete", "seal"):
                self.assertIn(k, m)
            self.assertEqual(m["as_of"], "2026-11-16T00:37:00Z")
            self.assertEqual(m["params"]["sorts"], ["h24_volume_usd_desc", "h24_tx_count_desc"])

    def test_young_pools_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            p2 = next(p for p in doc["pools"] if p["pool"] == P2)
            self.assertEqual(p2["pool_created_at"], "2026-11-15T00:00:00Z")
            self.assertIn(BBB, {t["token"] for t in doc["tokens"]})

    def test_asset_kind_price_in_memory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            kinds = {t["token"]: t["asset_kind"] for t in doc["tokens"]}
            self.assertEqual(kinds[USDC], "stable")
            self.assertEqual(kinds[WETH], "wrapped")  # by the in-memory price (3000 >= 100) or the symbol
            self.assertEqual(kinds[AAA], "wrapped")   # only the in-memory price (123.45) makes AAA "wrapped"
            self.assertNotIn("3000", json.dumps(doc["tokens"]))

    def test_deepest_pool_among_unsealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            aaa = next(t for t in doc["tokens"] if t["token"] == AAA)
            self.assertEqual(aaa["pool"], P4)  # P6 is deeper but sealed
            self.assertEqual((aaa["base_pairs"], aaa["quote_pairs"]), (2, 0))


class Seal(unittest.TestCase):
    def test_main_file_has_no_sealed_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            addrs = {a for p in doc["pools"] for a in (p["base"], p["quote"])}
            self.assertNotIn(LAUNCH, addrs)
            for x in (P3, P6, P7):
                self.assertNotIn(x, {p["pool"] for p in doc["pools"]})
            self.assertNotIn(LAUNCH, {t["token"] for t in doc["tokens"]})
            self.assertEqual(doc["meta"]["seal"]["tokens_dropped"], 1)
            self.assertEqual(doc["meta"]["seal"]["pools_dropped"], 3)
            self.assertNotIn(LAUNCH, json.dumps(doc))
            self.assertNotIn("LNCH", json.dumps(doc))

    def test_sealed_tokens_resolved_into_the_sealed_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Fake()
            (doc, sdoc, complete), _ = run_all(tmp, fake)
            self.assertTrue(any(LAUNCH in p for p in fake.paths if "/tokens/multi/" in p))  # seal fix A: resolved
            self.assertTrue(sdoc["meta"]["sealed"])
            self.assertEqual(sdoc["meta"]["complete"], complete)
            self.assertIn("tasks.csv address column as of the run", sdoc["meta"]["rule"])
            self.assertEqual(sorted(p["pool"] for p in sdoc["pools"]), [P3, P6, P7])
            for p in sdoc["pools"]:
                self.assertEqual(tuple(p), S.POOL_KEYS)
                self.assertIn(LAUNCH, (p["base"], p["quote"]))
            self.assertEqual([t["token"] for t in sdoc["tokens"]], [LAUNCH])
            t = sdoc["tokens"][0]
            self.assertEqual(tuple(t), S.TOKEN_KEYS)
            self.assertEqual(t["pool"], P7)  # its deepest pool (777,777 > 250,000.5)
            self.assertEqual((t["base_pairs"], t["quote_pairs"]), (2, 0))  # its own top pools + listed pools, as the survey
            self.assertEqual(t["asset_kind"], "")
            self.assertEqual(sdoc["meta"]["counts"], {"pools": 3, "tokens": 1, "tokens_unresolved": 0,
                                                     "asset_kinds": {}})
            self.assertEqual([k for k in walk_keys([sdoc["pools"], sdoc["tokens"]]) if BANNED_KEY.search(k)], [])
            self.assertNotIn(str(PRICE_MARK), json.dumps(sdoc))
            self.assertNotIn("0.5", json.dumps(sdoc["tokens"]))
            # every pool lands in exactly one of the two files
            main = {p["pool"] for p in doc["pools"]}
            self.assertEqual(main & {p["pool"] for p in sdoc["pools"]}, set())

    def test_sealed_file_written_beside_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(S.main(["--write", "--root", tmp, "--out", str(out)], client=Fake(), now=NOW), 0)
            sp = out / "sealed" / "2026-11-16.json.gz"
            sdoc = read_fake_sealed(sp)
            self.assertTrue(sdoc["meta"]["sealed"])
            self.assertEqual(sp.read_bytes()[4:8], b"\0\0\0\0")
            printed = buf.getvalue()
            self.assertNotIn(LAUNCH, printed)  # the run prints sealed counts and bytes only
            self.assertNotIn("LNCH", printed)
            self.assertNotIn(P7, printed)
            self.assertIn("sealed file: pools=3 tokens=1 raw_bytes=", printed)
            with self.assertRaises(PermissionError):
                S.read(sp)

    def test_nothing_else_names_the_sealed_folder(self):
        for folder in ("tools", "core", "adapters", "prompts", "site"):
            for p in (ROOT / folder).rglob("*"):
                if not p.is_file() or p.suffix not in (".py", ".html", ".js", ".yml") or p.name == "snapshot.py":
                    continue
                src = p.read_text(errors="replace")
                for word in ("snapshots/sealed", "liquid/snapshots", "sealed_path", "snapshot.SEALED",
                             "tools.snapshot", "import snapshot"):
                    self.assertFalse(word in src, f"{p.relative_to(ROOT)}: {word}")
        for wf in (ROOT / ".github/workflows").glob("*.yml"):
            if wf.name != "snapshot.yml":
                self.assertNotIn("snapshots", wf.read_text(), wf.name)
        wf = (ROOT / ".github/workflows/snapshot.yml").read_text()
        self.assertIn("git add -A backtest/liquid/snapshots/\n", wf)  # both files, nothing else

    def test_address_column_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, _), _ = run(tmp)
            self.assertIn(NOTE, {t["token"] for t in doc["tokens"]})  # only in the detail column
            self.assertIn(P5, {p["pool"] for p in doc["pools"]})

    def test_seal_is_bl45s_function(self):
        from tools import costs
        self.assertIs(S.launch_addresses, costs.launch_addresses)


class Complete(unittest.TestCase):
    def test_complete_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            (doc, complete), _ = run(tmp)
            self.assertTrue(complete)
            self.assertTrue(doc["meta"]["complete"])
            self.assertEqual(doc["meta"]["failed"], 0)
            self.assertFalse(doc["meta"]["capped"])

    def test_failed_call_writes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            fake = Fake(fail=("h24_tx_count_desc",))
            rc = quiet_main(["--write", "--root", tmp, "--out", str(out)], client=fake, now=NOW)
            self.assertEqual(rc, S.EXIT_PARTIAL)
            doc = S.read(out / "2026-11-16.json.gz")
            self.assertFalse(doc["meta"]["complete"])
            self.assertEqual(doc["meta"]["failed"], 10)  # each failed page counted, the next still asked
            self.assertTrue(doc["pools"])

    def test_cap_writes_partial_and_is_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            fake = Fake(never_end=True)
            rc = quiet_main(["--write", "--root", tmp, "--out", str(out), "--max-calls", "12"], client=fake, now=NOW)
            self.assertEqual(rc, S.EXIT_PARTIAL)
            self.assertLessEqual(fake.calls, 12 - candles.RETRIES + 1)
            doc = S.read(out / "2026-11-16.json.gz")
            m = doc["meta"]
            self.assertFalse(m["complete"])
            self.assertTrue(m["capped"])
            self.assertLessEqual(m["calls"], m["max_calls"])
            self.assertIn("max calls 12", m["errors"][-1])

    def test_budget_never_passes_the_cap(self):
        class Retrying(Fake):
            def get(self, path):  # every get() spends all its retries, as candles.Client can
                self.calls += candles.RETRIES
                return {"data": []}
        b = S.Budget(Retrying(), 23)
        with self.assertRaises(S.CapError):
            for _ in range(100):
                b.get("/x")
        self.assertLessEqual(b.calls, 23)

    def test_default_cap(self):
        self.assertEqual(S.MAX_CALLS, 300)

    def test_nothing_back_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            fake = Fake(fail=("/networks/base/",))
            self.assertEqual(quiet_main(["--write", "--root", tmp, "--out", str(out)], client=fake, now=NOW), 1)
            self.assertFalse((out / "2026-11-16.json.gz").exists())


class Files(unittest.TestCase):
    def test_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            out = Path(tmp) / "out"
            self.assertEqual(quiet_main(["--write", "--root", tmp, "--out", str(out)], client=Fake(), now=NOW), 0)
            p = out / "2026-11-16.json.gz"
            before = p.read_bytes()
            fake = Fake()
            self.assertEqual(quiet_main(["--write", "--root", tmp, "--out", str(out)], client=fake, now=NOW), 2)
            self.assertEqual(fake.calls, 0)  # refused before the first call
            self.assertEqual(p.read_bytes(), before)
            (doc, sdoc, _), _ = run_all(tmp)
            with self.assertRaises(FileExistsError):
                S.write(out, doc, NOW)
            sp = out / "sealed" / "2026-11-16.json.gz"
            sbefore = sp.read_bytes()
            with self.assertRaises(FileExistsError):
                S.write(out, sdoc, NOW)
            self.assertEqual(p.read_bytes(), before)
            self.assertEqual(sp.read_bytes(), sbefore)
            p.unlink()  # either file alone also refuses the day
            fake = Fake()
            self.assertEqual(quiet_main(["--write", "--root", tmp, "--out", str(out)], client=fake, now=NOW), 2)
            self.assertEqual(fake.calls, 0)

    def test_default_location(self):
        self.assertEqual(S.DIR, "backtest/liquid/snapshots")
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            self.assertEqual(quiet_main(["--write", "--root", tmp], client=Fake(), now=NOW), 0)
            got = [p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(sorted(got), ["backtest/liquid/snapshots/2026-11-16.json.gz",
                                           "backtest/liquid/snapshots/sealed/2026-11-16.json.gz",
                                           "data/daily/2026-11-15/tasks.csv"])

    def test_without_write_nothing_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_root(tmp)
            self.assertEqual(quiet_main(["--root", tmp], client=Fake(), now=NOW), 0)
            self.assertFalse((Path(tmp) / "backtest").exists())

    def test_committed_snapshots_are_clean(self):
        for p in (ROOT / S.DIR).glob("*.json.gz"):  # main files only: sealed/ is never opened
            self.assertRegex(p.name, r"^\d{4}-\d{2}-\d{2}\.json\.gz$")
            doc = S.read(p)
            self.assertEqual(doc["meta"]["card"], "BL-46")
            self.assertEqual([k for k in walk_keys(doc["pools"]) if BANNED_KEY.search(k)], [])

    def test_survey_files_byte_identical(self):
        pins = {"backtest/liquid/universe.json":
                "09c301d0ed8b5fbe5d4365659b950749d085fbec192be44dd8226e760e9b695a",
                "backtest/liquid/daily.jsonl.gz":
                "5d14fcee0fca60ac5f71f616acb6869a0cb5489cf99fc9d24d688aa6f6f71267"}
        for rel, h in pins.items():
            self.assertEqual(hashlib.sha256((ROOT / rel).read_bytes()).hexdigest(), h, rel)

    def test_snapshot_never_opens_survey_files(self):
        src = (ROOT / "tools/snapshot.py").read_text()
        for name in ("UNIVERSE", "DAILY", "universe.json", "daily.jsonl", "liquid.write", "liquid.load",
                     "liquid.fetch", "ohlcv"):
            self.assertNotIn(name, src)


class Isolation(unittest.TestCase):
    def test_core_never_imports_or_reads_it(self):
        for p in (ROOT / "core").glob("*.py"):
            src = p.read_text()
            self.assertNotIn("tools.snapshot", src, p.name)
            self.assertNotIn("snapshots", src, p.name)
            self.assertNotIn("backtest/liquid", src, p.name)

    def test_other_workflows_untouched(self):
        for wf in ("tick.yml", "costs.yml", "backtest.yml", "pages.yml", "alert-test.yml"):
            self.assertNotIn("snapshot", (ROOT / ".github/workflows" / wf).read_text(), wf)

    def test_workflow(self):
        wf = (ROOT / ".github/workflows/snapshot.yml").read_text()
        self.assertIn('cron: "37 0 * * 1"', wf)
        self.assertIn("workflow_dispatch:", wf)
        self.assertIn("group: snapshot", wf)
        self.assertIn('python-version: "3.13"', wf)
        self.assertIn("timeout-minutes:", wf)
        self.assertIn("python -m tools.snapshot --write", wf)
        self.assertNotIn("secrets.", wf)
        self.assertNotIn("env:", wf)
        self.assertNotIn("set -x", wf)
        adds = [ln.strip() for ln in wf.splitlines() if ln.strip().startswith("git add")]
        self.assertEqual(adds, ["git add -A backtest/liquid/snapshots/"])
        self.assertIn("ABORT: something outside backtest/liquid/snapshots/ is staged", wf)
        self.assertIn('git commit -q -m "backtest: snapshot $day"', wf)
        self.assertIn("for try in 1 2 3 4 5; do", wf)
        self.assertIn("git pull --rebase -q origin main && git push -q origin HEAD:main", wf)
        code = "\n".join(ln for ln in wf.splitlines() if not ln.lstrip().startswith("#"))
        for bad in ("data/", "core/", "site/"):
            self.assertNotIn(bad, code)


if __name__ == "__main__":
    unittest.main()
