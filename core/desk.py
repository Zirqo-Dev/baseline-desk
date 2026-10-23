"""EXECUTE: limits -> size off cash -> paper fill minus friction. Paper only."""
from core import config, friction
from core.book import ts


def size(cash, liq_usd):
    """Article sizing. Returns (usd, "") or (0, "size")."""
    pool_cap = liq_usd * config.SKIP_IF["max_ticket_share"]
    if cash < config.MIN_TICKET or pool_cap < config.MIN_TICKET:
        return 0.0, "size"
    usd = cash * config.SIZE_PCT
    usd = min(max(usd, config.MIN_TICKET), config.MAX_TICKET)
    return min(usd, pool_cap), ""


def entry_block(book, address, now):
    """Reason a new entry is refused, or "" if allowed."""
    if config.PAUSED:
        return "paused"
    if book.halted(now):
        return "loss_halt"
    if address in book.positions:
        return "holding"
    if len(book.positions) >= config.MAX_OPEN:
        return "max_open"
    return ""


def buy(book, address, facts, now):
    """Paper buy at facts["price"] plus friction. Logs one EXECUTE task row. Returns the position or None."""
    if config.PAPER is not True:
        raise RuntimeError("Baseline is paper only")
    sym = facts["symbol"]
    reason = entry_block(book, address, now)
    usd = 0.0
    if not reason:
        usd, reason = size(book.cash, facts["liq_usd"])
    if reason:
        book.task(now, "EXECUTE", sym, address, "SKIP", reason)
        book.save()
        return None

    fill, pct = friction.buy_fill(facts["price"], facts["liq_usd"], facts["launchpad"])
    qty = usd / fill
    book.s["cash"] -= usd
    pos = {
        "symbol": sym, "qty": qty, "orig_qty": qty, "cost_usd": usd,
        "entry_intended": facts["price"], "entry_fill": fill,
        "entry_ts": ts(now), "entry_liq_usd": facts["liq_usd"],
        "launchpad": facts["launchpad"], "pair": facts.get("pair", ""),
        "last_price": facts["price"], "peak_price": facts["price"],
        "ladder_done": [],
    }
    book.positions[address] = pos
    book.trade(now, {"side": "BUY", "symbol": sym, "address": address, "qty": qty,
                     "usd": round(usd, 6), "intended_price": facts["price"], "fill_price": fill,
                     "friction_pct": pct, "liq_usd": facts["liq_usd"],
                     "launchpad": facts["launchpad"], "reason": "entry", "pnl_usd": ""})
    book.task(now, "EXECUTE", sym, address, "BUY", "", f"usd={usd:.2f} fill={fill:.10g}")
    book.save()
    return pos


def sell(book, address, quote, qty, reason, liq_usd, now, intended=None, note=""):
    """Paper sell of `qty` tokens (None = all that remains) at `quote` minus friction.

    `intended` is the price the exit should have fired at (defaults to the quote);
    logging it next to the fill shows stop slippage. `note` goes to the trade row (e.g. "quote=fallback").
    Logs one trade row. Returns proceeds in USD.
    """
    if config.PAPER is not True:
        raise RuntimeError("Baseline is paper only")
    p = book.positions[address]
    qty = p["qty"] if qty is None else min(max(qty, 0.0), p["qty"])
    share = qty / p["qty"]
    fill, pct = friction.sell_fill(quote, liq_usd, p["launchpad"])
    proceeds = qty * fill
    cost = p["cost_usd"] * share
    pnl = proceeds - cost
    book.s["cash"] += proceeds
    p["qty"] -= qty
    p["cost_usd"] -= cost
    p["last_price"] = quote
    if share >= 1.0 or p["qty"] <= 0:
        del book.positions[address]
    book.trade(now, {"side": "SELL", "symbol": p["symbol"], "address": address, "qty": qty,
                     "usd": round(proceeds, 6),
                     "intended_price": quote if intended is None else intended,
                     "fill_price": fill, "friction_pct": pct, "liq_usd": liq_usd,
                     "launchpad": p["launchpad"], "reason": reason, "pnl_usd": round(pnl, 6),
                     "note": note})
    book.save()
    return proceeds
