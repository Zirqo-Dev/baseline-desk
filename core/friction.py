"""Paper fill friction: fees + slippage per side. Paper must not flatter.

BL-32 (both books, one function): per side = FRICTION base_pct + the launchpad pool fee + price impact, where
impact = 100 x side USD / qliq (qliq = the BL-28 real depth: USD on the pair's non-token side).
  - side USD: a buy = the ticket; a sell = qty x quote (gross, before friction).
  - qliq: a buy = the qliq at entry (facts["qliq"]); a sell = the qliq of this tick's exit quote when it has one
    ("exit"), else the position's entry_qliq ("entry"); neither known = no impact, thin_pct in its place ("none").
    Never total liquidity: it is mostly the token's own side and would flatter paper (the owner, BL-32).
  - impact is capped at 100%, and the whole sell side at 100% (fill 0, proceeds 0, never negative).
thin_liq_usd is not read since BL-32.
"""
from core import config

CAP_PCT = 100.0


def _num(q):
    return q if isinstance(q, (int, float)) and not isinstance(q, bool) and q == q else None


def impact_pct(usd, qliq):
    """Price impact in percent for a side of `usd` into `qliq`; None when qliq is unknown. qliq 0 (real) = the cap."""
    q = _num(qliq)
    if q is None:
        return None
    if q <= 0:
        return CAP_PCT
    return min(CAP_PCT, 100.0 * max(float(usd), 0.0) / q)


def side_pct(usd, qliq, launchpad=""):
    """Friction for one side, in percent: base + launchpad pool fee + impact (thin_pct when qliq is unknown)."""
    f = config.FRICTION
    imp = impact_pct(usd, qliq)
    return f["base_pct"] + f["pool_fee_pct"].get((launchpad or "").lower(), 0.0) + (f["thin_pct"] if imp is None else imp)


def exit_depth(qliq_now, entry_qliq):
    """(qliq, src) that prices a sell: this tick's quote qliq ("exit"), else the entry qliq ("entry"), else (None, "none")."""
    if _num(qliq_now) is not None:
        return qliq_now, "exit"
    if _num(entry_qliq) is not None:
        return entry_qliq, "entry"
    return None, "none"


def note(usd, qliq, src):
    """The trades.csv note keys (BL-32): impact_pct= qliq= qliq_src= ("na" when qliq is unknown)."""
    imp = impact_pct(usd, qliq)
    return (f"impact_pct={'na' if imp is None else f'{imp:.4f}'} "
            f"qliq={'na' if _num(qliq) is None else round(qliq)} qliq_src={src}")


def buy_fill(price, usd, qliq, launchpad=""):
    """Buys fill above the quote. Returns (fill_price, friction_pct)."""
    pct = side_pct(usd, qliq, launchpad)
    return price * (1 + pct / 100), pct


def sell_fill(price, qty, qliq, launchpad=""):
    """Sells fill below the quote; side USD = qty x price. Returns (fill_price, friction_pct), friction capped at 100%."""
    pct = min(CAP_PCT, side_pct(qty * price, qliq, launchpad))
    return price * (1 - pct / 100), pct
