# Technical notes

Moved from CLAUDE.md section 11 on 2026-10-23 (BL-2 process cleanup). Wording is unchanged; section 11 keeps only the short state.

## BL-4
- BL-4: core/config.py, friction.py, book.py, desk.py. data/state.json holds each book by name ("desk", later "baseline"):
  cash, positions, day, day_start_value, halted_day. desk.buy() logs one EXECUTE task row (BUY or SKIP) and saves
  facts["pair"] on the position; desk.sell(qty) logs the trade row with intended vs fill price (+ "note" column).
  book.load_state()/save_key() write one state key each.

## BL-5
- BL-5: core/exits.py check(book, quote, now): 6 rules in order liq_pull, hard_stop, ladder_1, ladder_2, trail, time_stop;
  one exit per position per run; every check logs one EXIT task row (SELL / HOLD / HOLD no_quote). Gains vs FILLED entry.
  A fallback quote adds "quote=fallback" to the EXIT row and the trade row note.
- BL-5/6: core/verify.py hard_skip(facts): fetch_error if facts["fetch_errors"] is non-empty, then goplus_partial (final, BL-7),
  then missing:<field> (or goplus_not_ready when every gap is in facts["goplus_pending"];
  liquidity under the minimum still final), then honeypot, mint_open, freeze_or_blacklist, risk_flag:<flag> (HARD_FLAGS),
  sells_1h == 0 -> "nobody_can_sell", then SKIP_IF.

## BL-6
- BL-6: adapters/base.py. scan() = GeckoTerminal new_pools page 1 + DexScreener latest profiles/boosts on Base, 3 requests,
  deduped, WETH/ETH/stables dropped. facts(address) = 15 fields + "pair" + "goplus_pending" + "fetch_errors", 2 requests
  (3 if the age needs GeckoTerminal); field mapping in the module docstring; sells_1h summed over all Base pairs with the token.
  quote(address, pair) (BL-7) = our pool via DexScreener latest/dex/pairs, else deepest Base pair ("fallback"); 1-2 requests.
  Client paces per section 8; 429/5xx/timeout/bad JSON -> FETCH_ERROR (retried), other 4xx -> None (missing data).
  GoPlus body code: 1 ok; 3 -> facts["goplus_partial"] (final skip); other -> fetch error + facts["goplus_code"],
  logged as goplus_code=<n> in the RETRY / fetch_error_x3 detail.
  CLI: `python -m adapters.base check <address>` / `scan` / `quote <address> [pair]`.
- BL-6: core/leads.py Leads(root): skipped(), due(), record(address, facts, now) -> one VERIFY row (PASS/WATCH/RETRY/SKIP) with
  first_seen, checks, pending, goplus_ready_at, try, hosts, until. state.json "leads": skips {until},
  watch {first_seen, last_check, checks}, retry {tries} (fetch_error: 3 tries in a row, then final fetch_error_x3).

## BL-7
- BL-7: core/tick.py run() / `python -m core.tick`: roll day -> exits (quote per position) -> unless PAUSED or loss_halt:
  scan -> queue (due watch oldest first, then fetch_error retries, then new leads; held-at-start tokens skipped, no row)
  -> at most MAX_CHECKS = 20 facts() and at most config.GOPLUS_MAX_PER_TICK = 9 GoPlus calls (every facts() makes one,
  so 9 checks a tick today; the rest deferred, no row, not a retry try) -> Leads.record() -> desk.buy() on PASS.
  One TICK row per tick: positions seen rechecks new checked passed bought held deferred goplus requests secs;
  reason paused/loss_halt/error:<type>.
  .github/workflows/tick.yml: workflow_dispatch only (on main), concurrency "tick", 10 min timeout, Python 3.13,
  commit step if: always(), "data: tick ..." only on change, pull --rebase, retry once. Run red on a crash.
- BL-7 smoke 2026-10-23 (live, scratch folder, nothing committed): 41 requests in 38 s (DexScreener 21, GoPlus 19,
  GeckoTerminal 1, all HTTP 200). 19 leads, 19 checked: 13 liquidity, 1 missing:lp_locked_pct, 1 missing:liq_usd (NDOF, ZORA pair),
  2 WATCH goplus_not_ready, 2 RETRY fetch_error (GoPlus answered 200 with a non-OK code, both addresses 0xb2000000...), 0 PASS.
  quote() checked live on BRETT's own pool: 1 request, source pool.
- BL-7 scheduler: GitHub's schedule fired 0 times in 35 min after the first manual run (07f3457 -> e13340f). Since 2026-10-23
  tick.yml is triggered by cron-job.org (POST workflow_dispatch every 5 min, :03 ... :58 UTC) with its own fine-grained token
  scoped to baseline-desk only (Actions: read and write). Single point of failure: if cron-job.org stops or the token
  expires, the desk stops SILENTLY (no red run, just no TICK rows). Note the token expiry date and renew before it.
  cron-job.org live since 2026-10-23 15:28 UTC. By 18:53 UTC: 41 of 42 expected runs (97.6%), no error TICK rows, no overlap,
  one TICK row each; 17:18 was never dispatched (no Actions run exists for it). 24h window ends 2026-10-24 15:28 UTC.

## GoPlus diagnosis 2026-10-23
- GoPlus diagnosis 2026-10-23 (read-only, tasks.csv to 18:53 UTC + 9 live GoPlus requests). Two separate causes, both
  logged as fetch_error because adapters/base.py treats any GoPlus "code" != 1 as a rate limit:
  - 0xb2000000... vanity addresses: GoPlus answers HTTP 200, code 3, message "OK", with a 10-field partial entry
    (token_name/symbol, total_supply, holder_count 0, buy_tax/sell_tax "", no honeypot/mintable fields). Still code 3
    5.5 h after first sight (JEVO). 35 addresses, 103 rows, 0 ever got a clean check (34 fetch_error_x3, 1 retrying).
    Code 3 = partial data is our reading, not GoPlus docs (asserted). Who deploys them is unconfirmed.
  - All others: never fail in a tick's first 10 checks (0 of 369 rows), fail at check 11+ (most of them) -> GoPlus
    budget is about 10 calls per tick at 2 s pacing. The code they get is NOT logged and was not reproduced live
    (would need 11+ calls). 120 addresses, 195 rows; 104 later got a clean check, 14 lost to fetch_error_x3, 2 retrying.
  - Both fixes BUILT 2026-10-23 (Pete chose them, DECISIONS): code 3 -> "goplus_partial" final 24h skip, other codes
    logged as goplus_code=<n>; GOPLUS_MAX_PER_TICK = 9. Live smoke (4 requests): WIZARD 0xb2000000... -> goplus_partial,
    BRETT -> missing:lp_locked_pct (known LP-lock issue), all HTTP 200.
  - Reported, not built: facts() calls GoPlus even when DexScreener alone already makes the lead a final skip.
    tasks.csv to 19:13 UTC: at least 154 of 757 checks (20%: 134 liquidity, 20 missing:liq_usd) wasted a GoPlus call,
    about 2 of the 9 per tick. Pete decides in chat whether that becomes a new card.

## Open items (detail)
- Open items:
  - BL-7 done only after 24h of green runs, no overlap, one TICK row per run. The TICK rows show the real run rate for Gate A (90%).
  - With the cap, a busy tick checks only 9 leads, so "deferred" will rise; new leads deferred come back only if the next
    scan finds them again. Watch whether watched leads still get their 15-min re-checks on time.
  - LP lock: GoPlus can't tie lp_holders to our pool unless it lists exactly one pool, so many leads die on
    missing:lp_locked_pct (Uniswap v4 may never have LP holders). Pete decides after 24-48h of BL-7 skip-reason data.
  - Clanker/Zora factory addresses unconfirmed; launchpad is pool-shape only until BL-10 (CLANKER_CREATORS/ZORA_CREATORS empty).
  - top10 is the top 8-10 after exclusions (GoPlus returns only 10 holders); slightly lenient.
  - GoPlus rate limit is detected as any non-OK "code" in the body (fetch_error); the exact rate-limit code is unverified live
    (code 3 is NOT a rate limit, see GoPlus diagnosis above).
  - Week-1 review of the watchlist: if more than about 1/3 of watched leads time out, Pete may raise WATCH max_h to 6.

## Next step (full text before the 2026-10-23 cleanup)
- Next step: confirm the GoPlus fixes live from tasks.csv after the push: TICK rows show goplus<=9, no fetch_error with
  hosts=goplus at check 10+, 0xb2000000... leads get one SKIP goplus_partial, and any goplus_code=<n> seen is the real
  rate-limit code (write it in section 11). Pete decides on the "GoPlus wasted on DexScreener-only skips" card.
  After 2026-10-24 15:28 UTC count TICK rows since 15:28 (expect 288) and runs in the Actions tab; if green with one
  TICK row per run, mark BL-7 done, then BL-8 (report.py + report.yml).
