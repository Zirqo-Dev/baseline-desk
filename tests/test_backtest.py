"""Offline tests for BL-33: tools/candles.py and tools/backtest.py. Fixtures only, no network."""
import contextlib
import csv
import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from core import config, friction
from core.book import TASK_FIELDS, TRADE_FIELDS
from tools import backtest as bt
from tools import candles as cdl

T = cdl._ts("2026-11-01T12:00:00Z")  # a decision time on a candle boundary
OUT_FIELDS = ["ts", "symbol", "address", "first_seen", "horizon", "age_min", "first_price", "price", "chg_pct", "liq",
              "pair_b1", "pair_s1", "pair_b24", "pair_s24", "dex", "gone", "qliq"]
EXITS = dict(config.EXIT)


def a(i):
    return f"0x{i:040x}"


def c(t, o, h, low, close, v=100.0):
    return [t, o, h, low, close, v]


def keys(qliq=20000, liq=80000, s1=3, s24=40, dex="uniswap:v4", pair_age=None):
    k = f"b1=5 s1={s1} b24=50 s24={s24} liq={liq} dex={dex} qliq={qliq}"
    return k + (f" pair_age={pair_age} token_age={pair_age}" if pair_age is not None else "")


def verify(ts, address, detail, action="WATCH", reason="goplus_not_ready", symbol="S"):
    return {"ts": ts, "book": "desk", "stage": "VERIFY", "symbol": symbol, "address": address, "action": action,
            "reason": reason, "detail": detail}


def label(ts, address, first_seen, h, price="1.0", chg="0.0", qliq="20000", liq="80000"):
    return {"ts": ts, "symbol": "S", "address": address, "first_seen": first_seen, "horizon": f"{h}h",
            "age_min": str(h * 60), "first_price": "1.0", "price": price, "chg_pct": chg, "liq": liq, "pair_b1": "1",
            "pair_s1": "1", "pair_b24": "9", "pair_s24": "9", "dex": "uniswap:v4", "gone": "0", "qliq": qliq}


class Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, day, name, fields, rows):
        p = self.root / f"data/daily/{day}/{name}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def days(self, *days):
        for d in days:
            p = self.root / f"data/daily/{d}/tasks.csv"
            if not p.exists():
                self.put(d, "tasks", TASK_FIELDS, [])


class SimRulesTest(unittest.TestCase):
    """The pessimistic candle rules, one at a time (ticket 5, fixed 0% fees so prices read plainly)."""

    def sim(self, candles, t=T, end=None, exits=None, **kw):
        return bt.simulate(candles, end or (candles[-1][0] + 300 if candles else T), t, exits or EXITS, 5.0,
                           bt.FixedFees(0.0, 0.0), **kw)

    def test_buy_at_the_open_of_the_first_candle_at_or_after_the_decision(self):
        r = self.sim([c(T - 300, 9, 9, 9, 9), c(T + 300, 1.0, 1.1, 0.95, 1.05)], t=T + 100)
        self.assertEqual((r["entry_t"], r["entry_price"], r["wait_s"]), (T + 300, 1.0, 200))
        r = self.sim([c(T, 2.0, 2.0, 2.0, 2.0)], t=T)  # a candle starting exactly at the decision counts
        self.assertEqual(r["entry_price"], 2.0)

    def test_no_trade_within_5_min_is_no_fill_and_no_candles_no_data(self):
        self.assertEqual(self.sim([c(T + 300, 1, 1, 1, 1)], t=T)["status"], "no_fill")  # 300 s: too late
        self.assertEqual(self.sim([c(T + 299, 1, 1, 1, 1)], t=T)["status"], "pending")
        self.assertEqual(self.sim([c(T - 300, 1, 1, 1, 1)], t=T)["status"], "no_fill")  # only before the decision
        self.assertEqual(bt.simulate([], T, T, EXITS, 5.0, bt.FixedFees(0, 0))["status"], "no_data")

    def test_stop_checked_first_inside_a_candle(self):
        # one candle reaches the +40% rung AND the -25% stop: the stop wins
        r = self.sim([c(T, 1.0, 1.5, 0.7, 1.4)])
        self.assertEqual((r["status"], r["exit_rule"], r["exit_price"]), ("closed", "hard_stop", 0.75))
        self.assertEqual(len(r["sells"]), 1)

    def test_gap_below_the_stop_exits_at_the_open(self):
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 0.5, 0.6, 0.4, 0.45)])
        self.assertEqual((r["exit_rule"], r["exit_price"], r["exit_t"]), ("hard_stop", 0.5, T + 300))

    def test_ladder_then_trail_uses_peaks_of_earlier_candles_only(self):
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0),          # entry 1.0
                      c(T + 300, 1.2, 1.45, 1.2, 1.4),    # rung 1 at 1.40 (+40%), peak 1.45 from here on
                      c(T + 600, 1.4, 3.0, 1.05, 1.1)])   # a high of 3.0 in the same candle must NOT lift the trail
        self.assertEqual([s["rule"] for s in r["sells"]], ["ladder_1", "trail"])
        self.assertAlmostEqual(r["sells"][0]["price"], 1.4)
        self.assertAlmostEqual(r["sells"][1]["price"], 1.45 * 0.75)  # trail from the EARLIER peak 1.45, not 3.0
        self.assertAlmostEqual(r["sells"][0]["qty"], 5.0 * 0.33)

    def test_time_stop_on_the_last_close_when_no_trade(self):
        # entry, then no trade for an hour: at +45 min the pool price is the last close (+5% < +10%)
        r = self.sim([c(T, 1.0, 1.05, 1.0, 1.05), c(T + 3600, 2.0, 2.0, 2.0, 2.0)])
        self.assertEqual((r["exit_rule"], r["exit_t"], r["exit_price"]), ("time_stop", T + 2700, 1.05))
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0)], end=T + 4 * 3600)  # no candle after entry at all
        self.assertEqual((r["exit_rule"], r["exit_t"]), ("time_stop", T + 2700))

    def test_time_stop_later_at_a_candle_open(self):
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.2), c(T + 2700, 1.2, 1.2, 1.2, 1.2), c(T + 3000, 1.05, 1.05, 1.0, 1.0)])
        self.assertEqual((r["exit_rule"], r["exit_t"], r["exit_price"]), ("time_stop", T + 3000, 1.05))

    def test_horizon_not_reached_is_pending_never_a_win(self):
        r = self.sim([c(T, 1.0, 1.3, 1.0, 1.3), c(T + 300, 1.3, 1.39, 1.3, 1.35)], end=T + 600)
        self.assertEqual((r["status"], r["pnl"]), ("pending", None))
        r = self.sim([c(T, 1.0, 1.3, 1.0, 1.3)], end=T + 600, exits=dict(EXITS, time_stop_min=10_000))
        self.assertEqual(r["status"], "pending")

    def test_candles_after_the_data_end_are_never_read(self):
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 600, 0.1, 0.1, 0.1, 0.1)], end=T + 600,
                     exits=dict(EXITS, time_stop_min=10_000))
        self.assertEqual(r["status"], "pending")  # the crash at T + 600 is past the data end

    def test_liq_pull_reads_the_logged_liquidity(self):
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 1.1, 1.1, 1.1, 1.1)], liq=100_000,
                     liq_at=lambda t: 50_000 if t >= T + 300 else None)
        self.assertEqual((r["exit_rule"], r["exit_price"]), ("liq_pull", 1.1))

    def test_max_hold(self):
        ex = dict(EXITS, time_stop_min=10_000, max_hold_h=1)
        r = self.sim([c(T, 1.0, 1.0, 1.0, 1.0)], end=T + 7200, exits=ex)
        self.assertEqual((r["exit_rule"], r["exit_t"]), ("max_hold", T + 3600))


class FrictionTest(unittest.TestCase):
    def test_core_friction_is_imported_not_copied(self):
        self.assertIs(bt.friction, friction)
        src = Path(bt.__file__).read_text()
        self.assertNotIn("base_pct", src)
        self.assertNotIn("impact_pct", src)

    def test_buy_and_sells_priced_by_core(self):
        r = bt.simulate([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 0.5, 0.5, 0.5, 0.5)], T + 600, T, EXITS, 5.0,
                        bt.CoreFees("clanker"), qliq=20_000, qliq_at=lambda t0, t1: 4_000)
        fill, pct = friction.buy_fill(1.0, 5.0, 20_000, "clanker")
        self.assertEqual((r["entry_fill"], r["entry_pct"]), (fill, pct))
        qty = 5.0 / fill
        out, spct = friction.sell_fill(0.5, qty, 4_000, "clanker")  # the last logged qliq after entry
        self.assertEqual(r["sells"][0]["fill"], out)
        self.assertEqual(r["sells"][0]["qliq_src"], "exit")
        r = bt.simulate([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 0.5, 0.5, 0.5, 0.5)], T + 600, T, EXITS, 5.0,
                        bt.CoreFees("other"), qliq=20_000, qliq_at=lambda t0, t1: None)
        self.assertEqual(r["sells"][0]["qliq_src"], "entry")
        r = bt.simulate([c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 0.5, 0.5, 0.5, 0.5)], T + 600, T, EXITS, 5.0,
                        bt.CoreFees("other"), qliq=None)
        self.assertEqual((r["entry_pct"], r["sells"][0]["qliq_src"]), (3.0 + 5.0, "none"))  # the thin_pct fallback


class LeakTest(Root):
    """No row, label or field dated after the decision time ever reaches a decision."""

    def fixture(self, extra_future=False):
        d1, d2 = "2026-11-01", "2026-11-02"
        rows = [verify("2026-11-01T10:00:00Z", a(1), keys(qliq=20000, pair_age=600)),
                verify("2026-11-01T11:00:00Z", a(1), "first_seen=2026-11-01T10:00:00Z checks=5 " + keys(qliq=30000))]
        labels = [label("2026-11-01T11:00:30Z", a(1), "2026-11-01T10:00:00Z", 1, price="1.5", chg="50.0")]
        later = [verify("2026-11-01T12:30:00Z", a(1), "first_seen=2026-11-01T10:00:00Z " + keys(qliq=1)),
                 verify("2026-11-01T13:00:00Z", a(2), keys())]
        later_labels = [label("2026-11-01T16:00:30Z", a(1), "2026-11-01T10:00:00Z", 6, price="9.0", chg="800.0")]
        self.put(d1, "tasks", TASK_FIELDS, rows + (later if extra_future else []))
        self.put(d1, "outcomes", OUT_FIELDS, labels + (later_labels if extra_future else []))
        self.put(d2, "tasks", TASK_FIELDS, [verify("2026-11-02T01:00:00Z", a(1), keys(qliq=2))] if extra_future else [])
        return bt.Logs(self.root, [d1, d2])

    def test_facts_as_of_t_ignore_everything_later(self):
        t = cdl._ts("2026-11-01T12:00:00Z")
        before = self.fixture(False).asof(t).facts(a(1))
        after = self.fixture(True).asof(t).facts(a(1))
        self.assertEqual(before, after)
        self.assertEqual((after["qliq"], after["l1_price"], after["l1_chg_pct"]), (30000, 1.5, 50.0))
        self.assertNotIn("l6_price", after)
        self.assertAlmostEqual(after["pool_age_h"], (600 + 120) / 60)  # 600 min at 10:00 + 2 h
        self.assertEqual(after["sight_qliq"], 20000)

    def test_view_never_returns_a_later_row(self):
        logs = self.fixture(True)
        for t in sorted({x[0] for v in logs.rows.values() for x in v}):
            v = logs.asof(t)
            for addr in logs.rows:
                self.assertTrue(all(x[0] <= t for x in v.rows(addr)))
                for k, val in v.facts(addr).items():
                    self.assertIsNotNone(val, k)
        self.assertEqual(logs.asof(cdl._ts("2026-11-01T12:00:00Z")).tokens(), [a(1)])

    def test_cutoff_drops_rows_at_or_after_it(self):
        self.fixture(True)
        logs = bt.Logs(self.root, ["2026-11-01", "2026-11-02"], cutoff=cdl._ts("2026-11-02T00:00:00Z"))
        self.assertTrue(all(x[0] < logs.cutoff for v in logs.rows.values() for x in v))
        self.assertEqual(logs.asof(cdl._ts("2026-11-03T00:00:00Z")).facts(a(1))["qliq"], 1)  # not the Nov 2 row

    def test_last_qliq_after_entry_only(self):
        logs = self.fixture(True)
        v = logs.asof(cdl._ts("2026-11-01T14:00:00Z"))
        self.assertEqual(v.last(a(1), "qliq", after=cdl._ts("2026-11-01T12:00:00Z")), 1)
        self.assertIsNone(v.last(a(1), "qliq", after=cdl._ts("2026-11-01T12:45:00Z")))


SPEC = {"name": "test-spec", "text": "qliq >= 10k at pool age >= 1 h", "ticket_usd": 5,
        "universe": [["qliq", ">=", 10000]], "entry": [["pool_age_h", ">=", 1]], "exits": dict(EXITS),
        "match": ["exact", "single", "age"]}


class RunTest(Root):
    def setUp(self):
        super().setUp()
        days = ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        self.days(*days)
        self.put("2026-11-01", "tasks", TASK_FIELDS,
                 [verify(f"2026-11-01T{h:02d}:00:00Z", a(i), keys(qliq=20000 if i < 4 else 5, pair_age=90))
                  for i, h in ((1, 10), (2, 10), (3, 11), (4, 11))])
        self.put("2026-11-05", "tasks", TASK_FIELDS,  # a holdout day: never traded by a development run
                 [verify("2026-11-05T10:00:00Z", a(9), keys(qliq=50000, pair_age=90))])
        lines = []
        for i in (1, 2, 3, 4, 9):
            t0 = cdl._ts(("2026-11-05" if i == 9 else "2026-11-01") + "T00:00:00Z")
            lines.append({"address": a(i), "symbol": "S", "sight_ts": cdl._iso(t0), "pool": "0xp",
                          "match": "exact" if i != 3 else "dex", "pool_created": None, "start": t0,
                          "end": t0 + 7 * 86400, "candles": [c(t0 + k * 300, 1.0, 1.0, 1.0, 1.0) for k in range(300)]})
        cdl.write(self.root / cdl.DIR / "2026-11-01.jsonl.gz", {"day": "2026-11-01"}, lines)
        self.now = cdl._ts("2026-11-06T12:00:00Z")
        self.spec = bt.check_spec(json.loads(json.dumps(SPEC)))

    def test_split_last_third_rounded_up_and_printed(self):
        dev, hold = bt.split(self.root, self.now)
        self.assertEqual((dev[-1], hold), ("2026-11-03", ["2026-11-04", "2026-11-05"]))
        self.days("2026-11-06")
        dev, hold = bt.split(self.root, cdl._ts("2026-11-07T01:00:00Z"))  # 7 days: holdout 3
        self.assertEqual(hold, ["2026-11-04", "2026-11-05", "2026-11-06"])
        self.assertIn("holdout 2026-11-04 .. 2026-11-06 (3 days)", bt.split_line(dev, hold, False))

    def test_development_run_never_sees_holdout_days(self):
        res = bt.run_spec(self.root, self.spec, now=self.now)
        mine = [t for t in res["trades"] if t["book"] == "test-spec"]
        self.assertEqual(sorted(t["address"] for t in mine), [a(1), a(2), a(3)])  # a(4) thin, a(9) holdout
        cut = cdl._ts("2026-11-04T00:00:00Z")
        self.assertTrue(all(t["decision_t"] < cut for t in res["trades"]))
        self.assertTrue(all(t.get("exit_t") is None or t["exit_t"] < cut for t in res["trades"]))
        self.assertEqual(res["split"]["holdout"], ["2026-11-04", "2026-11-05"])
        by = {t["address"]: t for t in mine}
        self.assertEqual((by[a(3)]["match"], by[a(3)]["status"]), ("dex", "no_data"))  # not a qualified pool
        self.assertEqual(by[a(1)]["status"], "closed")  # flat candles: the time stop
        self.assertEqual(res["summary"]["test-spec"]["no_data"], 1)

    def test_twin_is_seeded_same_universe_same_exits(self):
        r1 = bt.run_spec(self.root, self.spec, now=self.now)
        r2 = bt.run_spec(self.root, self.spec, now=self.now)
        tw = lambda r: [(t["address"], t["decision_t"]) for t in r["trades"] if t["book"] == "twin"]
        self.assertEqual(tw(r1), tw(r2))  # same spec sha: same picks
        self.assertTrue(all(t["address"] in (a(1), a(2)) for t in r1["trades"] if t["book"] == "twin"))
        other = dict(self.spec, sha256="f" * 64)
        import hashlib
        picks = {tuple(tw(bt.run_spec(self.root, dict(other, sha256=hashlib.sha256(str(k).encode()).hexdigest()),
                                      now=self.now))) for k in range(8)}
        self.assertGreater(len(picks), 1)  # the seed moves the picks

    def test_holdout_lock_once_then_refused_then_allowed_by_an_owner_line(self):
        self.assertEqual(bt.holdout_refusal(self.root, self.spec), "")
        res = bt.run_spec(self.root, self.spec, holdout=True, now=self.now)
        self.assertEqual([t["address"] for t in res["trades"] if t["book"] == "test-spec"], [a(9)])
        bt.write_lock(self.root, self.spec, res["split"]["dev"], res["split"]["holdout"], self.now, res["summary"])
        lock = bt.load_lock(self.root)
        self.assertEqual((lock["spec"], lock["sha256"], lock["split"]["holdout"]),
                         ("test-spec", self.spec["sha256"], ["2026-11-04", "2026-11-05"]))
        self.assertIn("refused", bt.holdout_refusal(self.root, self.spec))
        with self.assertRaises(PermissionError):
            bt.run_spec(self.root, self.spec, holdout=True, now=self.now)
        self.days("2026-11-06", "2026-11-07")  # new days never move a frozen split
        self.assertEqual(bt.split(self.root, cdl._ts("2026-11-09T00:00:00Z"))[1], ["2026-11-04", "2026-11-05"])
        dec = self.root / bt.DECISIONS
        dec.parent.mkdir(parents=True, exist_ok=True)
        dec.write_text(f"| 2026-11-10 | BL-34 | holdout-rerun test-spec {self.spec['sha256'][:12]} allowed |\n")
        self.assertIn("refused", bt.holdout_refusal(self.root, self.spec))  # not marked (the owner)
        dec.write_text(f"| 2026-11-10 | BL-34 | holdout-rerun test-spec {self.spec['sha256'][:12]} (the owner) |\n")
        self.assertEqual(bt.holdout_refusal(self.root, self.spec), "")
        self.assertIn("refused", bt.holdout_refusal(self.root, dict(self.spec, sha256="0" * 64)))  # another spec
        bt.write_lock(self.root, self.spec, [], [], self.now + 60, {"x": 1})
        lock = bt.load_lock(self.root)
        self.assertEqual((lock["ran_at"], len(lock["reruns"])), (cdl._iso(self.now), 1))  # the first stays

    def test_cli_holdout_needs_write(self):
        p = self.root / "spec.json"
        p.write_text(json.dumps(SPEC))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bt.main(["--spec", str(p), "--holdout", "--root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / bt.LOCK).exists())

    def test_spec_sha256_is_of_the_file_and_bad_specs_refused(self):
        p = self.root / "spec.json"
        p.write_text(json.dumps(SPEC))
        import hashlib
        self.assertEqual(bt.load_spec(p)["sha256"], hashlib.sha256(p.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            bt.check_spec(dict(SPEC, universe=[["tomorrow_price", ">", 1]]))  # not an as-of field
        with self.assertRaises(ValueError):
            bt.check_spec(dict(SPEC, exits={"hard_stop_pct": -25}))


class FakeHTTP:
    """urlopen stand-in: answers from a list of (status or body) in order; records URLs."""

    def __init__(self, answers):
        self.answers, self.urls = list(answers), []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        ans = self.answers.pop(0)
        if isinstance(ans, int):
            raise urllib.error.HTTPError(req.full_url, ans, "x", {}, None)
        return contextlib.closing(io.BytesIO(json.dumps(ans).encode()))


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class CandleFetchTest(Root):
    def client(self, answers):
        clock = Clock()
        http = FakeHTTP(answers)
        return cdl.Client(urlopen=http, sleep=clock.sleep, clock=clock), http, clock

    def test_paced_and_retried_on_429(self):
        cl, http, clock = self.client([429, 429, {"ok": 1}, {"ok": 2}, 404])
        self.assertEqual(cl.get("/x"), {"ok": 1})
        self.assertEqual(cl.get("/y"), {"ok": 2})
        self.assertIsNone(cl.get("/z"))
        self.assertEqual((cl.calls, cl.refused), (5, 2))
        self.assertTrue(all(s >= cdl.BACKOFF_S or abs(s - cdl.PACE_S) < 1e-9 for s in clock.slept))
        cl, _, _ = self.client([429] * cdl.RETRIES)
        with self.assertRaises(cdl.FetchError):
            cl.get("/x")

    def test_ohlcv_pages_back_past_1000_and_sorts(self):
        end = T + 7 * 86400
        page1 = [c(end - 300 * (k + 1), 1, 1, 1, 1) for k in range(1000)]  # newest first, as the API answers
        page2 = [c(end - 300 * (k + 1001), 2, 2, 2, 2) for k in range(500)]
        body = lambda rows: {"data": {"attributes": {"ohlcv_list": rows}}}
        cl, http, _ = self.client([body(page1), body(page2)])
        out = cdl.ohlcv(cl, "0xpool", a(1), T, end)
        self.assertEqual(len(http.urls), 2)
        self.assertIn(f"before_timestamp={end}", http.urls[0])
        self.assertIn(f"before_timestamp={page1[-1][0]}", http.urls[1])
        self.assertIn("aggregate=5&limit=1000", http.urls[0])
        self.assertIn(f"token={a(1)}", http.urls[0])
        self.assertEqual([x[0] for x in out], sorted(x[0] for x in out))
        self.assertTrue(all(T <= x[0] < end for x in out))
        self.assertEqual(len(out), 1500)

    def test_match_classes(self):
        tok = {"dex": "uniswap:v4", "created": T}
        p = lambda pool, dex, created, res=1.0: {"pool": pool, "dex": dex, "created": created, "reserve": res}
        self.assertEqual(cdl.classify(tok, [])[1], "none")
        self.assertEqual(cdl.classify(tok, [p("A", "uniswap-v4-base", None)])[1], "single")
        two = [p("A", "uniswap-v4-base", T - 5000, 9), p("B", "uniswap-v4-base", T + 300, 1)]
        self.assertEqual(cdl.classify(tok, two)[:2], ("B", "age"))
        self.assertEqual(cdl.classify(dict(tok, created=None), two)[:2], ("A", "guess"))
        self.assertEqual(cdl.classify(tok, [p("A", "uniswap-v4-base", None), p("C", "aerodrome-base", None)])[1], "dex")
        self.assertEqual(cdl.classify(tok, [p("C", "aerodrome-base", None, 5), p("D", "aerodrome-base", None, 9)])[:2],
                         ("D", "other"))
        self.assertTrue(cdl.dex_match("uniswap:v3", "uniswap-v3-base"))
        self.assertFalse(cdl.dex_match("uniswap:v4", "uniswap-v3-base"))
        self.assertFalse(cdl.dex_match("na", "uniswap-v3-base"))
        self.assertEqual(cdl.QUALIFIED, ("exact", "single", "age"))

    def test_resolve_known_first_then_one_batch(self):
        toks = {a(1): {"dex": "uniswap:v4", "created": None}, a(2): {"dex": "uniswap:v4", "created": None}}
        multi = {"data": [{"attributes": {"address": a(2)}, "relationships": {"top_pools": {"data": [{"id": "p2"}]}}}],
                 "included": [{"id": "p2", "type": "pool", "attributes": {"address": "0xP2",
                               "pool_created_at": "2026-11-01T00:00:00Z", "reserve_in_usd": "100"},
                               "relationships": {"dex": {"data": {"id": "uniswap-v4-base"}}}}]}
        cl, http, _ = self.client([multi])
        out = cdl.resolve(cl, toks, known={a(1): "0xexact"})
        self.assertEqual(out[a(1)], ("0xexact", "exact", None))
        self.assertEqual(out[a(2)][:2], ("0xp2", "single"))
        self.assertEqual(len(http.urls), 1)
        self.assertIn(f"tokens/multi/{a(2)}?include=top_pools", http.urls[0])

    def test_day_file_final_once_closed_else_open(self):
        day = "2026-11-01"
        self.put(day, "tasks", TASK_FIELDS, [verify("2026-11-01T10:00:00Z", a(1), keys())])
        empty = {"data": [], "included": []}
        cl, _, _ = self.client([empty, None and 0 or {"data": []}])
        with mock.patch.object(cdl, "state_pairs", return_value={}):
            path, n = cdl.fetch_day(cl, self.root, day, now=cdl._ts("2026-11-03T00:00:00Z"))
            self.assertEqual((path.name, n), ("2026-11-01.open.jsonl.gz", 1))
            meta, lines = cdl.read(path)
            self.assertEqual((meta["complete"], lines[0]["match"], lines[0]["candles"]), (False, "none", []))
            cl, _, _ = self.client([empty, {"data": []}])
            path, _ = cdl.fetch_day(cl, self.root, day, now=cdl._ts("2026-11-09T00:00:00Z"))
        self.assertEqual(path.name, "2026-11-01.jsonl.gz")
        self.assertFalse((self.root / cdl.DIR / "2026-11-01.open.jsonl.gz").exists())
        self.assertNotIn(day, cdl.days_to_fetch(self.root, cdl._ts("2026-11-09T00:00:00Z")))
        with self.assertRaises(ValueError):
            cdl.fetch_day(cl, self.root, "2026-11-01; rm -rf /")

    def test_files_never_under_data(self):
        self.assertTrue(cdl.DIR.startswith("backtest/"))
        self.assertTrue(bt.RESULTS.startswith("backtest/") and bt.LOCK.startswith("backtest/"))

    def test_no_network_in_the_suite(self):
        """The simulator and a run touch no socket (the fetch is the only networked path, faked above)."""
        def boom(*a, **k):
            raise AssertionError("network")
        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom):
            bt.simulate([c(T, 1, 1, 1, 1)], T + 600, T, EXITS, 5.0, bt.CoreFees())
            self.days("2026-10-31", "2026-11-01", "2026-11-02")
            bt.run_spec(self.root, bt.check_spec(json.loads(json.dumps(SPEC))), now=cdl._ts("2026-11-04T00:00:00Z"))


class TickNeverReadsBacktestTest(unittest.TestCase):
    def test_core_never_imports_tools_or_reads_backtest(self):
        for p in Path(friction.__file__).parent.glob("*.py"):
            src = p.read_text()
            self.assertNotIn("from tools", src, p.name)
            self.assertNotIn("import tools", src, p.name)
            self.assertNotIn("backtest/", src, p.name)

    def test_workflow_by_hand_only_and_stages_backtest_only(self):
        wf = (Path(friction.__file__).parent.parent / ".github/workflows/backtest.yml").read_text()
        self.assertIn("workflow_dispatch", wf)
        self.assertNotRegex(wf, r"(?m)^\s+schedule:")
        self.assertNotIn("cron:", wf)
        self.assertIn("git add -A backtest/candles/", wf)
        self.assertIn("grep -v '^backtest/'", wf)
        self.assertIn("pull --rebase", wf)
        self.assertNotIn("git add data", wf)
        self.assertNotIn("set -x", wf)
        tick = (Path(friction.__file__).parent.parent / ".github/workflows/tick.yml").read_text()
        self.assertNotIn("backtest", tick)


class ValidationTest(Root):
    def test_twin_trade_compared_with_its_logged_exit(self):
        days = ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        self.days(*days)
        buy_ts, sell_ts = "2026-11-01T12:00:12Z", "2026-11-01T12:45:12Z"
        fill_in = 1.0 * 1.03
        qty = 5.0 / fill_in
        self.put("2026-11-01", "trades", TRADE_FIELDS, [
            {"ts": buy_ts, "book": "baseline", "side": "BUY", "symbol": "S", "address": a(1), "qty": qty, "usd": 5.0,
             "intended_price": 1.0, "fill_price": fill_in, "friction_pct": 3.0, "liq_usd": 90000,
             "launchpad": "other", "reason": "entry", "pnl_usd": "", "note": ""},
            {"ts": sell_ts, "book": "baseline", "side": "SELL", "symbol": "S", "address": a(1), "qty": qty,
             "usd": qty * 0.97, "intended_price": 1.0, "fill_price": 0.97, "friction_pct": 3.0, "liq_usd": 90000,
             "launchpad": "other", "reason": "time_stop", "pnl_usd": qty * 0.97 - 5.0, "note": ""}])
        t0 = cdl._ts(buy_ts) // 300 * 300
        cdl.write(self.root / cdl.DIR / "twin-validation.jsonl.gz", {}, [
            {"address": a(1), "symbol": "S", "sight_ts": buy_ts, "pool": "0xp", "match": "exact",
             "pool_created": None, "start": t0, "end": t0 + 86400,
             "candles": [c(t0 + 300, 1.0, 1.0, 1.0, 1.0), c(t0 + 3600, 1.0, 1.0, 1.0, 1.0)]}])
        v = bt.validate_twin(self.root, now=cdl._ts("2026-11-06T12:00:00Z"))
        self.assertEqual(len(v["rows"]), 1)
        r = v["rows"][0]
        self.assertEqual((r["match"], r["status"], r["logged_exit"], r["sim_exit"]), ("exact", "closed", "time_stop",
                                                                                       "time_stop"))
        self.assertAlmostEqual(r["pnl_logged_fric"], r["logged_pnl"], places=9)  # same prices, same friction
        out = bt.render_validation(v)
        self.assertIn("split: development 2026-10-31 .. 2026-11-03", out)
        self.assertIn("same exit rule 1", out)

    def test_validation_reads_development_days_only(self):
        days = ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        self.days(*days)
        for d in days:
            self.put(d, "trades", TRADE_FIELDS, [])
            self.put(d, "outcomes", OUT_FIELDS, [])
        read = []
        real = bt.replay._read
        with mock.patch.object(bt.replay, "_read", lambda p: read.append(str(p)) or real(p)):
            bt.validate_twin(self.root, now=cdl._ts("2026-11-06T12:00:00Z"))
        self.assertTrue(read)
        self.assertFalse([p for p in read if "2026-11-04" in p or "2026-11-05" in p])

    def test_stop_inside_a_candle_fills_at_the_worse_of_level_and_close(self):
        rug = [c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 1.0, 1.0, 1e-7, 1e-4)]  # a liquidity pull inside one candle
        r = bt.simulate(rug, T + 600, T, EXITS, 5.0, bt.FixedFees(0, 0))
        self.assertEqual((r["exit_rule"], r["exit_price"]), ("hard_stop", 1e-4))
        r = bt.simulate(rug, T + 600, T, EXITS, 5.0, bt.FixedFees(0, 0), stop_fill="level")
        self.assertEqual(r["exit_price"], 0.75)
        wick = [c(T, 1.0, 1.0, 1.0, 1.0), c(T + 300, 1.0, 1.0, 0.7, 0.9)]  # close back above the stop: the level
        self.assertEqual(bt.simulate(wick, T + 600, T, EXITS, 5.0, bt.FixedFees(0, 0))["exit_price"], 0.75)


if __name__ == "__main__":
    unittest.main()
