"""EXECUTE: limits -> fixed ticket -> paper fill minus friction. Paper only.

BL-31 (both books, they share this code): a fixed TICKET_USD ticket, still capped by the pool (BL-32: qliq x
max_ticket_share, was liq_usd; below MIN_TICKET = SKIP size, also when cash is short); every SELL starts a COOLDOWN_H re-entry cooldown in that
book on the address AND the normalized symbol (EXECUTE SKIP reentry_cooldown); a buy whose normalized symbol
matches an open position of that book is refused as "holding".
BL-32: fills use core/friction.py (base + launchpad fee + impact 100 x side USD / qliq); the trades.csv note carries
impact_pct= qliq= qliq_src= (entry on a BUY; exit, entry or none on a SELL, see friction.exit_depth()).
"""
from datetime import timedelta

from core import config, friction
from core.book import ts
from core.verify import norm_symbol


def size(cash, qliq):
    """Fixed ticket (BL-31). Returns (usd, "") or (0, "size").

    usd = TICKET_USD capped by the pool (BL-32: qliq x max_ticket_share, the real depth, not liq_usd); "size" when
    that cap is under MIN_TICKET, qliq is unknown, or cash cannot pay the ticket."""
    if not isinstance(qliq, (int, float)) or isinstance(qliq, bool):
        return 0.0, "size"
    pool_cap = qliq * config.SKIP_IF["max_ticket_share"]
    if pool_cap < config.MIN_TICKET:
        return 0.0, "size"
    usd = min(float(config.TICKET_USD), pool_cap)
    if cash < usd or cash < config.MIN_TICKET:
        return 0.0, "size"
    return usd, ""


def _keys(address, symbol):
    """Cooldown keys: the address, and the normalized symbol when it has one."""
    s = norm_symbol(symbol)
    return [f"a:{(address or '').lower()}"] + ([f"s:{s}"] if s else [])


def start_cooldown(book, address, symbol, now):
    """BL-31: after a SELL (any rule), no re-entry in this book for COOLDOWN_H, by address and symbol. Prunes old keys."""
    cd = {k: u for k, u in (book.s.get("cooldown") or {}).items() if u > ts(now)}
    until = ts(now + timedelta(hours=config.COOLDOWN_H))
    for k in _keys(address, symbol):
        cd[k] = until
    book.s["cooldown"] = cd


def cooling(book, address, symbol, now):
    """BL-31: True while this book's cooldown holds the address or the normalized symbol."""
    cd = book.s.get("cooldown") or {}
    return any(cd.get(k, "") > ts(now) for k in _keys(address, symbol))


def holding(book, address, symbol):
    """The address is open in this book, or (BL-31) an open position has the same normalized symbol."""
    if address in book.positions:
        return True
    s = norm_symbol(symbol)
    return bool(s) and any(norm_symbol(p.get("symbol")) == s for p in book.positions.values())


def entry_block(book, address, now, symbol=""):
    """Reason a new entry is refused, or "" if allowed."""
    if config.PAUSED:
        return "paused"
    if book.halted(now):
        return "loss_halt"
    if holding(book, address, symbol):
        return "holding"
    if cooling(book, address, symbol, now):
        return "reentry_cooldown"
    if len(book.positions) >= config.MAX_OPEN:
        return "max_open"
    return ""


def entry_qliq(facts):
    """The real depth at entry, or None. Never raises. BL-27 stored it for display; since BL-32 it is the qliq that
    priced the buy (facts["qliq"], the same quote_liq() of the same pair as the logged trade qliq) and the exit's
    fallback depth (friction.exit_depth()). Falls back to the logged trade qliq for facts without a top-level qliq."""
    if not isinstance(facts, dict):
        return None
    q = facts.get("qliq")
    if q is None:
        t = facts.get("trade")
        q = t.get("qliq") if isinstance(t, dict) else None
    return q if isinstance(q, (int, float)) and not isinstance(q, bool) else None


def buy(book, address, facts, now, note="", buy_note=""):
    """Paper buy at facts["price"] plus friction. Logs one EXECUTE task row. Returns the position or None.

    `note` is appended to the EXECUTE row detail (the baseline logs its seed and pick there, BL-8).
    `buy_note` is appended after it on a BUY row only (the baseline's BL-25 trade keys); SKIP rows never carry it.
    """
    if config.PAPER is not True:
        raise RuntimeError("Baseline is paper only")
    sym = facts["symbol"]
    reason = entry_block(book, address, now, sym)
    usd = 0.0
    if not reason:
        usd, reason = size(book.cash, facts.get("qliq"))
    if reason:
        book.task(now, "EXECUTE", sym, address, "SKIP", reason, note)
        book.save()
        return None

    qliq = entry_qliq(facts)  # BL-32: the real depth at entry prices the buy
    fill, pct = friction.buy_fill(facts["price"], usd, qliq, facts["launchpad"])
    qty = usd / fill
    book.s["cash"] -= usd
    pos = {
        "symbol": sym, "qty": qty, "orig_qty": qty, "cost_usd": usd,
        "entry_intended": facts["price"], "entry_fill": fill,
        "entry_ts": ts(now), "entry_liq_usd": facts["liq_usd"],
        "launchpad": facts["launchpad"], "pair": facts.get("pair", ""),
        "last_price": facts["price"], "peak_price": facts["price"],
        "ladder_done": [],
        "entry_qliq": qliq,  # BL-27 display; BL-32: the exit friction's fallback depth (no exit rule reads it)
    }
    book.positions[address] = pos
    book.trade(now, {"side": "BUY", "symbol": sym, "address": address, "qty": qty,
                     "usd": round(usd, 6), "intended_price": facts["price"], "fill_price": fill,
                     "friction_pct": pct, "liq_usd": facts["liq_usd"],
                     "launchpad": facts["launchpad"], "reason": "entry", "pnl_usd": "",
                     "note": friction.note(usd, qliq, "entry")})
    book.task(now, "EXECUTE", sym, address, "BUY", "",
              " ".join(filter(None, [f"usd={usd:.2f} fill={fill:.10g}", note, buy_note])))
    book.save()
    return pos


def sell(book, address, quote, qty, reason, liq_usd, now, intended=None, note="", qliq=None):
    """Paper sell of `qty` tokens (None = all that remains) at `quote` minus friction.

    qliq (BL-32) = this tick's quote qliq, or None: friction.exit_depth() then uses the entry qliq, else thin_pct.

    `intended` is the price the exit should have fired at (defaults to the quote);
    logging it next to the fill shows stop slippage. `note` goes to the trade row (e.g. "quote=fallback").
    Logs one trade row. Returns proceeds in USD.
    """
    if config.PAPER is not True:
        raise RuntimeError("Baseline is paper only")
    p = book.positions[address]
    qty = p["qty"] if qty is None else min(max(qty, 0.0), p["qty"])
    share = qty / p["qty"]
    depth, src = friction.exit_depth(qliq, p.get("entry_qliq"))
    fill, pct = friction.sell_fill(quote, qty, depth, p["launchpad"])
    proceeds = qty * fill
    cost = p["cost_usd"] * share
    pnl = proceeds - cost
    book.s["cash"] += proceeds
    p["qty"] -= qty
    p["cost_usd"] -= cost
    p["last_price"] = quote
    if share >= 1.0 or p["qty"] <= 0:
        del book.positions[address]
    start_cooldown(book, address, p["symbol"], now)  # BL-31: any exit, partial or full
    book.trade(now, {"side": "SELL", "symbol": p["symbol"], "address": address, "qty": qty,
                     "usd": round(proceeds, 6),
                     "intended_price": quote if intended is None else intended,
                     "fill_price": fill, "friction_pct": pct, "liq_usd": liq_usd,
                     "launchpad": p["launchpad"], "reason": reason, "pnl_usd": round(pnl, 6),
                     "note": " ".join(filter(None, [note, friction.note(qty * quote, depth, src)]))})
    book.save()
    return proceeds
