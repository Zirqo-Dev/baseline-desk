BL-34 spec v1: survivor momentum (approved by the owner 2026-11-06, frozen)
Hypothesis: Base tokens still alive about 24 h after the desk first saw them, with real depth >= 10k USD and real sellers, that are beating ETH over the last 6 h, keep rising long enough to pay 8% round-trip friction; they beat a random pick from the same survivors and beat holding ETH.
DECISION TIME = the +24 h outcome label of a desk sighting (pool age 24.25-72 h).
UNIVERSE (as of the decision; a missing number = not in): sighting's desk verdict not too_young/too_old; +24 h label gone = 0; +24 h real depth (best_qliq, else qliq) >= 10,000 USD; +24 h pair_s24 >= 30; pool match exact, single or age.
ENTRY (all): token 6 h return > 0 (candles that ended before the decision); token 6 h return > ETH 6 h return; REGIME ETH 24 h return >= -3%.
EXITS (fixed before entry): hard stop -25% (inside a candle: worse of stop and close); ladder +40% sell 1/3, +100% sell 1/3; trail 25% from the peak after the first rung, earlier candles only; max hold 72 h; NO 45-min time stop.
TICKET 5 USD, one trade per token. FRICTION per side: 3% + 1% pool fee on EVERY trade + impact 5 USD / qliq at +24 h.
TWIN: at each strategy entry time t, a random pick among universe tokens (universe rules only) whose +24 h decision fell in (t - 6 h, t], with a qualified candle line, entered at t, same exits and ticket; empty pool = twin_skip (counted).
HOLD-ETH: ETH return from each trade's entry fill to its exit fill, no friction.
PASS (holdout, once; all five): (1) >= 20 closed trades; (2) mean net P&L per trade after friction > 0; (3) > the twin's mean net per trade; (4) mean return per trade > hold-ETH over the same windows; (5) max drawdown of the cumulative per-trade P&L (by exit time) on a 50 USD book < 30%. Under 20 closed = inconclusive (forward paper vs twin). Fails 2, 3 or 4 = retired. Always reported, never the verdict: no_fill rate, pending, twin_skip, stop fill "level", tick-like close-only exits.
