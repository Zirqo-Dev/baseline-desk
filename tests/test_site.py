"""Offline tests for BL-13: site/index.html reads only keys core/report.py writes, and fetches only the two data URLs."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from core import config, report
from core.book import TRADE_FIELDS, Book, _append
from tests.test_machine import D, ROLL, Files, row, skip, tick_row
from tests.test_tick import addr

PAGE = Path(__file__).resolve().parent.parent / "site" / "index.html"
RAW = "https://raw.githubusercontent.com/Zirqo-Dev/baseline-desk/main/data/"
URLS = {"REPORT_URL": RAW + "report.json", "TOKENS_URL": RAW + "tokens_24h.json"}


def trade(t, book, side, a, usd, reason, pnl="", intended="1.0", fill="0.97", fric="3.0"):
    return {"ts": t, "book": book, "side": side, "symbol": "S", "address": a, "qty": "5", "usd": usd,
            "intended_price": intended, "fill_price": fill, "friction_pct": fric, "liq_usd": "60000",
            "launchpad": "other", "reason": reason, "pnl_usd": pnl, "note": ""}


def script():
    return re.search(r"<script>(.*)</script>", PAGE.read_text(encoding="utf-8"), re.S).group(1)


def reads(helper):
    """Every key path the page passes to a reader: R('a.b') / M('a.b') or B(x, 'k')."""
    pat = rf"\b{helper}\('([^']+)'\)" if helper in ("R", "M") else rf"\b{helper}\(\w+, '([^']+)'\)"
    return set(re.findall(pat, script()))


def has(obj, path):
    for k in path.split("."):
        if not isinstance(obj, dict) or k not in obj:
            return False
        obj = obj[k]
    return True


class PageKeysTest(Files):
    """Build report.json and tokens_24h.json the way the day roll does, then check every key the page reads."""

    def setUp(self):
        super().setUp()
        t = "2026-10-23T01:00:00Z"
        self.put(D, [tick_row("2026-10-23T00:03:00Z", 2.0), tick_row(t, 3.0, seen=5),
                     skip(t, addr(1), "liquidity", "goplus=skipped"), skip(t, addr(2), "goplus_timeout"),
                     row(t, "VERIFY", "WATCH", "goplus_not_ready", address=addr(3), symbol="W"),
                     row(t, "VERIFY", "PASS", "", "lp_unknown", address=addr(4), symbol="P"),
                     row(t, "EXECUTE", "BUY", "", "usd=5.00", address=addr(4), symbol="P")])
        for r in [trade(t, "desk", "BUY", addr(4), "5.0", "entry"),
                  trade("2026-10-23T02:00:00Z", "desk", "SELL", addr(4), "1.7", "ladder_1", "0.7"),
                  trade("2026-10-23T03:00:00Z", "desk", "SELL", addr(4), "2.5", "hard_stop", "-0.8", "0.8", "0.75"),
                  trade(t, "baseline", "BUY", addr(5), "5.0", "entry"),
                  trade("2026-10-23T04:00:00Z", "baseline", "SELL", addr(5), "4.6", "time_stop", "-0.4")]:
            _append(self.root / config.TRADES.format(day=D), TRADE_FIELDS, r)
        s = report.load(self.root, ROLL)
        books = [Book(n, self.root) for n in report.BOOKS]
        report.on_roll(s, books, D)
        report.run_due(self.root, ROLL, s, books)
        report.run_machine(self.root, ROLL, s, books)
        self.rep = json.loads((self.root / report.REPORT).read_text())
        self.tok = json.loads((self.root / report.TOKENS).read_text())

    def missing(self, obj, keys):
        return sorted(k for k in keys if not has(obj, k))

    def test_report_keys(self):
        keys = reads("R")
        self.assertIn("desk_lead_usd", keys)
        self.assertEqual(self.missing(self.rep, keys), [])

    def test_machine_keys(self):
        keys = reads("M")
        self.assertIn("last_tick.secs", keys)
        self.assertEqual(self.missing(self.rep["machine"], keys), [])

    def test_book_keys_both_books(self):
        keys = reads("B")
        self.assertIn("stop_slip_pct", keys)
        for b in report.BOOKS:
            self.assertEqual(self.missing(self.rep[b], keys), [], b)

    def test_list_entry_keys(self):
        m = self.rep["machine"]
        for helper, items in [("EQ", self.rep["desk"]["equity_daily"] + self.rep["baseline"]["equity_daily"]),
                              ("HR", m["ticks_per_hour"]), ("KL", m["kill_log"]), ("TK", self.tok["tokens"])]:
            keys = reads(helper)
            self.assertTrue(keys and items, helper)
            for x in items:
                self.assertEqual(self.missing(x, keys), [], helper)

    def test_tokens_file_keys(self):
        self.assertEqual(self.missing(self.tok, reads("TF")), [])

    def test_exit_rules_match(self):
        page = set(re.findall(r"\['(\w+)', '[^']+', [01]\]", script()))
        self.assertEqual(page, set(report.RULES))

    def test_gate_kills_match(self):
        js = re.search(r"const GATE = new Set\(\[(.*?)\]\)", script(), re.S).group(1)
        self.assertEqual(set(re.findall(r"'([^']+)'", js)), report.GATE_KILLS)


class PageFetchTest(unittest.TestCase):
    """The page fetches only the two raw.githubusercontent.com data URLs; nothing can change where from."""

    def test_two_data_urls_only(self):
        js = script()
        consts = dict(re.findall(r"const (\w+_URL) = '([^']+)';", js))
        self.assertEqual(consts, URLS)
        self.assertEqual(len(re.findall(r"\bfetch\(", js)), 1)
        self.assertRegex(js, r"await fetch\(url, ")
        self.assertEqual(set(re.findall(r"\bload\((\w+)\)", js)), set(URLS) | {"url"})  # url: the definition
        self.assertEqual(sorted(re.findall(r"\bload\(", js)), ["load("] * 3)  # the function + two calls

    def test_no_other_url_or_override(self):
        page = PAGE.read_text(encoding="utf-8")
        urls = set(re.findall(r"https?://[^'\"\s)<>]+", page))
        self.assertEqual(urls - set(URLS.values()), {"https://dexscreener.com/base/", "http://www.w3.org/2000/svg"})
        for bad in ["XMLHttpRequest", "import(", "location", "URLSearchParams", "localStorage", "sessionStorage",
                    "EventSource", "WebSocket", "sendBeacon", "<script src", "<link", "@import", "<iframe",
                    "url(", "eval(", "document.cookie"]:
            self.assertNotIn(bad, page, bad)
        self.assertEqual(page.count("<script"), 1)

    def test_no_sample_numbers(self):
        page = PAGE.read_text(encoding="utf-8")
        for bad in ["SAMPLE", "s-only", "m-sample", "47.62", "38.95"]:
            self.assertNotIn(bad, page, bad)

    def test_mock_kept_outside_site(self):
        root = PAGE.parent.parent
        self.assertTrue((root / "docs/mock/bl13-mock.html").exists())
        self.assertEqual([p.name for p in PAGE.parent.iterdir()], ["index.html"])


if __name__ == "__main__":
    unittest.main()
