"""Offline tests for adapters/base.py, built from the BL-6 fixtures. No network."""
import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from adapters import base
from core.verify import hard_skip

FIX = Path(__file__).resolve().parent / "fixtures"
FRESH = "0x674c978b67927a8b89d53215d9e5fbce5b383d19"
OLDER = "0x532f27101965dd16442e59d40670faf5ebb142e4"
NOW = datetime(2026, 10, 23, 11, 30, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / f"bl6_{name}.json").read_text())


class FakeClient:
    """Answers by URL fragment from a dict; FETCH_ERROR (429/timeout) for anything else."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, bucket, url, headers=None):
        self.calls.append((bucket, url))
        for frag, body in self.routes.items():
            if frag in url:
                return copy.deepcopy(body)
        return base.FETCH_ERROR


def fresh_pairs(liq=60_000):
    """The asci pairs with liquidity over the minimum, so the BL-20 gate lets GoPlus run."""
    pairs = load("dexscreener_fresh")
    pairs[0]["liquidity"]["usd"] = liq
    return pairs


def gt_pool(created, name="X / WETH"):
    return {"data": {"attributes": {"pool_created_at": created, "name": name}}}


def gp_with(address, **fields):
    return {"code": 1, "message": "OK", "result": {address: fields}}


class MappingTest(unittest.TestCase):
    def test_fresh_token_waits_on_goplus(self):
        c = FakeClient({"token-pairs": fresh_pairs(), "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual(f["symbol"], "asci")
        self.assertAlmostEqual(f["price"], 0.0002443)
        self.assertEqual(f["liq_usd"], 60_000)
        self.assertEqual(f["sells_1h"], 92)
        self.assertAlmostEqual(f["age_min"], (NOW.timestamp() * 1000 - 1792752795000) / 60000)
        self.assertEqual(f["pair"], "0xd69e052b412840909dda5717dc3f7e0b5fa1a79dcaa1050717a6a51897707771")
        self.assertEqual(f["launchpad"], "other")  # v4 pool, but no fee known yet
        for k in ["mint_open", "honeypot", "buy_tax_pct", "top10_pct", "risk_flags"]:
            self.assertNotIn(k, f)
            self.assertIn(k, f["goplus_pending"])
        self.assertEqual(len(c.calls), 2)  # pairCreatedAt present: no GeckoTerminal call
        self.assertNotIn("goplus_skipped", f)
        self.assertEqual(hard_skip(f), "goplus_not_ready")
        # GoPlus-only gap, but under the liquidity minimum: final
        self.assertEqual(hard_skip(dict(f, liq_usd=9493.9)), "liquidity")

    def test_older_token_mapping(self):
        c = FakeClient({"token-pairs": load("dexscreener_older"), "gopluslabs": load("goplus_older"),
                        "geckoterminal.com/api/v2/networks/base/pools/": gt_pool("2024-02-29T12:00:00Z")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["symbol"], "BRETT")
        self.assertEqual(f["sells_1h"], 14)
        self.assertEqual(f["mint_open"], False)
        self.assertEqual(f["honeypot"], False)
        self.assertEqual(f["freeze_or_blacklist"], False)
        self.assertEqual(f["buy_tax_pct"], 0)
        self.assertEqual(f["dev_pct"], 0)
        self.assertEqual(f["risk_flags"], ["honeypot_with_same_creator"])  # log-only
        self.assertEqual(f["launchpad"], "other")
        self.assertEqual(f["goplus_pending"], [])
        # GoPlus lists 28 pools: its LP holders can't be tied to our Aerodrome pair
        self.assertNotIn("lp_locked_pct", f)
        self.assertEqual(hard_skip(f), "too_old")  # BL-21: unknown LP lock goes on to the other rules

    def test_goplus_down_is_a_fetch_error(self):
        c = FakeClient({"token-pairs": fresh_pairs()})  # GoPlus 429
        f = base.facts(FRESH, NOW, c)
        self.assertEqual(f["fetch_errors"], ["goplus"])
        self.assertEqual(hard_skip(f), "fetch_error")

    def test_goplus_rate_limit_in_body_is_a_fetch_error(self):
        c = FakeClient({"token-pairs": fresh_pairs(),
                        "gopluslabs": {"code": 4029, "message": "too many requests", "result": {}}})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual(hard_skip(f), "fetch_error")
        self.assertEqual(f["goplus_code"], 4029)

    def test_goplus_code_3_is_partial_not_a_fetch_error(self):
        # live answer for a 0xb2000000... token, 2026-10-23: HTTP 200, code 3, 10 fields, no safety data
        partial = {"code": 3, "message": "OK", "result": {FRESH: {
            "buy_tax": "", "sell_tax": "", "holder_count": "0", "is_in_dex": "1", "is_open_source": "1",
            "owner_address": "", "token_name": "Wizard Gang", "token_symbol": "WIZARD",
            "total_supply": "1000000000", "dex": []}}}
        c = FakeClient({"token-pairs": fresh_pairs(), "gopluslabs": partial})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual(f["fetch_errors"], [])
        self.assertTrue(f["goplus_partial"])
        self.assertNotIn("goplus_code", f)
        self.assertEqual(hard_skip(f), "goplus_partial")

    def test_dexscreener_down_is_a_fetch_error(self):
        c = FakeClient({"gopluslabs": load("goplus_older")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["fetch_errors"], ["ds_pairs"])
        self.assertEqual(hard_skip(f), "fetch_error")

    def test_no_pair_found_stays_missing(self):
        c = FakeClient({"token-pairs": [], "gopluslabs": load("goplus_older")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["fetch_errors"], [])
        self.assertEqual(hard_skip(f), "missing:symbol")

    def test_real_client_errors(self):
        c = base.Client(timeout=0.01, sleep=lambda s: None)
        self.assertIs(c.get("goplus", "http://127.0.0.1:9/nothing"), base.FETCH_ERROR)
        self.assertEqual(len(c.calls), 1)

    def test_http_status_classes(self):
        import io
        import urllib.error
        from unittest import mock
        c = base.Client(sleep=lambda s: None)
        for code, want in ((429, base.FETCH_ERROR), (500, base.FETCH_ERROR), (503, base.FETCH_ERROR), (404, None)):
            err = urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b""))
            with mock.patch("urllib.request.urlopen", side_effect=err):
                self.assertIs(c.get("gt", "http://x"), want, code)


class DexFirstTest(unittest.TestCase):
    """BL-20: GoPlus only when the DexScreener side hasn't already decided."""

    def gp_calls(self, c):
        return [url for _, url in c.calls if "gopluslabs" in url]

    def test_gate_fires_on_liquidity_no_goplus_call(self):
        c = FakeClient({"token-pairs": load("dexscreener_fresh"), "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual(self.gp_calls(c), [])
        self.assertEqual(len(c.calls), 1)
        self.assertTrue(f["goplus_skipped"])
        self.assertEqual(f["liq_usd"], 9493.9)
        self.assertEqual(f["pair"], "0xd69e052b412840909dda5717dc3f7e0b5fa1a79dcaa1050717a6a51897707771")
        self.assertEqual(hard_skip(f), "liquidity")

    def test_gate_fires_on_dexscreener_fetch_error(self):
        c = FakeClient({"gopluslabs": load("goplus_older")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual((self.gp_calls(c), f["fetch_errors"], hard_skip(f)), ([], ["ds_pairs"], "fetch_error"))

    def test_gate_fires_on_missing_dexscreener_fields_in_order(self):
        for field, want in (("symbol", "missing:symbol"), ("price", "missing:price"),
                            ("liq_usd", "missing:liq_usd"), ("sells_1h", "missing:sells_1h")):
            pairs = fresh_pairs()
            for p in pairs:
                if field == "symbol":
                    p["baseToken"]["symbol"] = ""
                elif field == "price":
                    p["priceUsd"] = None
                elif field == "liq_usd":
                    del p["liquidity"]
                else:
                    p["txns"]["h1"] = {}
            c = FakeClient({"token-pairs": pairs, "gopluslabs": load("goplus_fresh")})
            f = base.facts(FRESH, NOW, c)
            self.assertEqual((hard_skip(f), self.gp_calls(c)), (want, []), field)
        c = FakeClient({"token-pairs": [], "gopluslabs": load("goplus_older")})  # no pair at all
        self.assertEqual(hard_skip(base.facts(OLDER, NOW, c)), "missing:symbol")
        self.assertEqual(self.gp_calls(c), [])

    def test_age_from_geckoterminal_keeps_todays_flow(self):
        pairs = fresh_pairs(liq=1000)
        for p in pairs:
            p.pop("pairCreatedAt", None)
        c = FakeClient({"token-pairs": pairs, "geckoterminal": gt_pool("2026-10-23T10:00:00Z"),
                        "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual([b for b, _ in c.calls], ["ds_pairs", "gt"])  # no GoPlus, no extra request
        self.assertEqual(hard_skip(f), "liquidity")
        c = FakeClient({"token-pairs": pairs, "gopluslabs": load("goplus_fresh")})  # GeckoTerminal down
        f = base.facts(FRESH, NOW, c)
        self.assertEqual((hard_skip(f), self.gp_calls(c)), ("fetch_error", []))

    def test_gate_passes_goplus_called_as_before(self):
        c = FakeClient({"token-pairs": fresh_pairs(), "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, NOW, c, goplus_ok=lambda: True)
        self.assertEqual(len(self.gp_calls(c)), 1)
        self.assertNotIn("goplus_skipped", f)
        self.assertIn("launchpad", f)
        self.assertEqual(hard_skip(f), "goplus_not_ready")

    def test_goplus_budget_used_returns_none_only_when_goplus_needed(self):
        c = FakeClient({"token-pairs": fresh_pairs(), "gopluslabs": load("goplus_fresh")})
        self.assertIsNone(base.facts(FRESH, NOW, c, goplus_ok=lambda: False))
        self.assertEqual(self.gp_calls(c), [])
        c = FakeClient({"token-pairs": load("dexscreener_fresh")})
        self.assertEqual(hard_skip(base.facts(FRESH, NOW, c, goplus_ok=lambda: False)), "liquidity")

    def test_low_liquidity_honeypot_now_logs_liquidity(self):
        # accepted label shift (Pete, 2026-10-25): GoPlus would say honeypot, but it is never asked
        gp = load("goplus_fresh")
        gp["result"][FRESH].update(is_honeypot="1", cannot_sell_all="1")
        c = FakeClient({"token-pairs": load("dexscreener_fresh"), "gopluslabs": gp})
        f = base.facts(FRESH, NOW, c)
        self.assertEqual((hard_skip(f), self.gp_calls(c)), ("liquidity", []))


class SymbolAndPairTest(unittest.TestCase):
    def test_symbol_from_quote_side(self):
        pairs = load("dexscreener_older")
        p = pairs[0]
        p["baseToken"], p["quoteToken"] = p["quoteToken"], p["baseToken"]  # BRETT now on the quote side
        p["priceUsd"], p["priceNative"] = "2394.0", "485900"  # WETH priced in BRETT
        c = FakeClient({"token-pairs": pairs})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["symbol"], "BRETT")
        self.assertAlmostEqual(f["price"], 2394.0 / 485900)
        self.assertEqual(f["sells_1h"], 21)  # buys of WETH = sells of BRETT

    def test_sells_summed_over_all_pairs(self):
        a = load("dexscreener_older")[0]  # 14 sells, 723k liquidity
        b = copy.deepcopy(a)
        b["pairAddress"], b["liquidity"]["usd"] = "0xbbb", 900_000
        b["txns"]["h1"] = {"buys": 8, "sells": 0}  # deepest pool, quiet
        q = copy.deepcopy(a)
        q["pairAddress"], q["liquidity"]["usd"] = "0xccc", 1_000
        q["baseToken"], q["quoteToken"] = q["quoteToken"], q["baseToken"]
        q["txns"]["h1"] = {"buys": 5, "sells": 99}  # BRETT on the quote side: 5 BRETT sells
        foreign = copy.deepcopy(a)
        foreign["baseToken"]["address"] = "0x1111111111111111111111111111111111111111"
        foreign["txns"]["h1"] = {"buys": 0, "sells": 1000}
        c = FakeClient({"token-pairs": [a, b, q, foreign], "gopluslabs": load("goplus_older"),
                        "networks/base/pools/": None})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["sells_1h"], 14 + 0 + 5)
        self.assertEqual(f["liq_usd"], 900_000)  # price/liq/age still from the deepest pair
        self.assertEqual(f["pair"], "0xbbb")
        self.assertEqual(base.total_sells_1h([foreign], OLDER), None)

    def test_highest_liquidity_pair_either_side(self):
        a = load("dexscreener_older")[0]
        b = copy.deepcopy(a)
        b["pairAddress"] = "0xbbb"
        b["liquidity"]["usd"] = 900_000
        b["baseToken"], b["quoteToken"] = b["quoteToken"], b["baseToken"]
        other = copy.deepcopy(a)
        other["baseToken"]["address"] = "0x1111111111111111111111111111111111111111"
        other["liquidity"]["usd"] = 5_000_000
        pair, side = base.pick_pair([a, other, b], OLDER)
        self.assertEqual(pair["pairAddress"], "0xbbb")
        self.assertEqual(side, "quoteToken")

    def test_other_chains_ignored(self):
        a = load("dexscreener_older")[0]
        a["chainId"] = "ethereum"
        self.assertEqual(base.pick_pair([a], OLDER), (None, ""))


class AgeTest(unittest.TestCase):
    def test_geckoterminal_fallback(self):
        c = FakeClient({"token-pairs": load("dexscreener_older"),
                        "networks/base/pools/0x4e829f8a": gt_pool("2026-10-23T10:30:00Z")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual(f["age_min"], 60)
        self.assertTrue(any("geckoterminal" in u for _, u in c.calls))

    def test_no_age_anywhere_is_missing(self):
        c = FakeClient({"token-pairs": load("dexscreener_older"), "gopluslabs": load("goplus_older"),
                        "networks/base/pools/": None})  # GeckoTerminal answers but knows no date
        f = base.facts(OLDER, NOW, c)
        self.assertNotIn("age_min", f)
        self.assertEqual(hard_skip(dict(f, lp_locked_pct=100)), "missing:age_min")


class GoPlusFieldsTest(unittest.TestCase):
    PAIR = "0x00000000000000000000000000000000000000aa"

    def g(self, **over):
        g = list(load("goplus_older")["result"].values())[0]
        g.update(over)
        return g

    def test_top10_drops_pool_and_burn(self):
        holders = [{"address": self.PAIR, "percent": "0.50"},
                   {"address": base.ETH, "percent": "0.20"},
                   {"address": "0x000000000000000000000000000000000000dEaD", "percent": "0.10"},
                   {"address": "0x01", "percent": "0.05"}, {"address": "0x02", "percent": "0.03"}]
        f, _ = base.goplus_facts(self.g(holders=holders), self.PAIR, is_v4=False)
        self.assertAlmostEqual(f["top10_pct"], 8.0)

    def test_top10_drops_v4_pool_manager(self):
        holders = [{"address": base.V4_POOL_MANAGER, "percent": "0.60"}, {"address": "0x01", "percent": "0.05"}]
        f, _ = base.goplus_facts(self.g(holders=holders), "0x" + "ab" * 32, is_v4=True)
        self.assertAlmostEqual(f["top10_pct"], 5.0)
        f, _ = base.goplus_facts(self.g(holders=holders), self.PAIR, is_v4=False)
        self.assertAlmostEqual(f["top10_pct"], 65.0)

    def test_top10_older_fixture(self):
        f, _ = base.goplus_facts(self.g(), self.PAIR, is_v4=False)
        self.assertAlmostEqual(f["top10_pct"], 32.84, places=1)

    def test_lp_lock_only_on_our_single_pool(self):
        lp = [{"address": "0x01", "percent": "0.70", "is_locked": 1},
              {"address": base.ETH, "percent": "0.25", "is_locked": 0},
              {"address": "0x02", "percent": "0.05", "is_locked": 0}]
        one = [{"pair": self.PAIR, "pool_fee": "0.01", "liquidity_type": "UniV3"}]
        f, _ = base.goplus_facts(self.g(lp_holders=lp, dex=one), self.PAIR, is_v4=False)
        self.assertAlmostEqual(f["lp_locked_pct"], 95.0)
        other = [{"pair": "0x00000000000000000000000000000000000000bb"}]
        f, pending = base.goplus_facts(self.g(lp_holders=lp, dex=other), self.PAIR, is_v4=False)
        self.assertNotIn("lp_locked_pct", f)
        self.assertNotIn("lp_locked_pct", pending)  # structural gap: final, not watched

    def test_taxes_are_percent(self):
        f, _ = base.goplus_facts(self.g(buy_tax="0.05", sell_tax="0.1"), self.PAIR, is_v4=False)
        self.assertAlmostEqual(f["buy_tax_pct"], 5.0)
        self.assertAlmostEqual(f["sell_tax_pct"], 10.0)

    def test_risk_flags(self):
        f, _ = base.goplus_facts(self.g(hidden_owner="1", external_call="1", is_open_source="0",
                                        honeypot_with_same_creator="0"), self.PAIR, is_v4=False)
        self.assertEqual(f["risk_flags"], ["hidden_owner", "external_call", "is_open_source=0"])


class LaunchpadTest(unittest.TestCase):
    def pair(self, quote=base.WETH, v4=False):
        p = copy.deepcopy(load("dexscreener_older")[0])
        p["quoteToken"]["address"] = quote
        if v4:
            p["labels"] = ["v4"]
        return p

    def test_zora_quote(self):
        self.assertEqual(base.launchpad({}, self.pair(quote=base.ZORA), "baseToken", None), "zora")

    def test_v4_one_percent_is_clanker(self):
        self.assertEqual(base.launchpad({}, self.pair(v4=True), "baseToken", 0.01), "clanker")
        self.assertEqual(base.launchpad({}, self.pair(v4=True), "baseToken", 0.003), "other")
        self.assertEqual(base.launchpad({}, self.pair(v4=False), "baseToken", 0.01), "other")

    def test_known_creator_wins(self):
        base.CLANKER_CREATORS.add("0xc1")
        try:
            g = {"creator_address": "0xC1"}
            self.assertEqual(base.launchpad(g, self.pair(quote=base.ZORA), "baseToken", None), "clanker")
        finally:
            base.CLANKER_CREATORS.discard("0xc1")

    def test_fee_from_goplus_then_geckoterminal_name(self):
        g = {"dex": [{"pair": "0xp", "pool_fee": "0.01"}]}
        self.assertEqual(base.pool_fee(g, "0xp"), 0.01)
        self.assertEqual(base.pool_fee({}, "0xp", gt_pool("x", "TIA / ETH 1%")), 0.01)
        self.assertIsNone(base.pool_fee({}, "0xp", gt_pool("x", "asci / ETH")))


class ScanTest(unittest.TestCase):
    def test_scan_dedupes_and_drops_quote_assets(self):
        profiles = [{"chainId": "base", "tokenAddress": "0x674C978B67927A8B89D53215D9E5FBCE5B383D19"},
                    {"chainId": "solana", "tokenAddress": "So1111"},
                    {"chainId": "base", "tokenAddress": base.WETH},
                    {"chainId": "base", "tokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
                    {"chainId": "base", "tokenAddress": "0x" + "12" * 20}]
        c = FakeClient({"new_pools": load("geckoterminal_new_pools"), "token-profiles": profiles,
                        "token-boosts": [{"chainId": "base", "tokenAddress": "0x" + "12" * 20}]})
        leads = base.scan(c)
        addrs = [l["address"] for l in leads]
        self.assertEqual(len(addrs), len(set(addrs)))
        self.assertEqual(len(c.calls), 3)
        self.assertNotIn(base.WETH, addrs)
        self.assertNotIn(base.ETH, addrs)
        self.assertNotIn("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", addrs)
        asci = [l for l in leads if l["address"] == FRESH][0]
        self.assertEqual((asci["symbol"], asci["source"]), ("asci", "gt_new_pools"))
        self.assertEqual(leads[-1], {"address": "0x" + "12" * 20, "symbol": "", "source": "ds_profile", "pool": ""})
        self.assertEqual(len(leads), 21)  # 20 pools (20 tokens) + 1 new DS lead; the asci profile is a duplicate

    def test_scan_survives_every_source_down(self):
        self.assertEqual(base.scan(FakeClient({})), [])


if __name__ == "__main__":
    unittest.main()
