# Changelog (append-only, never edit old lines)

| Date | Card | Commit | What changed | Next step |
|---|---|---|---|---|
| 2026-10-23 | BL-2 | ea144b5 | Bootstrap knowledge: CLAUDE.md, DECISIONS, LEARNINGS, README, .gitignore | Confirm Actions write permission (BL-1), then BL-4 to BL-8 |
| 2026-10-23 | BL-4 | 0f87705 | config, friction, book, desk + 7 tests (round trip -5.8% at 150k, -9.5% at 60k) | BL-5 exits |
| 2026-10-23 | BL-5 | 51a04ba | exits.py (6 rules, intended vs fill), verify.py hard_skip() incl. nobody_can_sell, desk.sell(qty) + 19 tests | BL-6 raw API fetch + field mapping |
| 2026-10-23 | BL-5 | 98e3560 | Wrap-up: CLAUDE.md section 11, BL-5 hash in CHANGELOG, BL-6 blocked on network | Open network for the 3 API hosts |
| 2026-10-23 | BL-6 | 8bdee7c | Raw DexScreener, GoPlus, GeckoTerminal samples in tests/fixtures/ + field mapping; Baseline environment decision | Pete reviews mapping, then adapter code |
| 2026-10-23 | BL-6 | 043f0e5 | changelog hash rule (CLAUDE.md section 10) + GoPlus readiness probe, no code (GoPlus empty for all 6 tokens under 1h; 1 address per call; GeckoTerminal ~20 min per page, 429 on page 3) | adapter code |
| 2026-10-23 | BL-6 | pending | adapters/base.py scan()/facts()/CLI, core/leads.py skip memory + GoPlus watchlist + fetch_error retries (3 then final), verify risk_flags + goplus_not_ready + fetch_error, sells_1h summed over all pairs, WATCH in config, one-writer rule in CLAUDE.md, 42 new tests (68 pass), smoke 8 requests | BL-7 tick loop |
