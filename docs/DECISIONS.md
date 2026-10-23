# Decisions (append-only)

| Date | Card | Decision | Why | Rejected |
|---|---|---|---|---|
| 2026-10-22 | BL-1 | Name: Baseline | The random baseline judges every trade | Desk Lab, Base Jump, BaseCase |
| 2026-10-22 | BL-1 | Base only (chain 8453) | Free GoPlus safety data; Pete knows Base | Robinhood (no free verify), Solana (bot competition), Cardano (TapTools gone) |
| 2026-10-22 | BL-1 | Fully separate from HoodScout | Clean scope; no shared data or workflows | Building it as HoodScout layer 5 |
| 2026-10-23 | BL-1 | Repo lives at Zirqo-Dev/baseline-desk (personal account), public; separate GitHub org deferred | Pete created it there; still its own repo and Pages path, free Actions on public repos | Org now (can transfer later), second personal account (against GitHub terms), GitHub Pro |
| 2026-10-22 | BL-1 | Paper only; engine on GitHub Actions for now | $0, no Linux needed | VPS now (deferred to BL-15) |
| 2026-10-23 | BL-4 | Config uses the article's numbers adapted for Base; no max_risk_score | Rugcheck is Solana only; GoPlus fields cover safety on Base | Own defaults; keeping max_risk_score |
| 2026-10-23 | BL-4 | Sizing: usd = cash x SIZE_PCT, clamp to MIN..MAX_TICKET, cap at liq_usd x max_ticket_share; skip "size" only if cash < MIN_TICKET or pool cap < MIN_TICKET | Article formula; never trade bigger than 1% of the pool | Rounding up past the pool cap |
| 2026-10-23 | BL-4 | Loss halt: total value (cash + open at last price) vs day_start_value set at the first run of each UTC day; once tripped, no entries until next UTC day, exits still run | Simple, resets daily, never blocks exits | Vs START_BALANCE; vs peak |
| 2026-10-23 | BL-4 | Friction moves the fill price: buy = quote x (1+pct), sell = quote x (1-pct); pct = 3% (5% under 100k liq) + launchpad pool fee per side | Round trip ~5.8% / ~9.5%, honest and testable | Flat USD fee |
| 2026-10-23 | BL-4 | One state.json with books keyed by name | Baseline book reuses the same code and file | One state file per book |
| 2026-10-23 | BL-4 | This session commits to its branch, pushes, then fast-forwards main if allowed (Pete, this session only) | Cloud session is set up on a branch | Committing straight to main |
| 2026-10-23 | BL-5 | Exit gains measured from the FILLED entry price | Friction included, paper must not flatter | Quoted entry price |
| 2026-10-23 | BL-5 | Six exit rules, one per position per run, in order: liq_pull, hard_stop, ladder_1 (+40%), ladder_2 (+100%), trail, time_stop | Safety exits first; ladder counted as two rules | Firing several rules in one run |
| 2026-10-23 | BL-5 | Each ladder rung sells 33% of the ORIGINAL size; the 34% left rides the trail | Pete's rule; predictable size per rung | 33% of the remainder |
| 2026-10-23 | BL-5 | Trail only after the first rung; time stop only before the first rung | A winner is not cut by the clock; the trail protects gains only | Trail from entry; time stop always on |
| 2026-10-23 | BL-5 | Liq pull and hard stop close the whole position; trail and time stop close whatever remains | Safety exits never leave a tail | Partial safety exits |
| 2026-10-23 | BL-5 | Intended price = level the rule should fire at (stop or trail level, rung level, or quote for liq_pull / time_stop); fill = quote minus friction | Shows stop slippage on gaps | Logging the quote only |
| 2026-10-23 | BL-5 | No quote or no liquidity number at exit time = HOLD (no_quote), retried next run | A missing number can't trigger a sell; the position must still be watched | Force-selling on missing data |
| 2026-10-23 | BL-5 | hard_skip() lives in new core/verify.py; top10_pct is in the missing-data check | Article bug fix; keeps VERIFY separate from EXECUTE and EXIT | Inside core/desk.py |
| 2026-10-23 | BL-5 | sells_1h == 0 is a hard skip, reason "nobody_can_sell" (article rule); no risk_flags rule yet, decided at BL-6 on real GoPlus data | Zero sells in an hour is a honeypot sign GoPlus can miss | A risk_flags rule before seeing real data |
| 2026-10-23 | BL-4 | CHANGELOG BL-4 line edited once to put in the real hash 0f87705 (Pete asked) | A commit can't carry its own hash | Leaving "this commit" |
