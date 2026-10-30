"""VERIFY: hard skips on the standard facts dict. A missing number is a skip."""
from core import config

REQUIRED = ["symbol", "price", "liq_usd", "age_min", "sells_1h", "mint_open",
            "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
            "lp_locked_pct", "dev_pct", "top10_pct", "launchpad", "risk_flags"]

# Fields that come from GoPlus. Only these can wait on the watchlist (BL-6 3a).
GOPLUS_FIELDS = ["mint_open", "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
                 "lp_locked_pct", "dev_pct", "top10_pct", "risk_flags"]

# Fields DexScreener gives on its own (GeckoTerminal only for a missing age), in REQUIRED order.
# launchpad is neither: it also reads the GoPlus creator. BL-20 gate, see dex_skip().
DEX_FIELDS = ["symbol", "price", "liq_usd", "age_min", "sells_1h"]

# risk_flags that are a hard skip. Log-only flags (external_call, is_open_source=0,
# honeypot_with_same_creator) stay in the list but never skip.
HARD_FLAGS = ["hidden_owner", "can_take_back_ownership", "owner_change_balance",
              "slippage_modifiable", "personal_slippage_modifiable", "selfdestruct"]

# BL-21: an unknown LP lock (missing, or still pending at GoPlus) no longer skips or keeps a lead watched.
# A known lock under min_lp_locked_pct still skips. DECISIONS 2026-10-25, BL-21.
LP = "lp_locked_pct"

# BL-25: DexScreener trade facts logged at the end of VERIFY and baseline BUY rows (adapters/base.py trade_facts()).
TRADE_KEYS = ["b1", "s1", "b24", "s24", "liq", "dex"]


def dex_skip(facts):
    """BL-20 gate, before any GoPlus call: the skip DexScreener alone decides, or "" when GoPlus is needed.

    In order: fetch_error (a request so far failed), missing:<first DEX_FIELDS gap>, liquidity.
    Same outcome as hard_skip() on the full facts; only the label can differ (a low-liquidity
    honeypot logs "liquidity"). DECISIONS 2026-10-26, BL-20.
    """
    if facts.get("fetch_errors"):
        return "fetch_error"
    for k in DEX_FIELDS:
        if facts.get(k) is None:
            return f"missing:{k}"
    if facts["liq_usd"] < config.SKIP_IF["min_liquidity_usd"]:
        return "liquidity"
    return ""


def hard_skip(facts):
    """Return the skip reason, or "" if the lead survives every hard rule.

    "fetch_error" (not final) when any request failed (429, 5xx, timeout): a rate limit
    is our problem, not a fact about the token.

    "goplus_partial" (final) when GoPlus answered code 3: it can't read the token.

    "goplus_not_ready" (not final) when every missing field is one the adapter
    listed in facts["goplus_pending"] (GoPlus has not filled it yet), unless the
    pool is already under the liquidity minimum.

    facts["goplus_skipped"] (BL-20): GoPlus was never called because dex_skip() decided; that reason stands.

    lp_locked_pct None (BL-21) is not a gap: the lead goes on with an unknown LP lock (see lp_unknown()).
    """
    if facts.get("goplus_skipped"):
        return dex_skip(facts)
    if facts.get("fetch_errors"):
        return "fetch_error"
    if facts.get("goplus_partial"):
        return "goplus_partial"
    s = config.SKIP_IF
    missing = [k for k in REQUIRED if k != LP and facts.get(k) is None]
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
    if facts.get(LP) is not None and facts[LP] < s["min_lp_locked_pct"]:
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


def lp_unknown(facts):
    """BL-21: True when the lead got past the missing-data gate of hard_skip() with no LP-lock number."""
    if facts.get("goplus_skipped") or facts.get("fetch_errors") or facts.get("goplus_partial"):
        return False
    return facts.get(LP) is None and all(facts.get(k) is not None for k in REQUIRED if k != LP)


def trade_detail(facts):
    """BL-25: the trade keys for a row's detail, "na" where missing; [] for a gate-killed check (goplus_skipped).

    Logging only, never read by a rule. Never raises.
    """
    if not isinstance(facts, dict) or facts.get("goplus_skipped"):
        return []
    t = facts.get("trade") if isinstance(facts.get("trade"), dict) else {}
    return [f"{k}={t.get(k, 'na')}" for k in TRADE_KEYS]
