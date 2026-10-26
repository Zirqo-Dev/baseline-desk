import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.leads import Leads
from tests.test_verify import GOOD

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
A = "0xAbC0000000000000000000000000000000000001"
WAIT = {k: v for k, v in GOOD.items() if k not in ("mint_open", "honeypot")}
WAIT["goplus_pending"] = ["mint_open", "honeypot"]


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class LeadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.leads = Leads(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        out = []
        for p in sorted((self.root / "data/daily").glob("*/tasks.csv")):
            with p.open() as fh:
                out += list(csv.DictReader(fh))
        return out

    def test_watch_then_ready(self):
        self.assertTrue(self.leads.due(A, T0))
        self.assertEqual(self.leads.record(A, WAIT, T0), "goplus_not_ready")
        self.assertFalse(self.leads.due(A, at(14)))
        self.assertTrue(self.leads.due(A, at(15)))
        self.assertEqual(self.leads.record(A, WAIT, at(15)), "goplus_not_ready")
        self.assertEqual(self.leads.record(A, GOOD, at(30)), "")
        rows = self.rows()
        self.assertEqual([r["action"] for r in rows], ["WATCH", "WATCH", "PASS"])
        self.assertTrue(all(r["stage"] == "VERIFY" for r in rows))
        self.assertIn("first_seen=2026-10-23T12:00:00Z", rows[1]["detail"])
        self.assertIn("checks=2", rows[1]["detail"])
        self.assertIn("pending=mint_open,honeypot", rows[0]["detail"])
        self.assertIn("goplus_ready_at=2026-10-23T12:30:00Z", rows[2]["detail"])
        self.assertIn("first_seen=2026-10-23T12:00:00Z", rows[2]["detail"])
        self.assertEqual(self.leads.s["watch"], {})

    def test_timeout_after_3h(self):
        for m in range(0, 180, 15):
            self.assertEqual(self.leads.record(A, WAIT, at(m)), "goplus_not_ready")
        self.assertEqual(self.leads.record(A, WAIT, at(180)), "goplus_timeout")
        self.assertEqual(self.leads.skipped(A, at(181)), "goplus_timeout")
        last = self.rows()[-1]
        self.assertEqual((last["action"], last["reason"]), ("SKIP", "goplus_timeout"))
        self.assertIn("checks=13", last["detail"])
        self.assertEqual(len(self.rows()), 13)  # one VERIFY row per check

    def test_ready_just_before_timeout(self):
        self.leads.record(A, WAIT, T0)
        self.assertEqual(self.leads.record(A, GOOD, at(179)), "")

    def test_liquidity_drop_while_watched_is_final(self):
        self.leads.record(A, WAIT, T0)
        self.assertEqual(self.leads.record(A, dict(WAIT, liq_usd=40_000), at(15)), "liquidity")
        self.assertEqual(self.leads.skipped(A, at(16)), "liquidity")
        self.assertNotIn(A.lower(), self.leads.s["watch"])

    def test_goplus_skipped_in_detail(self):
        dex_only = {"symbol": "LOW", "price": 0.001, "liq_usd": 1_000, "age_min": 60, "sells_1h": 4,
                    "pair": "0xp", "fetch_errors": [], "goplus_skipped": True}
        self.assertEqual(self.leads.record(A, dex_only, T0), "liquidity")
        row = self.rows()[-1]
        self.assertEqual((row["action"], row["reason"]), ("SKIP", "liquidity"))
        self.assertTrue(row["detail"].startswith("goplus=skipped until="))
        self.leads.record("0x2", {"fetch_errors": ["ds_pairs"], "goplus_skipped": True}, T0)
        self.assertEqual(self.rows()[-1]["detail"], "try=1 hosts=ds_pairs goplus=skipped")
        self.leads.record("0x3", dict(GOOD, liq_usd=1_000), T0)  # GoPlus was called: no tag
        self.assertNotIn("goplus=skipped", self.rows()[-1]["detail"])

    def test_watched_lead_liquidity_drop_settled_without_goplus(self):
        self.leads.record(A, WAIT, T0)
        dex_only = {k: WAIT[k] for k in ("symbol", "price", "age_min", "sells_1h")}
        dex_only.update(liq_usd=40_000, pair="0xp", fetch_errors=[], goplus_skipped=True)
        self.assertEqual(self.leads.record(A, dex_only, at(15)), "liquidity")
        row = self.rows()[-1]
        self.assertEqual((row["action"], row["reason"]), ("SKIP", "liquidity"))
        self.assertIn("first_seen=2026-10-23T12:00:00Z checks=2 goplus=skipped until=", row["detail"])
        self.assertNotIn("goplus_ready_at", row["detail"])  # GoPlus was not asked, so not "ready"
        self.assertNotIn(A.lower(), self.leads.s["watch"])
        self.assertFalse(self.leads.due(A, at(30)))

    def test_dexscreener_gap_while_watched_is_final(self):
        self.leads.record(A, WAIT, T0)
        f = dict(WAIT)
        del f["price"]
        self.assertEqual(self.leads.record(A, f, at(15)), "missing:price")
        self.assertFalse(self.leads.due(A, at(30)))

    def test_final_skip_lasts_24h(self):
        self.assertEqual(self.leads.record(A, dict(GOOD, top10_pct=50), T0), "top10_pct")
        self.assertEqual(self.leads.skipped(A, at(24 * 60 - 1)), "top10_pct")
        self.assertFalse(self.leads.due(A, at(24 * 60 - 1)))
        self.assertEqual(self.leads.skipped(A, at(24 * 60)), "")
        self.assertTrue(self.leads.due(A, at(24 * 60)))
        row = self.rows()[-1]
        self.assertIn("until=2026-10-24T12:00:00Z", row["detail"])

    def test_state_kept_beside_books(self):
        state = self.root / "data/state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"desk": {"cash": 50.0}}))
        Leads(self.root).record(A, dict(GOOD, sells_1h=0), T0)
        saved = json.loads(state.read_text())
        self.assertEqual(saved["desk"], {"cash": 50.0})
        self.assertEqual(saved["leads"]["skips"][A.lower()]["reason"], "nobody_can_sell")
        self.assertEqual(Leads(self.root).skipped(A, at(1)), "nobody_can_sell")

    def test_fetch_error_retried_then_final_after_3(self):
        bad = dict(GOOD, fetch_errors=["goplus"])
        self.assertEqual(self.leads.record(A, bad, T0), "fetch_error")
        self.assertTrue(self.leads.due(A, at(5)))  # not locked: retried next run
        self.assertEqual(self.leads.record(A, bad, at(5)), "fetch_error")
        self.assertEqual(self.leads.record(A, bad, at(10)), "fetch_error_x3")
        self.assertEqual(self.leads.skipped(A, at(11)), "fetch_error_x3")
        self.assertFalse(self.leads.due(A, at(24 * 60 + 9)))
        rows = self.rows()
        self.assertEqual([(r["action"], r["reason"]) for r in rows],
                         [("RETRY", "fetch_error"), ("RETRY", "fetch_error"), ("SKIP", "fetch_error_x3")])
        self.assertIn("try=2", rows[1]["detail"])
        self.assertIn("hosts=goplus", rows[0]["detail"])

    def test_goplus_code_logged_in_retry_and_final_rows(self):
        bad = dict(GOOD, fetch_errors=["goplus"], goplus_code=4029)
        for m in (0, 5, 10):
            self.leads.record(A, bad, at(m))
        rows = self.rows()
        self.assertEqual([r["action"] for r in rows], ["RETRY", "RETRY", "SKIP"])
        for r in rows:
            self.assertIn("goplus_code=4029", r["detail"].split())

    def test_goplus_partial_is_final_24h_one_skip_row(self):
        part = dict(WAIT, goplus_partial=True)
        self.leads.record(A, WAIT, T0)  # watched first
        self.assertEqual(self.leads.record(A, part, at(15)), "goplus_partial")
        self.assertNotIn(A.lower(), self.leads.s["watch"])
        self.assertNotIn(A.lower(), self.leads.s["retry"])
        self.assertEqual(self.leads.skipped(A, at(24 * 60 + 14)), "goplus_partial")
        self.assertTrue(self.leads.due(A, at(24 * 60 + 15)))
        last = self.rows()[-1]
        self.assertEqual((last["action"], last["reason"]), ("SKIP", "goplus_partial"))
        self.assertIn("until=2026-10-24T12:15:00Z", last["detail"])

    def test_fetch_error_count_resets_after_a_clean_check(self):
        bad = dict(GOOD, fetch_errors=["ds_pairs"])
        self.leads.record(A, bad, T0)
        self.leads.record(A, bad, at(5))
        self.assertEqual(self.leads.record(A, GOOD, at(10)), "")
        self.assertEqual(self.leads.record(A, bad, at(15)), "fetch_error")
        self.assertIn("try=1", self.rows()[-1]["detail"])

    def test_fetch_error_keeps_a_watched_lead_watched(self):
        self.leads.record(A, WAIT, T0)
        self.assertEqual(self.leads.record(A, dict(WAIT, fetch_errors=["goplus"]), at(15)), "fetch_error")
        self.assertIn(A.lower(), self.leads.s["watch"])
        self.assertTrue(self.leads.due(A, at(20)))
        self.assertIn("first_seen=2026-10-23T12:00:00Z", self.rows()[-1]["detail"])
        self.assertEqual(self.leads.record(A, GOOD, at(20)), "")


if __name__ == "__main__":
    unittest.main()
