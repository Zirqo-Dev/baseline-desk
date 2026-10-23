"""Paper fill friction: fees + slippage per side. Paper must not flatter."""
from core import config


def side_pct(liq_usd, launchpad=""):
    """Friction for one side, in percent."""
    f = config.FRICTION
    pct = f["thin_pct"] if liq_usd < f["thin_liq_usd"] else f["base_pct"]
    return pct + f["pool_fee_pct"].get((launchpad or "").lower(), 0.0)


def buy_fill(price, liq_usd, launchpad=""):
    """Buys fill above the quote. Returns (fill_price, friction_pct)."""
    pct = side_pct(liq_usd, launchpad)
    return price * (1 + pct / 100), pct


def sell_fill(price, liq_usd, launchpad=""):
    """Sells fill below the quote. Returns (fill_price, friction_pct)."""
    pct = side_pct(liq_usd, launchpad)
    return price * (1 - pct / 100), pct
