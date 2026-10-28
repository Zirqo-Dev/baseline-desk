import unittest

from core.verify import DEX_FIELDS, GOPLUS_FIELDS, REQUIRED, dex_skip, hard_skip, lp_unknown

GOOD = {"symbol": "TEST", "price": 0.001, "liq_usd": 80_000, "age_min": 60, "sells_1h": 40,
        "mint_open": False, "freeze_or_blacklist": False, "honeypot": False,
        "buy_tax_pct": 0, "sell_tax_pct": 0, "lp_locked_pct": 100, "dev_pct": 3,
        "top10_pct": 20, "launchpad": "clanker", "risk_flags": []}


class VerifyTest(unittest.TestCase):
    def test_survivor(self):
        self.assertEqual(hard_skip(GOOD), "")

    def test_every_field_missing_is_a_skip(self):
        for k in REQUIRED:
            if k == "lp_locked_pct":  # BL-21: unknown LP lock is allowed, see LpUnknownTest
                continue
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
        del f["mint_open"], f["top10_pct"]
        self.assertEqual(hard_skip(f), "missing:top10_pct")

    def test_fetch_error_comes_first(self):
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=["gt"])), "fetch_error")
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=["ds_pairs"], price=None)), "fetch_error")
        self.assertEqual(hard_skip(dict(GOOD, fetch_errors=[])), "")

    def test_goplus_partial_after_fetch_error_before_gaps(self):
        part = {k: v for k, v in GOOD.items() if k not in ("honeypot", "buy_tax_pct")}
        self.assertEqual(hard_skip(dict(part, goplus_partial=True)), "goplus_partial")
        self.assertEqual(hard_skip(dict(part, goplus_partial=True, fetch_errors=["ds_pairs"])), "fetch_error")


def gated(f):
    """What facts() returns when the BL-20 gate fires: the DexScreener side only, GoPlus never asked."""
    out = {k: f[k] for k in DEX_FIELDS if f.get(k) is not None}
    return dict(out, fetch_errors=[e for e in f.get("fetch_errors", []) if e != "goplus"], goplus_skipped=True)


class DexFirstTest(unittest.TestCase):
    def test_fields_split(self):
        self.assertEqual(set(DEX_FIELDS) | set(GOPLUS_FIELDS) | {"launchpad"}, set(REQUIRED))
        self.assertEqual(DEX_FIELDS, [k for k in REQUIRED if k in DEX_FIELDS])  # same order as hard_skip

    def test_gate_order(self):
        self.assertEqual(dex_skip(dict(GOOD, fetch_errors=["ds_pairs"], liq_usd=None)), "fetch_error")
        self.assertEqual(dex_skip(dict(GOOD, symbol=None, liq_usd=1)), "missing:symbol")
        self.assertEqual(dex_skip(dict(GOOD, liq_usd=49_999)), "liquidity")
        self.assertEqual(dex_skip(GOOD), "")
        self.assertEqual(dex_skip({k: GOOD[k] for k in DEX_FIELDS}), "")  # GoPlus fields not needed

    def test_label_shift_only_never_outcome(self):
        """Every lead the gate settles was a skip before BL-20 too (never PASS or WATCH); only the label can move."""
        low = dict(GOOD, liq_usd=40_000)
        pending = {k: v for k, v in low.items() if k != "mint_open"}
        cases = {  # today's facts (GoPlus called) -> today's label
            "honeypot": dict(low, honeypot=True),
            "mint_open": dict(low, mint_open=True),
            "freeze_or_blacklist": dict(low, freeze_or_blacklist=True),
            "risk_flag:hidden_owner": dict(low, risk_flags=["hidden_owner"]),
            "goplus_partial": dict(low, goplus_partial=True),
            "nobody_can_sell": dict(low, sells_1h=0),
            "missing:top10_pct": {k: v for k, v in low.items() if k != "top10_pct"},
            "fetch_error": dict(low, fetch_errors=["goplus"]),  # was RETRY, final skip anyway after
            "liquidity": dict(pending, goplus_pending=["mint_open"]),  # watched lead, pool too thin
        }
        for was, f in cases.items():
            self.assertEqual(hard_skip(f), was)
            self.assertEqual(hard_skip(gated(f)), "liquidity", was)  # the label after BL-20
        self.assertEqual(hard_skip(gated(low)), hard_skip(low))  # plain liquidity skip: no change
        # missing DexScreener fields and failed DexScreener requests: same label as today
        no_price = {k: v for k, v in dict(GOOD, honeypot=True).items() if k != "price"}
        self.assertEqual((hard_skip(no_price), hard_skip(gated(no_price))), ("missing:price", "missing:price"))
        ds_down = dict(GOOD, fetch_errors=["ds_pairs"])
        self.assertEqual((hard_skip(ds_down), hard_skip(gated(ds_down))), ("fetch_error", "fetch_error"))

    def test_gate_open_means_goplus_decides_as_before(self):
        for f in (GOOD, dict(GOOD, honeypot=True), dict(GOOD, top10_pct=50), dict(GOOD, liq_usd=50_000)):
            self.assertEqual(dex_skip(f), "")
        self.assertEqual(hard_skip(dict(GOOD, honeypot=True)), "honeypot")



class LpUnknownTest(unittest.TestCase):
    """BL-21 (DECISIONS 2026-10-25): an unknown LP lock is allowed and logged lp_unknown."""
    NO_LP = {k: v for k, v in GOOD.items() if k != "lp_locked_pct"}

    def test_missing_lp_everything_else_clean_passes(self):
        self.assertEqual(hard_skip(self.NO_LP), "")
        self.assertEqual(hard_skip(dict(GOOD, lp_locked_pct=None)), "")
        self.assertTrue(lp_unknown(self.NO_LP))
        self.assertFalse(lp_unknown(GOOD))

    def test_lp_only_pending_continues(self):
        f = dict(self.NO_LP, goplus_pending=["lp_locked_pct"])
        self.assertEqual(hard_skip(f), "")
        self.assertTrue(lp_unknown(f))
        self.assertEqual(hard_skip(dict(f, top10_pct=50)), "top10_pct")  # other rules still apply

    def test_lp_and_honeypot_pending_waits(self):
        f = {k: v for k, v in self.NO_LP.items() if k != "honeypot"}
        f["goplus_pending"] = ["honeypot", "lp_locked_pct"]
        self.assertEqual(hard_skip(f), "goplus_not_ready")
        self.assertFalse(lp_unknown(f))  # stopped at the missing-data gate

    def test_known_lp_under_minimum_still_skips(self):
        self.assertEqual(hard_skip(dict(GOOD, lp_locked_pct=50)), "lp_locked")
        self.assertFalse(lp_unknown(dict(GOOD, lp_locked_pct=50)))

    def test_hard_rules_still_win(self):
        cases = {"honeypot": dict(honeypot=True), "mint_open": dict(mint_open=True),
                 "freeze_or_blacklist": dict(freeze_or_blacklist=True),
                 "risk_flag:selfdestruct": dict(risk_flags=["selfdestruct"]),
                 "nobody_can_sell": dict(sells_1h=0), "liquidity": dict(liq_usd=49_999),
                 "dev_pct": dict(dev_pct=11), "top10_pct": dict(top10_pct=36),
                 "too_young": dict(age_min=14), "too_old": dict(age_min=48 * 60 + 1),
                 "buy_tax": dict(buy_tax_pct=11), "sell_tax": dict(sell_tax_pct=11)}
        for reason, change in cases.items():
            f = dict(self.NO_LP, **change)
            self.assertEqual(hard_skip(f), reason)
            self.assertTrue(lp_unknown(f), reason)  # past the gate: the SKIP row carries lp_unknown too

    def test_missing_field_order_skips_lp(self):
        f = {k: v for k, v in GOOD.items() if k not in ("lp_locked_pct", "dev_pct", "top10_pct")}
        self.assertEqual(hard_skip(f), "missing:dev_pct")
        f = {k: v for k, v in GOOD.items() if k not in ("sell_tax_pct", "lp_locked_pct", "dev_pct")}
        self.assertEqual(hard_skip(f), "missing:sell_tax_pct")
        self.assertEqual(hard_skip(dict(f, goplus_pending=["sell_tax_pct", "lp_locked_pct"])), "missing:dev_pct")

    def test_not_past_the_gate_is_not_lp_unknown(self):
        self.assertFalse(lp_unknown(dict(self.NO_LP, fetch_errors=["goplus"])))
        self.assertFalse(lp_unknown(dict(self.NO_LP, goplus_partial=True)))
        self.assertFalse(lp_unknown({k: self.NO_LP[k] for k in DEX_FIELDS} | {"goplus_skipped": True}))
        self.assertFalse(lp_unknown({k: v for k, v in self.NO_LP.items() if k != "dev_pct"}))


if __name__ == "__main__":
    unittest.main()
