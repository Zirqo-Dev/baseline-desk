"""VERIFY: hard skips on the standard facts dict. A missing number is a skip."""
from core import config

REQUIRED = ["symbol", "price", "liq_usd", "age_min", "sells_1h", "mint_open",
            "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
            "lp_locked_pct", "dev_pct", "top10_pct", "launchpad", "risk_flags"]

# Fields that come from GoPlus. Only these can wait on the watchlist (BL-6 3a).
GOPLUS_FIELDS = ["mint_open", "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
                 "lp_locked_pct", "dev_pct", "top10_pct", "risk_flags"]

# risk_flags that are a hard skip. Log-only flags (external_call, is_open_source=0,
# honeypot_with_same_creator) stay in the list but never skip.
HARD_FLAGS = ["hidden_owner", "can_take_back_ownership", "owner_change_balance",
              "slippage_modifiable", "personal_slippage_modifiable", "selfdestruct"]


def hard_skip(facts):
    """Return the skip reason, or "" if the lead survives every hard rule.

    "fetch_error" (not final) when any request failed (429, 5xx, timeout): a rate limit
    is our problem, not a fact about the token.

    "goplus_partial" (final) when GoPlus answered code 3: it can't read the token.

    "goplus_not_ready" (not final) when every missing field is one the adapter
    listed in facts["goplus_pending"] (GoPlus has not filled it yet), unless the
    pool is already under the liquidity minimum.
    """
    if facts.get("fetch_errors"):
        return "fetch_error"
    if facts.get("goplus_partial"):
        return "goplus_partial"
    s = config.SKIP_IF
    missing = [k for k in REQUIRED if facts.get(k) is None]
    if missing:
        pending = set(facts.get("goplus_pending") or []) & set(GOPLUS_FIELDS)
        final = [k for k in missing if k not in pending]
        if final:
            return f"missing:{final[0]}"
        if facts["liq_usd"] < s["min_liquidity_usd"]:
            return "liquidity"
        return "goplus_not_ready"
    if facts["honeypot"]:
        return "honeypot"
    if facts["mint_open"]:
        return "mint_open"
    if facts["freeze_or_blacklist"]:
        return "freeze_or_blacklist"
    for flag in HARD_FLAGS:
        if flag in facts["risk_flags"]:
            return f"risk_flag:{flag}"
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
