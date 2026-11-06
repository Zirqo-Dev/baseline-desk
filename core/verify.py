"""VERIFY: hard skips on the standard facts dict. A missing number is a skip.

BL-31: universe_skip() is the universe both books share (the desk and the random twin): dex_skip() (incl. the asset
exclusion) + sells_skip() + age_skip(). The desk applies every one of these rules: dex_skip() before any GoPlus call,
sells_skip() and age_skip() inside hard_skip() at their old place, so the desk's outcomes and labels are unchanged
except for the new asset:<kind> gate skip. The twin applies universe_skip() to the DexScreener side only.

BL-32: the real-depth floor (SKIP_IF min_qliq_usd, on qliq = the BL-28 USD value of the chosen pair's non-token side)
replaces the total-liquidity floor in dex_skip(), so both books get it: qliq missing = missing:qliq, under the floor =
real_depth (final 24 h gate skip, goplus=skipped). min_liquidity_usd is now only the logging floor: logged() keeps the
labels and trade keys on every check today's (pre-BL-32) gate let through, real_depth or not (the owner, BL-32).
"""
import re

from core import config

REQUIRED = ["symbol", "price", "liq_usd", "age_min", "sells_1h", "qliq", "mint_open",
            "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
            "lp_locked_pct", "dev_pct", "top10_pct", "launchpad", "risk_flags"]

# Fields that come from GoPlus. Only these can wait on the watchlist (BL-6 3a).
GOPLUS_FIELDS = ["mint_open", "freeze_or_blacklist", "honeypot", "buy_tax_pct", "sell_tax_pct",
                 "lp_locked_pct", "dev_pct", "top10_pct", "risk_flags"]

# Fields DexScreener gives on its own (GeckoTerminal only for a missing age), in REQUIRED order.
# launchpad is neither: it also reads the GoPlus creator. BL-20 gate, see dex_skip().
DEX_FIELDS = ["symbol", "price", "liq_usd", "age_min", "sells_1h", "qliq"]  # qliq last (BL-32)

# BL-32: the dex_skip() reasons that come from the real-depth floor alone; logged() keeps logging these checks.
DEPTH_SKIPS = ("real_depth", "missing:qliq")

# risk_flags that are a hard skip. Log-only flags (external_call, is_open_source=0,
# honeypot_with_same_creator) stay in the list but never skip.
HARD_FLAGS = ["hidden_owner", "can_take_back_ownership", "owner_change_balance",
              "slippage_modifiable", "personal_slippage_modifiable", "selfdestruct"]

# BL-21: an unknown LP lock (missing, or still pending at GoPlus) no longer skips or keeps a lead watched.
# A known lock under min_lp_locked_pct still skips. DECISIONS 2026-10-25, BL-21.
LP = "lp_locked_pct"

# BL-25: DexScreener trade facts logged at the end of VERIFY and baseline BUY rows (adapters/base.py trade_facts()).
# BL-30: pair_age = whole minutes since the chosen pair was created (the age_min the age rule reads); token_age = whole
# minutes since the oldest Base pair in the same /token-pairs answer was created. Both are pair creation times.
TRADE_KEYS = ["b1", "s1", "b24", "s24", "liq", "dex", "qliq", "pair_age", "token_age"]  # qliq: BL-28; ages: BL-30


# BL-31: asset exclusion (stables, wrapped/staked majors, quote assets), from data already fetched, no request.
# HEURISTICS for the fresh-launch universe only (revisit at BL-34 and BL-39): "symbol contains USD" and
# "price >= 100 USD" would be wrong for an older or wider universe. DECISIONS 2026-11-06, BL-31 f.
ASSET = {
    "fiat": ("EUR", "GBP", "CHF", "JPY", "CAD", "AUD", "SGD"),  # with 0-2 letters before, 0-1 after (tGBP, EURC)
    "stable_names": {"DAI", "GHO", "FRAX"},
    "wrapped_price_usd": 100,  # a fresh launch never trades this high; wrapped ETH/BTC and tokenized stocks do
    "wrapped_prefixes": ("W", "WT", "CB", "ST", "WST", "MS", "R", "WE", "EZ", "RS", "T", "L", "SUPERO"),
    "wrapped_majors": ("ETH", "BTC"),
}
QUOTE_ASSETS = {"0x1111111111166b7fe7bd91427724b487980afc69"}  # ZORA (WETH, ETH and USDC never become leads)
_FIAT = re.compile(r"[A-Z]{0,2}(%s)[A-Z]?" % "|".join(ASSET["fiat"]))
_WRAPPED = re.compile(r"(%s)?(%s)B?" % ("|".join(sorted(ASSET["wrapped_prefixes"], key=len, reverse=True)),
                                         "|".join(ASSET["wrapped_majors"])))


def norm_symbol(symbol):
    """BL-31: the symbol uppercased, letters and digits only ("I O F" = "IOF", "U.S OIL" = "USOIL"); "" if none."""
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())


def asset_kind(symbol, price, address, base_pairs=0, quote_pairs=0):
    """BL-31: "stable", "wrapped", "quote" or "" for one token, from data in hand. Never raises.

    stable  : the normalized symbol contains USD, or is 0-2 letters + EUR/GBP/CHF/JPY/CAD/AUD/SGD + 0-1 letter, or DAI/GHO/FRAX
    wrapped : price >= 100 USD, or the symbol is a wrapped/staked ETH or BTC name (cbETH, wstETH, msETH, cbBTC, superOETHb)
    quote   : a known quote asset (ZORA), or the token is the quoteToken in more of its Base pairs than the baseToken
    """
    s = norm_symbol(symbol)
    if "USD" in s or _FIAT.fullmatch(s) or s in ASSET["stable_names"]:
        return "stable"
    if (isinstance(price, (int, float)) and not isinstance(price, bool) and price >= ASSET["wrapped_price_usd"]) \
            or _WRAPPED.fullmatch(s):
        return "wrapped"
    if str(address or "").lower() in QUOTE_ASSETS or (quote_pairs or 0) > (base_pairs or 0):
        return "quote"
    return ""


def dex_skip(facts):
    """BL-20 gate, before any GoPlus call: the skip DexScreener alone decides, or "" when GoPlus is needed.

    In order: fetch_error (a request so far failed), missing:<first DEX_FIELDS gap> (qliq last, BL-32),
    real_depth (BL-32: qliq < min_qliq_usd; it replaced the total-liquidity floor "liquidity"),
    then (BL-31) asset:<kind> from facts["asset"] (adapters/base.py facts() fills it with asset_kind()).
    Same outcome as hard_skip() on the full facts; only the label can differ (a thin honeypot logs
    "real_depth"). DECISIONS 2026-10-26, BL-20.
    """
    if facts.get("fetch_errors"):
        return "fetch_error"
    for k in DEX_FIELDS:
        if facts.get(k) is None:
            return f"missing:{k}"
    if depth_skip(facts):
        return "real_depth"
    if facts.get("asset"):
        return f"asset:{facts['asset']}"
    return ""


def depth_skip(facts):
    """BL-32: True when the real depth (qliq, a number; 0 is real) is under min_qliq_usd. Both books, via dex_skip()."""
    return facts["qliq"] < config.SKIP_IF["min_qliq_usd"]


def logged(facts):
    """BL-32, logging only: True when this check gets the BL-25/28/30 trade keys on its VERIFY row and BL-26 labels.

    The logging set is unchanged by the real-depth floor (the owner, BL-32): every check past dex_skip() (as before),
    plus a check dex_skip() killed only on real depth (real_depth or missing:qliq) that today's old gate let through:
    total liq_usd >= min_liquidity_usd (the logging floor) and no asset kind. A check new to the universe (qliq over
    the floor, liq under 50k) is past dex_skip(), so it is logged too. Never read by a trading rule. Never raises."""
    try:
        if not isinstance(facts, dict):
            return False
        if not facts.get("goplus_skipped"):
            return True
        return (dex_skip(facts) in DEPTH_SKIPS and not facts.get("asset")
                and facts["liq_usd"] >= config.SKIP_IF["min_liquidity_usd"])
    except Exception:  # logging must never break a check
        return False


def sells_skip(facts):
    """Nobody sold in the last hour (sells_1h 0): shared by hard_skip() and universe_skip() (BL-31)."""
    return "nobody_can_sell" if facts["sells_1h"] == 0 else ""


def age_skip(facts):
    """Pool age outside min_age_min .. max_age_h: shared by hard_skip() and universe_skip() (BL-31)."""
    s = config.SKIP_IF
    if facts["age_min"] < s["min_age_min"]:
        return "too_young"
    if facts["age_min"] > s["max_age_h"] * 60:
        return "too_old"
    return ""


def universe_skip(facts):
    """BL-31: the universe both books share, on the DexScreener side alone (no GoPlus): dex_skip() (fetch_error,
    missing DexScreener field incl. qliq, real_depth (BL-32), asset:<kind>), then sells_skip(), then age_skip().
    "" = in the universe. The random twin picks only from leads where this is "" (BL-32: the real-depth floor is in
    dex_skip(), so both books get it from one place)."""
    return dex_skip(facts) or sells_skip(facts) or age_skip(facts)


def hard_skip(facts):
    """Return the skip reason, or "" if the lead survives every hard rule.

    "fetch_error" (not final) when any request failed (429, 5xx, timeout): a rate limit
    is our problem, not a fact about the token.

    "goplus_partial" (final) when GoPlus answered code 3: it can't read the token.

    "goplus_not_ready" (not final) when every missing field is one the adapter
    listed in facts["goplus_pending"] (GoPlus has not filled it yet), unless the
    pool's real depth is already under the floor (real_depth, BL-32).

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
        if depth_skip(facts):
            return "real_depth"
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
    if sells_skip(facts):
        return sells_skip(facts)
    if depth_skip(facts):
        return "real_depth"
    if facts.get(LP) is not None and facts[LP] < s["min_lp_locked_pct"]:
        return "lp_locked"
    if facts["dev_pct"] > s["max_dev_pct"]:
        return "dev_pct"
    if facts["top10_pct"] > s["max_top10_pct"]:
        return "top10_pct"
    if age_skip(facts):
        return age_skip(facts)
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
    """BL-25: the trade keys for a row's detail, "na" where missing; [] for a gate-killed check outside the logging
    set (BL-32 logged(): a real_depth / missing:qliq check that today's old gate let through keeps its keys).

    Logging only, never read by a rule. Never raises.
    """
    if not isinstance(facts, dict) or not logged(facts):
        return []
    t = facts.get("trade") if isinstance(facts.get("trade"), dict) else {}
    return [f"{k}={t.get(k, 'na')}" for k in TRADE_KEYS]
