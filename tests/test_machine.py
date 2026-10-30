"""Offline tests for BL-23: the machine cards in report.json and data/tokens_24h.json. No network."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core import config, report, tick
from core.book import TASK_FIELDS, _append
from tests import test_baseline, test_tick
from tests.test_tick import T0, addr, detail, good

ROLL = datetime(2026, 10, 24, 0, 3, tzinfo=timezone.utc)
D = "2026-10-23"


def row(t, stage, action="", reason="", detail="", book="desk", symbol="", address=""):
    return {"ts": t, "book": book, "stage": stage, "symbol": symbol, "address": address,
            "action": action, "reason": reason, "detail": detail}


def tick_row(t, secs=1.0, **kw):
    keys = dict(dict.fromkeys(tick.COUNTS, 0), goplus=0, requests=3, secs=secs, **kw)
    return row(t, "TICK", "HEARTBEAT", detail=" ".join(f"{k}={v}" for k, v in keys.items()))


def skip(t, a, reason, extra="", symbol="S"):
    return row(t, "VERIFY", "SKIP", reason, f"{extra} until=x".strip(), symbol=symbol, address=a)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, d, rows):
        for r in rows:
            _append(self.root / config.TASKS.format(day=d), TASK_FIELDS, r)

    def state(self, since=D, **kw):
        eq = {"desk": [{"day": D, "value_usd": 50.0, "cash_usd": 50.0, "open": 0}]}  # as the roll leaves it
        return {"since": since, "due": "", "machine_due": "", "equity": eq, "peak": {}, "max_dd_pct": {}, **kw}

    def cards(self, d=D, s=None, open_n=0):
        return report.machine(self.root, ROLL, d, s or self.state(), open_n)


class CardsTest(Files):
    def test_field_shapes(self):
        self.put(D, [tick_row("2026-10-23T00:03:00Z", 2.0), tick_row("2026-10-23T00:08:00Z", 4.5, seen=7),
                     skip("2026-10-23T00:08:00Z", addr(1), "liquidity", "goplus=skipped")])
        c, tokens, t = self.cards(open_n=2)
        self.assertEqual(set(c), {"day", "as_of", "last_tick", "funnel", "funnel_since", "kills", "kills_since",
                                  "kill_log", "runs", "tick_secs", "ticks_per_hour"})
        self.assertEqual(c["last_tick"], {"ts": "2026-10-23T00:08:00Z", "seen": 7, "new": 0, "checked": 0,
                                          "dex_only": 0, "goplus": 0, "passed": 0, "bought": 0, "requests": 3,
                                          "secs": 4.5})
        self.assertEqual(c["funnel"], {"from": D, "to": D, "scan": 1, "gate": 0, "safety": 0, "buy": 0,
                                       "exits_open": 2})
        self.assertEqual(c["funnel_since"], {"from": D, "to": D, "scan": 1, "gate": 0, "safety": 0, "buy": 0})
        self.assertEqual(c["kills"], {"from": D, "to": D, "gate": {"liquidity": 1}, "safety": {}})
        self.assertEqual(c["runs"], {"ticks": 2, "expected": 288, "rate": 0.69})
        self.assertEqual(c["tick_secs"], {"median": 3.2, "max": 4.5})  # median 3.25 rounds to 3.2
        self.assertEqual(t["through"], D)
        json.dumps(c)  # serialisable

    def test_empty_day(self):
        c, tokens, _ = self.cards()
        self.assertIsNone(c["last_tick"])
        self.assertEqual((c["runs"]["ticks"], c["runs"]["rate"], c["tick_secs"]), (0, 0.0, {"median": 0.0, "max": 0.0}))
        self.assertEqual(tokens, [])

    def test_gate_safety_split(self):
        t = "2026-10-23T01:00:00Z"
        self.put(D, [skip(t, addr(1), "liquidity", "goplus=skipped"),
                     skip(t, addr(2), "missing:liq_usd", "goplus=skipped"),
                     skip(t, addr(3), "liquidity"),                       # GoPlus pending, pool too small
                     skip(t, addr(4), "fetch_error_x3", "try=3 hosts=ds_pairs"),
                     skip(t, addr(5), "missing:age_min"),
                     skip(t, addr(6), "goplus_timeout"), skip(t, addr(7), "mint_open"),
                     skip(t, addr(8), "lp_locked", "lp_unknown"), skip(t, addr(9), "missing:dev_pct"),
                     skip(t, addr(10), "too_old"),
                     row(t, "VERIFY", "WATCH", "goplus_not_ready", address=addr(11)),
                     row(t, "VERIFY", "RETRY", "fetch_error", "goplus=skipped", address=addr(12)),
                     row(t, "VERIFY", "PASS", address=addr(13)),
                     row(t, "EXECUTE", "BUY", address=addr(13)),
                     skip(t, addr(14), "liquidity", "goplus=skipped"),
                     row(t, "EXIT", "SELL", "hard_stop", book="baseline", address=addr(14))])  # not the desk
        c, _, _ = self.cards()
        self.assertEqual(c["kills"]["gate"], {"liquidity": 3, "missing:liq_usd": 1, "fetch_error_x3": 1,
                                              "missing:age_min": 1})
        self.assertEqual(c["kills"]["safety"], {"goplus_timeout": 1, "mint_open": 1, "lp_locked": 1,
                                                "missing:dev_pct": 1, "too_old": 1})
        # scan 14 tokens; gate kills 1 2 3 4 5 14 -> 8 left; 1 PASS; 1 desk BUY. WATCH and RETRY are not kills.
        self.assertEqual({k: c["funnel"][k] for k in report.FUNNEL}, {"scan": 14, "gate": 8, "safety": 1, "buy": 1})

    def test_kill_log_newest_first_last_ten(self):
        self.put(D, [skip(f"2026-10-23T{h:02d}:00:00Z", addr(h), "liquidity", "goplus=skipped", f"T{h}")
                     for h in range(12)] + [row("2026-10-23T12:00:00Z", "VERIFY", "WATCH", "goplus_not_ready",
                                                address=addr(99))])
        log = self.cards()[0]["kill_log"]
        self.assertEqual(len(log), 10)
        self.assertEqual([x["symbol"] for x in log], [f"T{h}" for h in range(11, 1, -1)])
        self.assertEqual(log[0], {"ts": "2026-10-23T11:00:00Z", "symbol": "T11", "stage": "gate",
                                  "reason": "liquidity"})

    def test_48_hour_buckets_missing_hours_zero(self):
        self.put("2026-10-21", [tick_row("2026-10-21T23:58:00Z")])  # outside the window
        self.put("2026-10-22", [tick_row("2026-10-22T00:03:00Z"), tick_row("2026-10-22T00:08:00Z")])
        self.put(D, [tick_row("2026-10-23T23:58:00Z")])
        h = self.cards()[0]["ticks_per_hour"]
        self.assertEqual(len(h), 48)
        self.assertEqual((h[0], h[1], h[-1]), ({"hour": "2026-10-22T00", "n": 2}, {"hour": "2026-10-22T01", "n": 0},
                                               {"hour": "2026-10-23T23", "n": 1}))
        self.assertEqual(sum(x["n"] for x in h), 3)
        self.assertEqual([x["hour"] for x in h], sorted(x["hour"] for x in h))

    def test_tokens_latest_desk_row_per_token(self):
        self.put(D, [row("2026-10-23T01:00:00Z", "VERIFY", "WATCH", "goplus_not_ready", symbol="A", address=addr(1)),
                     row("2026-10-23T01:15:00Z", "VERIFY", "PASS", "", "lp_unknown", symbol="A", address=addr(1)),
                     row("2026-10-23T01:15:00Z", "EXECUTE", "BUY", "", "usd=12", symbol="A", address=addr(1)),
                     row("2026-10-23T02:00:00Z", "EXECUTE", "BUY", "", "desk=PASS", book="baseline",
                         symbol="A", address=addr(1)),
                     row("2026-10-23T03:00:00Z", "EXIT", "SELL", "hard_stop", symbol="A", address=addr(1)),
                     skip("2026-10-23T01:30:00Z", addr(2), "liquidity", "goplus=skipped", "B"),
                     row("2026-10-23T04:00:00Z", "EXECUTE", "BUY", book="baseline", symbol="C", address=addr(3)),
                     tick_row("2026-10-23T04:00:00Z")])
        tokens = self.cards()[1]
        self.assertEqual(tokens, [
            {"symbol": "B", "address": addr(2), "verdict": "SKIP", "reason": "liquidity", "checks": 1,
             "last": "2026-10-23T01:30:00Z", "lp_unknown": False},
            {"symbol": "A", "address": addr(1), "verdict": "BUY", "reason": "", "checks": 2,
             "last": "2026-10-23T01:15:00Z", "lp_unknown": False}])

    def test_lp_unknown_from_the_latest_row(self):
        self.put(D, [row("2026-10-23T01:00:00Z", "VERIFY", "SKIP", "top10_pct", "lp_unknown until=x", address=addr(1))])
        self.assertTrue(self.cards()[1][0]["lp_unknown"])

    def test_tokens_file_overwritten_not_appended(self):
        report.write_tokens(self.root, "2026-10-22", ROLL, [{"symbol": "OLD", "address": addr(1)}])
        report.write_tokens(self.root, D, ROLL, [{"symbol": "富贵", "address": addr(2)}, {"symbol": "X"}])
        f = json.loads((self.root / report.TOKENS).read_text(encoding="utf-8"))
        self.assertEqual(f, {"day": D, "as_of": "2026-10-24T00:03:00Z",
                             "tokens": [{"symbol": "富贵", "address": addr(2)}, {"symbol": "X"}]})
        report.write_tokens(self.root, D, ROLL, [])
        self.assertEqual(json.loads((self.root / report.TOKENS).read_text())["tokens"], [])


class TotalsTest(Files):
    def day_rows(self, d, n):
        return [skip(f"{d}T01:00:00Z", addr(i), "liquidity", "goplus=skipped") for i in range(n)]

    def test_backfill_from_since_with_the_files_there(self):
        self.put("2026-10-21", self.day_rows("2026-10-21", 5))  # before since: not counted
        self.put("2026-10-22", self.day_rows("2026-10-22", 2))
        self.put(D, self.day_rows(D, 3))
        t = report.since_totals(self.root, self.state(since="2026-10-22"), D)
        self.assertEqual((t["through"], t["funnel"]["scan"], t["kills"]["gate"]), (D, 5, {"liquidity": 5}))

    def test_idempotent(self):
        self.put("2026-10-22", self.day_rows("2026-10-22", 2))
        s = self.state(since="2026-10-22")
        s["totals"] = report.since_totals(self.root, s, "2026-10-22")
        self.put(D, self.day_rows(D, 3))
        once = report.since_totals(self.root, s, D)
        s["totals"] = once
        self.assertEqual(report.since_totals(self.root, s, D), once)          # a second call adds nothing
        self.assertEqual(report.since_totals(self.root, s, "2026-10-22"), once)  # an older day neither
        self.assertEqual(once["funnel"]["scan"], 5)
        # the stored totals are never changed in place
        self.assertEqual(s["totals"]["funnel"]["scan"], 5)

    def test_run_machine_twice_counts_once(self):
        self.put(D, self.day_rows(D, 3))
        s = self.state(machine_due=D)
        report.write(self.root, {"day": D})
        report.run_machine(self.root, ROLL, s, [])
        first = json.loads((self.root / report.REPORT).read_text())["machine"]
        s["machine_due"] = D  # a retry of the same day
        report.run_machine(self.root, ROLL, s, [])
        again = json.loads((self.root / report.REPORT).read_text())["machine"]
        self.assertEqual((first["funnel_since"], s["totals"]["funnel"]["scan"]), (again["funnel_since"], 3))

    def test_failed_write_leaves_totals_and_due(self):
        self.put(D, self.day_rows(D, 3))
        s = self.state(machine_due=D)
        report.write(self.root, {"day": D})
        with mock.patch.object(report, "write_tokens", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                report.run_machine(self.root, ROLL, s, [])
        self.assertEqual((s["machine_due"], s.get("totals")), (D, None))

    def test_report_for_another_day_is_refused(self):
        s = self.state(machine_due=D)
        report.write(self.root, {"day": "2026-10-22"})
        with self.assertRaises(ValueError):
            report.run_machine(self.root, ROLL, s, [])
        self.assertEqual(s["machine_due"], D)


class MachineTickTest(unittest.TestCase):
    """In the tick, with the adapter faked (same fakes as tests/test_tick.py TickTest)."""

    T = test_tick.TickTest
    setUp, tearDown, run_tick, heartbeats = T.setUp, T.tearDown, T.run_tick, T.heartbeats

    def rep(self):
        return json.loads((self.root / report.REPORT).read_text())

    def test_on_the_roll(self):
        self.found = [addr(1), addr(2)]
        self.facts[addr(1)] = good(1)
        self.facts[addr(2)] = dict(good(2), liq_usd=1)
        self.run_tick(0)
        self.run_tick(13 * 60)
        hb = detail(self.heartbeats()[-1])
        self.assertEqual((hb["report"], hb["machine"]), ("ok", "ok"))
        keys = list(hb)
        self.assertEqual(keys[keys.index("report") + 1], "machine")
        m = self.rep()["machine"]
        self.assertEqual({k: m["funnel"][k] for k in report.FUNNEL + ["exits_open"]},
                         {"scan": 2, "gate": 1, "safety": 1, "buy": 1, "exits_open": 1})
        self.assertEqual(m["kills"]["gate"], {"liquidity": 1})
        self.assertEqual(m["runs"]["ticks"], 1)
        tok = json.loads((self.root / report.TOKENS).read_text())
        self.assertEqual((tok["day"], [t["verdict"] for t in tok["tokens"]]), (D, ["SKIP", "BUY"]))
        self.assertEqual(report.load(self.root, T0)["totals"]["through"], D)
        self.run_tick(13 * 60 + 5)
        self.assertEqual(detail(self.heartbeats()[-1])["machine"], "none")  # once per roll

    def test_failure_keeps_money_cards_and_retries(self):
        self.found = [addr(1)]
        self.run_tick(0)
        with mock.patch.object(report, "machine", side_effect=KeyError("x")):
            with self.assertRaises(KeyError):  # the run ends red
                self.run_tick(13 * 60)
        hb = self.heartbeats()[-1]
        self.assertEqual((hb["reason"], detail(hb)["report"], detail(hb)["machine"]), ("", "ok", "error:KeyError"))
        self.assertEqual(detail(hb)["seen"], "1")  # trading went on
        r = self.rep()
        self.assertNotIn("machine", r)
        self.assertEqual((r["day"], r["desk"]["value_usd"]), (D, 50.0))
        self.assertFalse((self.root / report.TOKENS).exists())
        s = report.load(self.root, T0)
        self.assertEqual((s["machine_due"], s["due"], s.get("totals")), (D, "", None))
        self.run_tick(13 * 60 + 5)
        self.assertEqual(detail(self.heartbeats()[-1])["machine"], "ok")
        self.assertEqual(self.rep()["machine"]["funnel"]["scan"], 1)
        self.assertEqual(report.load(self.root, T0)["totals"]["funnel"]["scan"], 1)  # counted once

    def test_report_failure_waits_for_the_report(self):
        self.run_tick(0)
        with mock.patch.object(report, "build", side_effect=KeyError("x")):
            with self.assertRaises(KeyError):
                self.run_tick(13 * 60)
        self.assertEqual(detail(self.heartbeats()[-1])["machine"], "none")
        self.run_tick(13 * 60 + 5)
        hb = detail(self.heartbeats()[-1])
        self.assertEqual((hb["report"], hb["machine"]), ("ok", "ok"))

    def test_by_hand_today_so_far(self):
        self.found = [addr(1)]
        self.run_tick(0)  # 12:00 UTC
        with mock.patch.object(report, "utcnow", return_value=T0.replace(minute=30)), \
                contextlib.redirect_stdout(io.StringIO()):
            report.main([str(self.root)])
        m = self.rep()["machine"]
        self.assertEqual((m["runs"]["ticks"], m["runs"]["expected"]), (1, 150))  # 12:03 ... 12:28 -> 150 slots
        self.assertEqual(m["ticks_per_hour"][-1], {"hour": "2026-10-23T12", "n": 1})
        self.assertEqual(json.loads((self.root / report.TOKENS).read_text())["day"], D)
        self.assertNotIn("totals", report.load(self.root, T0))  # by hand never stores totals


class DeskUnchangedTest(unittest.TestCase):
    """The same ticks (one roll) with machine cards working, skipped or broken: desk rows, state and keys equal."""

    T = test_baseline.DeskUnchangedTest
    run_tick, ticks, assert_desk_same = T.run_tick, T.ticks, T.assert_desk_same
    keys = tick.COUNTS + ["goplus", "requests"]

    def noop(self, root, now, s, books):
        return {"day": s["machine_due"], "funnel": dict.fromkeys(report.FUNNEL, 0)}

    def test_desk_identical(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on = self.ticks(Path(a), True)
            off = self.ticks(Path(b), True, [mock.patch.object(report, "run_machine", self.noop)])
            self.assertTrue((Path(a) / report.TOKENS).exists())
        self.assertEqual((on[3], off[3]), ([], []))
        self.assert_desk_same(on, off, self.keys)
        self.assertEqual([hb["machine"] for hb in on[0]], ["none", "none", "ok"])
        # the baseline is untouched too
        self.assertEqual([r for r in on[1] if r["book"] == "baseline"], [r for r in off[1] if r["book"] == "baseline"])

    def test_broken_machine_leaves_the_desk_identical(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            on = self.ticks(Path(a), True, [mock.patch.object(report, "machine", side_effect=RuntimeError("x"))])
            off = self.ticks(Path(b), True, [mock.patch.object(report, "run_machine", self.noop)])
        self.assert_desk_same(on, off, self.keys)
        self.assertEqual([type(e) for e in on[3]], [RuntimeError])  # the roll tick ends red
        self.assertEqual([hb["machine"] for hb in on[0]], ["none", "none", "error:RuntimeError"])
        self.assertEqual([hb["report"] for hb in on[0]], ["none", "none", "ok"])


if __name__ == "__main__":
    unittest.main()
