"""VERIFY: hard skips on the standard facts dict. A missing number is a skip."""
from core import config

REQUIRED = ["symbol", "price", "liq_usd", "age_min", "sells_1h", "mint_open",
            "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
            "lp_locked_pct", "dev_pct", "top10_pct", "launchpad", "risk_flags"]


def hard_skip(facts):
    """Return the skip reason, or "" if the lead survives every hard rule."""
    for k in REQUIRED:
        if facts.get(k) is None:
            return f"missing:{k}"
    s = config.SKIP_IF
    if facts["honeypot"]:
        return "honeypot"
    if facts["mint_open"]:
        return "mint_open"
    if facts["freeze_or_blacklist"]:
        return "freeze_or_blacklist"
    if facts["sells_1h"] == 0:
        return "nobody_can_sell"
    if facts["liq_usd"] < s["min_liquidity_usd"]:
        return "liquidity"
    if facts["lp_locked_pct"] < s["min_lp_locked_pct"]:
        return "lp_locked"
    if facts["dev_pct"] > s["max_dev_pct"]:
        return "dev_pct"
    if facts["top10_pct"] > s["max_top10_pct"]:
        return "top10_pct"
    if facts["age_min"] < s["min_age_min"]:
        return "too_young"
    if facts["age_min"] > s["max_age_h"] * 60:
        return "too_old"
    if facts["buy_tax_pct"] > s["max_buy_tax_pct"]:
        return "buy_tax"
    if facts["sell_tax_pct"] > s["max_sell_tax_pct"]:
        return "sell_tax"
    return ""
