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
