import unittest

from core.verify import REQUIRED, hard_skip

GOOD = {"symbol": "TEST", "price": 0.001, "liq_usd": 80_000, "age_min": 60, "sells_1h": 40,
        "mint_open": False, "freeze_or_blacklist": False, "honeypot": False,
        "buy_tax_pct": 0, "sell_tax_pct": 0, "lp_locked_pct": 100, "dev_pct": 3,
        "top10_pct": 20, "launchpad": "clanker", "risk_flags": []}


class VerifyTest(unittest.TestCase):
    def test_survivor(self):
        self.assertEqual(hard_skip(GOOD), "")

    def test_every_field_missing_is_a_skip(self):
        for k in REQUIRED:
            f = dict(GOOD)
            del f[k]
            self.assertEqual(hard_skip(f), f"missing:{k}")
            f[k] = None
            self.assertEqual(hard_skip(f), f"missing:{k}")

    def test_top10_missing_is_a_skip(self):
        f = dict(GOOD, top10_pct=None)
        self.assertEqual(hard_skip(f), "missing:top10_pct")

    def test_thresholds(self):
        cases = {"honeypot": dict(honeypot=True), "mint_open": dict(mint_open=True),
                 "freeze_or_blacklist": dict(freeze_or_blacklist=True),
                 "liquidity": dict(liq_usd=49_999), "lp_locked": dict(lp_locked_pct=89),
                 "dev_pct": dict(dev_pct=11), "top10_pct": dict(top10_pct=36),
                 "too_young": dict(age_min=14), "too_old": dict(age_min=48 * 60 + 1),
                 "buy_tax": dict(buy_tax_pct=11), "sell_tax": dict(sell_tax_pct=11)}
        for reason, change in cases.items():
            self.assertEqual(hard_skip(dict(GOOD, **change)), reason)

    def test_zero_is_a_number_not_missing(self):
        self.assertEqual(hard_skip(dict(GOOD, buy_tax_pct=0, sell_tax_pct=0)), "")

    def test_nobody_can_sell(self):
        self.assertEqual(hard_skip(dict(GOOD, sells_1h=0)), "nobody_can_sell")
        self.assertEqual(hard_skip(dict(GOOD, sells_1h=1)), "")

    def test_risk_flags_hard_skip(self):
        for flag in ["hidden_owner", "can_take_back_ownership", "owner_change_balance",
                     "slippage_modifiable", "personal_slippage_modifiable", "selfdestruct"]:
            self.assertEqual(hard_skip(dict(GOOD, risk_flags=[flag])), f"risk_flag:{flag}")

    def test_risk_flags_log_only(self):
        f = dict(GOOD, risk_flags=["external_call", "is_open_source=0", "honeypot_with_same_creator"])
        self.assertEqual(hard_skip(f), "")

    def test_goplus_not_ready(self):
        f = dict(GOOD, goplus_pending=["mint_open", "honeypot", "top10_pct"])
        for k in ["mint_open", "honeypot", "top10_pct"]:
            del f[k]
        self.assertEqual(hard_skip(f), "goplus_not_ready")
        self.assertEqual(hard_skip(dict(f, liq_usd=49_999)), "liquidity")  # final even while waiting

    def test_dexscreener_gap_stays_final(self):
        f = dict(GOOD, goplus_pending=["mint_open"])
        del f["mint_open"], f["age_min"]
        self.assertEqual(hard_skip(f), "missing:age_min")

    def test_goplus_gap_not_flagged_pending_is_final(self):
        f = dict(GOOD, goplus_pending=["mint_open"])
        del f["mint_open"], f["lp_locked_pct"]
        self.assertEqual(hard_skip(f), "missing:lp_locked_pct")

    def test_fetch_error_comes_first(self):
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=["gt"])), "fetch_error")
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=["ds_pairs"], price=None)), "fetch_error")
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=[])), "")


if __name__ == "__main__":
    unittest.main()
