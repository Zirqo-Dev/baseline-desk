"""Offline tests for BL-29: tools/replay.py on small fixture CSVs. No network, no writes."""
import contextlib
import csv
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import friction, labels
from core.book import TASK_FIELDS, TRADE_FIELDS
from tools import replay

D1, D2 = "2026-11-01", "2026-11-02"
OLD = labels.FIELDS[:-1]  # 16 columns, before BL-28


def a(i):
    return f"0x{i:040x}"


def keys(qliq="na", s24="0", liq=80000):
    return f"b1=1 s1=0 b24=3 s24={s24} liq={liq} dex=uniswap:v4 qliq={qliq}"


def task(ts, stage, action, address="", reason="", detail="", book="desk", symbol="S"):
    return {"ts": ts, "book": book, "stage": stage, "symbol": symbol, "address": address, "action": action,
            "reason": reason, "detail": detail}


def label(address, first_seen, h, age, chg="10.0", gone="0", s1="1", qliq=None):
    r = {"ts": "x", "symbol": "S", "address": address, "first_seen": first_seen, "horizon": f"{h}h", "age_min": age,
         "first_price": "1.0", "price": "1.1", "chg_pct": chg, "liq": "80000", "pair_b1": "1", "pair_s1": s1,
         "pair_b24": "2", "pair_s24": "3", "dex": "uniswap:v4", "gone": gone}
    if qliq is not None:
        r["qliq"] = qliq
    return r


class Data(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, d, name, fields, rows):
        p = self.root / f"data/daily/{d}/{name}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(replay.main(["--root", str(self.root), *args]), 0)
        return out.getvalue()


class BucketTest(Data):
    def test_bucket_edges(self):
        self.assertEqual([replay.qliq_bucket(x) for x in ("na", "", "0", "999", "1000", "4999", "5000", "24999",
                                                          "25000", "1e9")],
                         ["na", "na", "<1k", "<1k", "1k-5k", "1k-5k", "5k-25k", "5k-25k", ">=25k", ">=25k"])
        self.assertEqual([replay.sells_bucket(x) for x in ("na", "0", "1", "9", "10", "400")],
                         ["na", "0", "1-9", "1-9", ">=10", ">=10"])

    def test_first_row_with_qliq_decides_and_gate_kills_are_out(self):
        t = "2026-11-01T01:00:00Z"
        rows = [task(t, "VERIFY", "WATCH", a(1), "goplus_not_ready", f"first_seen={t} checks=1 b1=0 s1=0"),  # no qliq yet
                task("2026-11-01T01:15:00Z", "VERIFY", "WATCH", a(1), "goplus_not_ready",
                     f"first_seen={t} checks=2 " + keys(qliq="700", s24="0")),
                task("2026-11-01T01:30:00Z", "VERIFY", "PASS", a(1), "", f"first_seen={t} " + keys(qliq="90000", s24="40")),
                task(t, "VERIFY", "SKIP", a(2), "liquidity", "goplus=skipped until=x"),          # gate kill
                task(t, "VERIFY", "SKIP", a(3), "honeypot", "until=x"),                         # past the gate, no qliq
                task(t, "VERIFY", "SKIP", a(4), "mint_open", "until=x " + keys(qliq="3000", s24="12")),
                task(t, "VERIFY", "WATCH", a(5), "goplus_not_ready", "first_seen=x " + keys(), book="baseline")]
        s = replay.sightings(rows)
        self.assertEqual(set(s), {(a(1), t), (a(3), t), (a(4), t)})  # key: address + first_seen (else the row ts)
        self.assertEqual((s[(a(1), t)]["qb"], s[(a(1), t)]["sb"], s[(a(1), t)]["qliq"]), ("<1k", "0", "700"))
        self.assertEqual((s[(a(3), t)]["qb"], s[(a(3), t)]["sb"]), ("na", "na"))
        self.assertEqual((s[(a(4), t)]["qb"], s[(a(4), t)]["sb"]), ("1k-5k", ">=10"))

    def test_resighting_is_a_new_unit(self):
        rows = [task("2026-11-01T01:00:00Z", "VERIFY", "SKIP", a(1), "mint_open", "until=x " + keys("100")),
                task("2026-11-02T02:00:00Z", "VERIFY", "SKIP", a(1), "mint_open", "until=x " + keys("30000"))]
        s = replay.sightings(rows)
        self.assertEqual(sorted(x["qb"] for x in s.values()), ["<1k", ">=25k"])


class LabelTest(Data):
    def test_thirty_minute_filter(self):
        rows = [label(a(1), "f", 1, str(age)) for age in (29, 30, 60, 90, 91, 171)] + [label(a(2), "f", 6, "330"),
                                                                                       label(a(3), "f", 24, "1471")]
        self.assertEqual([replay.on_time(r) for r in rows], [False, True, True, True, False, False, True, False])
        self.assertFalse(replay.on_time(label(a(1), "f", 1, "na")))

    def test_16_and_17_column_files(self):
        t = "2026-11-01T01:00:00Z"
        self.write(D1, "tasks", TASK_FIELDS, [task(t, "VERIFY", "SKIP", a(1), "mint_open", "until=x " + keys("500")),
                                             task(t, "VERIFY", "SKIP", a(2), "mint_open", "until=x " + keys("500"))])
        self.write(D1, "outcomes", OLD, [label(a(1), t, 1, "60", chg="-60.0")])
        self.write(D2, "outcomes", labels.FIELDS, [label(a(2), t, 6, "362", chg="5.0", qliq="12")])
        win, tasks, trades, outcomes = replay.load(self.root, D1, D1)
        self.assertEqual(win, [D1])  # labels still read from the next day's 17-column file
        idx = replay.label_index(outcomes)
        self.assertEqual(set(idx), {(a(1), t, "1h"), (a(2), t, "6h")})
        self.assertNotIn("qliq", idx[(a(1), t, "1h")])
        self.assertEqual(idx[(a(2), t, "6h")]["qliq"], "12")


class CellTest(unittest.TestCase):
    def test_under_twenty_is_too_small(self):
        self.assertEqual(replay.cell([label(a(1), "f", 1, "60")] * 19), {"n": 19, "small": True})

    def test_stats_and_na(self):
        rows = ([label(a(i), "f", 1, "60", chg="-60.0", s1="0") for i in range(5)]
                + [label(a(i), "f", 1, "60", chg="20.0", s1="3") for i in range(5, 15)]
                + [label(a(i), "f", 1, "60", chg="na", s1="na") for i in range(15, 18)]   # first_price unknown
                + [label(a(i), "f", 1, "60", chg="na", gone="1", s1="na") for i in range(18, 20)])
        c = replay.cell(rows)
        self.assertEqual((c["n"], c["n_chg"], c["med"]), (20, 15, 20.0))
        self.assertEqual((c["gone"], round(c["le50"], 2), round(c["s1"], 2)), (10.0, 33.33, 66.67))

    def test_all_na(self):
        c = replay.cell([label(a(i), "f", 1, "60", chg="na", s1="na") for i in range(20)])
        self.assertEqual((c["n_chg"], c["med"], c["le50"], c["s1"]), (0, None, None, None))


class RepriceTest(Data):
    def buy(self, liq, book="desk", ts="2026-11-01T01:00:00Z", usd=5.0, price=0.01, launchpad="other"):
        fill, pct = friction.buy_fill(price, liq, launchpad)
        return {"ts": ts, "book": book, "side": "BUY", "symbol": "S", "address": a(1), "qty": str(usd / fill),
                "usd": str(usd), "intended_price": str(price), "fill_price": str(fill), "friction_pct": str(pct),
                "liq_usd": str(liq), "launchpad": launchpad, "reason": "entry", "pnl_usd": "", "note": ""}

    def sell(self, buy, share, quote, liq, reason, ts="2026-11-01T02:00:00Z"):
        fill, pct = friction.sell_fill(quote, liq, buy["launchpad"])
        qty = float(buy["qty"]) * share
        return {**buy, "ts": ts, "side": "SELL", "qty": str(qty), "usd": str(qty * fill), "intended_price": str(quote),
                "fill_price": str(fill), "friction_pct": str(pct), "liq_usd": str(liq), "reason": reason,
                "pnl_usd": str(qty * fill - share * float(buy["usd"]))}

    def test_matches_core_friction(self):
        b = self.buy(150_000, launchpad="clanker")
        sells = [self.sell(b, 0.33, 0.014, 150_000, "ladder_1"), self.sell(b, 0.67, 0.009, 150_000, "trail")]
        logged = sum(float(s["pnl_usd"]) for s in sells)
        self.assertAlmostEqual(replay.reprice(b, sells, "150000")["pnl"], logged, places=9)  # same depth: same P&L
        r = replay.reprice(b, sells, "2000")
        fill_in, pct_in = friction.buy_fill(0.01, 2000, "clanker")
        qty = 5.0 / fill_in
        want = sum(sh * qty * friction.sell_fill(q, 2000, "clanker")[0] - sh * 5.0 for sh, q in ((0.33, 0.014), (0.67, 0.009)))
        self.assertAlmostEqual(r["pnl"], want, places=9)
        self.assertEqual(pct_in, 6.0)  # 5% thin + 1% Clanker pool fee
        self.assertLess(r["pnl"], logged)
        self.assertIsNone(replay.reprice(b, sells, "na"))

    def test_trade_table_entry_qliq_size_and_counts(self):
        t1, t2, t3 = "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z", "2026-11-01T05:00:00Z"
        self.write(D1, "tasks", TASK_FIELDS, [
            task(t1, "VERIFY", "PASS", a(1), "", keys(qliq="40")),                                # desk entry qliq
            task(t2, "EXECUTE", "BUY", a(2), "", "usd=5.00 fill=1 " + keys(qliq="20"), book="baseline"),
        ])
        b1, b2, b3 = self.buy(90_000, ts=t1), dict(self.buy(90_000, "baseline", t2), address=a(2)), \
            dict(self.buy(200_000, "baseline", t3), address=a(3))
        trades = [b1, self.sell(b1, 1.0, 0.012, 90_000, "time_stop"), b2, b3]
        self.write(D1, "trades", TRADE_FIELDS, trades)
        rows = replay.trade_table(*replay.load(self.root, D1, D1)[1:3], {D1})
        self.assertEqual([(r["book"], r["qliq"], r["closed"]) for r in rows],
                         [("desk", "40", True), ("baseline", "20", False), ("baseline", "na", False)])
        self.assertEqual((rows[0]["size_pct"], rows[1]["size_pct"], rows[2]["size_pct"]), (12.5, 25.0, None))
        self.assertIsNone(rows[2]["pnl_new"])
        out = self.run_cli("--from", D1, "--to", D1)
        self.assertIn("trades: 3, with qliq: 2; size above 10% of qliq: 2; size above 100% of qliq: 0", out)
        self.assertIn("evidence only, no rule change", out)
        self.assertIn("not a friction model", out)
        self.assertIn("n too small", out)


class SafetyTest(Data):
    def snapshot(self, root):
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in Path(root).rglob("*") if p.is_file()}

    def test_no_network_and_no_writes(self):
        t = "2026-11-01T01:00:00Z"
        self.write(D1, "tasks", TASK_FIELDS, [task(t, "VERIFY", "PASS", a(1), "", keys(qliq="40"))])
        self.write(D1, "outcomes", OLD, [label(a(1), t, 1, "61")])
        before = self.snapshot(self.root)

        def no_net(*a, **k):
            raise AssertionError("network used")

        with mock.patch.object(socket, "socket", no_net), mock.patch.object(socket, "create_connection", no_net):
            out = self.run_cli()
        self.assertIn("Table 3", out)
        self.assertEqual(self.snapshot(self.root), before)

    def test_never_imports_the_tick_and_core_never_imports_tools(self):
        code = "import sys, tools.replay; print('core.tick' in sys.modules)"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=Path(replay.__file__).resolve().parents[1], env=dict(os.environ))
        self.assertEqual(r.stdout.strip(), "False", r.stderr)
        core = Path(replay.__file__).resolve().parents[1] / "core"
        self.assertFalse([p.name for p in core.glob("*.py") if "tools" in p.read_text()])


if __name__ == "__main__":
    unittest.main()
