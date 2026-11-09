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
- BL-7 scheduler token: the cron-job.org fine-grained token "baseline-desk cron" EXPIRES Thu 2027-01-21. Renew it by early
  January 2027 (new token, paste into the cron-job.org job header), or the desk stops silently.
- BL-7 closed by chat 2026-10-25 from tasks.csv: 287 of 288 TICK rows in the 24h window (only 17:18 UTC Oct 23 missing),
  288 of 288 on Oct 24 and Oct 25, 0 error TICK rows, no overlap. Post-fix (5,469 checks): 0 RETRY, 0 fetch_error,
  GoPlus never above 9 per tick, 0 PASS, 0 trades. 1,653 checks (30%) were DexScreener-only skips that still spent a
  GoPlus call (-> BL-20). 438 leads watched, 268 (61%) ended goplus_timeout.

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
  - Both fixes BUILT 2026-10-23 (the owner chose them, DECISIONS): code 3 -> "goplus_partial" final 24h skip, other codes
    logged as goplus_code=<n>; GOPLUS_MAX_PER_TICK = 9. Live smoke (4 requests): WIZARD 0xb2000000... -> goplus_partial,
    BRETT -> missing:lp_locked_pct (known LP-lock issue), all HTTP 200.
  - Reported, not built: facts() calls GoPlus even when DexScreener alone already makes the lead a final skip.
    tasks.csv to 19:13 UTC: at least 154 of 757 checks (20%: 134 liquidity, 20 missing:liq_usd) wasted a GoPlus call,
    about 2 of the 9 per tick. The owner decides in chat whether that becomes a new card.
    -> Built as BL-20 (2026-10-26), see below.

## BL-20
- DexScreener first (DECISIONS 2026-10-26). adapters/base.py facts(address, now, client, goplus_ok=None): DexScreener
  token-pairs, then GeckoTerminal only when pairCreatedAt is missing (today's flow, no request added to decide), then
  core/verify.py dex_skip() on what is in hand. Gate order: 1. fetch_error (any request so far failed: ds_pairs, or gt
  for the age), 2. missing:<first of symbol, price, liq_usd, age_min, sells_1h> (DEX_FIELDS, REQUIRED order),
  3. liquidity (liq_usd < SKIP_IF min_liquidity_usd). If it fires: no GoPlus call, facts carry "goplus_skipped": True
  (no launchpad, no goplus_pending), hard_skip() returns the dex_skip() reason, Leads.record() logs the VERIFY row as
  before plus "goplus=skipped" (also on RETRY rows), and never adds goplus_ready_at. Otherwise, if goplus_ok() is
  False, facts() returns None (deferred); else GoPlus and hard_skip() exactly as before.
- Cap then continue (core/tick.py): todo = queue[:MAX_CHECKS = 20]; each lead gets facts() with
  goplus_ok = GoPlus calls this tick < GOPLUS_MAX_PER_TICK (9). None -> deferred (no row, not a retry try; watch and retry
  state untouched, so the same queue order next tick; a deferred NEW lead returns only if the scan lists it again,
  as before). A deferred lead still spent its DexScreener request (and GeckoTerminal if needed).
  TICK row: ... deferred dex_only goplus requests secs. dex_only = checks settled without GoPlus (part of checked).
- Label shift (accepted by the owner 2026-10-25, test tests/test_verify.py DexFirstTest): only labels move, nothing moves to
  WATCH or PASS. Low liquidity + honeypot / mint_open / freeze / risk_flag / goplus_partial / nobody_can_sell /
  missing:<GoPlus field> -> "liquidity". Found while building: a token with no DexScreener pair whose GoPlus answer is
  code 3 was "goplus_partial", now "missing:symbol" (both final 24h). A low-liquidity (or DexScreener-gap) lead whose
  GoPlus call would have failed was RETRY fetch_error (up to 3 ticks), now a final skip at once; GoPlus fetch errors
  were 0 in 5,469 post-fix checks.
- Request budget, worst case per tick: exits 4 positions x 2 DexScreener = 8; scan 1 GeckoTerminal + 2 DexScreener;
  checks 20 DexScreener + up to 20 GeckoTerminal (only when pairCreatedAt is missing; 0 in the smoke) + 9 GoPlus.
  DexScreener pairs 28 per tick (limit 300/min, paced 0.2 s), profiles/boosts 2 (60/min), GeckoTerminal up to 21 per
  tick paced 2 s = at most 30/min (at its limit only in that worst case; was 10 before), GoPlus 9 (unchanged).
  Time: real ticks so far median 17.6 s, max 39.8 s (the pre-cap Oct 23 ticks did 20 checks with 20 GoPlus calls).
  Theoretical: if every request took the full 15 s client timeout and still answered, 60 requests = 900 s, over the
  10 min job timeout (before BL-20 the same bound was 38 requests = 570 s). A request that fails settles the lead
  (fetch_error) without GeckoTerminal or GoPlus, so all hosts down = 27 requests = 405 s. No time guard built.
- Live smoke 2026-10-26 (scratch folder, nothing under data/): one full tick, 31 requests in 16.1 s, all HTTP 200:
  DexScreener 22, GoPlus 8, GeckoTerminal 1. 21 leads, 20 checked, 1 deferred (over MAX_CHECKS), dex_only 12
  (9 liquidity, 3 missing:liq_usd), 8 GoPlus checks -> 8 WATCH goplus_not_ready, 0 PASS.
- Open item: Theoretical worst case: ~900 s if every request hangs 15 s (job timeout 600 s); real ticks 17.6 s typical, 39.8 s max. If a tick ever exceeds 120 s, add a time guard.

## BL-20 review (chat, 2026-10-28)
- 70 h (Oct 26 01:04 to Oct 28 23:13 UTC); 836 of 841 ticks; 0 error ticks; median 16.5 s, max 35.9 s.
- 9,180 checks, 42% dex_only; GoPlus 6.4 per tick (max 9); deferred 2.8 per tick (BL-7: 6.9).
- 4,356 leads, 631 watched, 427 timed out (68%, BL-7: 61%); 0 PASS.
- 405 of 427 timeouts still had honeypot + taxes pending, and none had only lp_locked_pct pending.
- 39 leads died on missing:lp_locked_pct.
- Verdict: BL-20 kept. Honeypot/tax silence is the bigger wall; revisit BL-22 or WATCH max_h on BL-21 data.
- WATCH max_h stays 3 h (the owner, 2026-10-25, DECISIONS).

## BL-21
- Unknown LP lock allowed (the owner, option A, 2026-10-25; DECISIONS). core/verify.py: LP = "lp_locked_pct".
  hard_skip() leaves lp_locked_pct out of the missing-data gate: None (missing, or listed in goplus_pending) is
  "unknown" and the lead goes on through every other rule in the same order. missing:<field> reports the first gap
  in REQUIRED order with lp_locked_pct skipped over; goplus_not_ready still fires when other GoPlus fields are
  pending (the WATCH row's pending= list may still name lp_locked_pct). The LP rule stays in its place (after
  liquidity, before dev_pct) and fires only on a known number: lp_locked_pct < min_lp_locked_pct (90) -> "lp_locked".
- lp_unknown(facts): True when every REQUIRED field but lp_locked_pct is present, lp_locked_pct is None, and the
  check is not fetch_error, goplus_partial or goplus=skipped (i.e. it got past the missing-data gate).
  core/leads.py Leads.record() then appends "lp_unknown" to the VERIFY row detail, on PASS and SKIP alike.
- Watchlist release: goplus_ready_at is added when no pending field other than lp_locked_pct is left, so a watched
  lead waiting only on LP leaves with its real outcome (PASS -> desk.buy() in the tick, or a final SKIP), never WATCH
  or goplus_timeout. dex_skip(), WATCH max_h (3 h) and core/config.py unchanged.
- Tests: tests/test_verify.py LpUnknownTest (7) + 2 in tests/test_leads.py. Old tests that asserted
  missing:lp_locked_pct now assert the new rule (BRETT fixture ends too_old; two stand-ins use top10_pct).
- Live smoke 2026-10-28 23:21 UTC (scratch folder, nothing under data/): one full tick, 27 requests in 9.7 s
  (DexScreener 22, GoPlus 4, GeckoTerminal 1). 21 leads, 20 checked, 1 deferred (over MAX_CHECKS), dex_only 16
  (14 liquidity, 2 missing:liq_usd), 4 GoPlus checks -> 4 WATCH goplus_not_ready (all still pending honeypot + taxes),
  0 lp_unknown, 0 PASS, 0 buys.

## BL-8
- Random baseline shadow book (the owner's 7 picks 2026-10-29, DECISIONS). core/baseline.py: book "baseline" in state.json
  beside "desk", run by the same core/book.py, desk.buy(), desk.sell() and exits.check(), so START_BALANCE, sizing,
  friction, the 6 exit rules, quote logic, day roll and loss halt are the desk's; only the pick differs. Its EXECUTE,
  EXIT and trade rows go in the same tasks.csv / trades.csv with book=baseline. Extra state key "ticks_since_buy".
- Tick order (core/tick.py): load desk, baseline, leads, report state -> on the UTC day roll keep each book's closing
  value, then roll both books -> if a report is due, core/report.py writes data/report.json (own try) -> held sets
  for both books (before exits) -> baseline counter +1 -> desk exits -> baseline exits -> unless PAUSED or desk
  loss_halt: desk scan/verify/buy exactly as before, collecting seen (each facts() lead's DexScreener side) and
  verdicts (the desk's outcome) -> if due, one baseline pick -> save -> running peak / max drawdown -> TICK row.
- Eligible (baseline.eligible): leads in this tick's todo that got a facts() call, checked or deferred by the GoPlus
  cap, whose dex dict passes dex_skip() and has sells_1h > 0, minus tokens the baseline held at the tick start.
  adapters/base.py facts(..., dex=None) fills dex right after the DexScreener (+GeckoTerminal age) step, before
  dex_skip() and the GoPlus cap: symbol, price, liq_usd, age_min, sells_1h, pair, fetch_errors, and launchpad =
  launchpad({}, ...) (pool shape only: ZORA quote -> zora; v4 + 1% fee from the GeckoTerminal pool name, only fetched
  when DexScreener has no pairCreatedAt -> clanker; else other). Return value unchanged (test FactsDexTest).
  Not eligible because never fetched this tick: leads past MAX_CHECKS, under a 24h skip, not due on the watchlist,
  or held by the desk. So the universe is mostly first-seen leads.
- Cadence: "ticks_since_buy" +1 every tick (also paused or halted), due at >= 12, reset to 0 only on a BUY.
  First baseline buy on the 12th tick after deploy. Due + scan ran: one EXECUTE row, BUY or SKIP (no_eligible with
  detail eligible=0; max_open / loss_halt / size with the pick detail). Due + no scan (PAUSED, desk loss_halt): no row.
  MAX_OPEN counts each book's own positions; the baseline runs after the desk, with its own cash.
- Pick and replay: seed = int(tick time as YYYYMMDDHHMM); pool = eligible addresses sorted; pick =
  random.Random(seed).randrange(len(pool)). BUY detail: usd= fill= seed= eligible= pick= pool=<6-hex prefixes>
  desk=<PASS | WATCH | deferred | SKIP or RETRY reason> launchpad=<pool shape>. Replay: rebuild the sorted pool from
  the prefixes, take pool[randrange] with that seed, compare with the row's address.
- Known biases, both flatter the baseline (Gate B stricter for the desk, kept on purpose, the owner): (1) pool-shape
  launchpad usually misses the Clanker 1% fee, DexScreener pairs carry no fee; (2) the baseline can buy tokens GoPlus
  flagged (honeypot, taxes) or has not read yet; the desk= tag measures it.
- core/report.py: state key "report" = since (first BL-8 day; Gate B closes count from it), due (closed day still to
  report), equity {book: [{day, value_usd, cash_usd, open}]} taken at the roll before exits (last-price marks),
  peak and max_dd_pct per book updated at the end of every tick. Report values come from that day's closing entry;
  closes / win / exits / slip from all trades.csv files, SELLs from since to the report day. Close = a SELL with any
  rule but ladder_1/ladder_2; win/loss % per position = sum of its SELL pnl_usd / BUY usd; stop_slip_pct = mean
  (fill / (1 - friction_pct/100) / intended - 1) x 100 over hard_stop and trail SELLs. Top level: as_of, day,
  start_balance, desk_lead_usd, gate_b, summary, then desk and baseline. The tick logs "REPORT <summary>".
  By hand: python -m core.report [root] = day today so far at current marks, reads state, writes <root>/data/report.json
  (in a session never commit it).
- Baseline failure isolation (the owner, review 2026-10-29): the baseline exits, the baseline pick/buy (with its eligible
  count) and the baseline save + drawdown step each run in their own try (core/tick.py bl_try). An error logs one
  row book=baseline action=ERROR reason=error:<type> (stage EXIT, EXECUTE or BOOK), TICK detail bl_error=<type>
  (bl_error=none otherwise; the first error if several); the desk's exits, verify, buy and saves still run; the
  baseline buy is skipped for the rest of the tick after a baseline exit error (stays due; a failed buy also stays
  due); the run raises after the TICK row (red). Tests: a baseline exit error and a baseline buy error each leave desk
  rows, desk and leads state, and the TICK desk keys (requests included) identical to a run without the baseline.
  Not isolated: loading the baseline book from state.json, its day roll and counter, and the report snapshot at the
  roll (simple dict code before exits); a broken "baseline" state key still crashes the tick as any state error does.
- Liquidation value (the owner, review 2026-10-29, CLAUDE.md section 6): report.liq_value(book) = cash + each open
  position x friction.sell_fill(last_price, last_liq_usd (else entry_liq_usd), launchpad), the fill a real exit
  would get now. Used for value_usd, net_pnl_usd/pct, multiple, equity_daily (snapshot at the roll), desk_lead_usd
  and max_dd_pct (tracked every tick; the report also counts the drop to the value it shows), both books alike.
  Book.value() (last price, no friction) still drives the loss halt, unchanged. exits.check() now passes the
  quote's liquidity to Book.mark(), which keeps it as "last_liq_usd" on the position (desk positions too: a new
  state key, no row or verdict change). A just-bought position has no last_liq_usd yet and uses its entry liquidity.
- Report failure: own try inside the tick; TICK detail report=error:<type>; exits, scan and buys still run; "due"
  stays set and the next tick retries; after the TICK row the run raises (red). report=ok on the tick that wrote it,
  report=none otherwise.
- TICK detail: desk keys unchanged and first (positions ... dex_only goplus requests secs), then bl_positions bl_due
  bl_eligible bl_bought report bl_error. requests includes the baseline exit quotes.
- Request budget, worst case per tick, added by BL-8: baseline exits 4 positions x 1 DexScreener pairs request
  (x 2 on fallback) = 4-8; selection 0; GoPlus 0; GeckoTerminal 0. DexScreener pairs 28 -> 36 per tick against
  300/min (paced 0.2 s, about +1-3 s); profiles/boosts 2 (60/min) and GeckoTerminal up to 21 (30/min) unchanged;
  GoPlus 9 unchanged. Theoretical hang bound (every request takes the full 15 s): 60 -> 68 requests = ~1,020 s
  (was ~900 s; job timeout 600 s). Open item kept, no time guard in BL-8 (the owner).
- Tests: tests/test_baseline.py (23): eligibility (sells_1h 0, goplus=skipped, fetch_error, held excluded; deferred
  and WATCH/SKIP-by-GoPlus included), real facts() dex dict (same return value, deferred keeps DexScreener facts with
  no GoPlus call, pool-shape launchpad), cadence (12th and 24th tick, retry on no_eligible, paused), separate
  MAX_OPEN both ways, replay from the seed, baseline exits book=baseline, report fields on a day roll, report failure
  + retry, by-hand all zeros, book_stats (ladder not a close, per-position win/loss, slip, since/upto window),
  desk unchanged (same ticks with and without the baseline: desk rows, desk and leads state, desk TICK keys).
  Old tests: the fake facts() takes dex; the heartbeat test checks the desk keys then the new keys. 144 pass.
  Review fixes (+6): baseline exit error and buy error isolation (DeskUnchangedTest), LiquidationValueTest (4:
  sell friction off the last quote, last liquidity and launchpad, equals a real exit's cash, report fields). 150 pass.
- Live smoke 2026-10-29 (scratch folder, nothing under data/):
  - Tick on a copy of the live state (c29b1ec), baseline due: 6 requests in 2.0 s, all HTTP 200 (DexScreener 5,
    GeckoTerminal 1). 18 seen, 15 under a 24h skip, 3 new, all dex_only -> 0 eligible -> baseline SKIP no_eligible,
    stays due.
  - A second run seconds later got GeckoTerminal 429 (shared IP throttle, section 8): 0 leads.
  - Tick on a fresh state a minute later, baseline due: 27 requests in 8.3 s, all HTTP 200 (DexScreener 21,
    GoPlus 4, GeckoTerminal 2). 19 leads, 19 checked, dex_only 15 (13 liquidity, 1 missing:price, 1 missing:symbol),
    4 WATCH goplus_not_ready, 0 PASS. 4 passed the DexScreener gate, 2 with sells_1h > 0 -> eligible 2.
    Baseline BUY CYBER 0xfd27f8...: seed 202610290253, pick 1 of 2 (replayed: randrange(2) = 1), desk=WATCH,
    launchpad=other, 5.00 USD at 5.206e-05, fill 5.4663e-05, friction 5% (liquidity 51.5k < 100k, no pool fee).
  - python -m core.report by hand: desk 50.00 (all zeros), baseline 49.76 (1 open, -0.48%, max_dd 0.48%),
    desk_lead_usd +0.24. After the liquidation-value fix, same state: baseline 49.52 (CYBER 4.76 -> 4.52 after its
    5% sell friction; -0.95%, multiple 0.9905, max_dd 0.95%), desk_lead_usd +0.48. One baseline exit quote (python -m adapters.base quote): 1 request, 0.43 s, source pool.

## BL-22 probe
- Step 1, read-only, 2026-10-30 03:32-03:33 UTC (the owner allowed api.honeypot.is for BL-22). Question: can honeypot.is fill
  honeypot + buy/sell tax where GoPlus stays silent on new Base tokens? Nothing under core/, adapters/, config.py,
  workflows or data/ was touched; the script ran from the session scratchpad, outside the repo.
- API as used: GET https://api.honeypot.is/v2/IsHoneypot?address=<token>&chainID=8453, no key, one request at a time,
  1.2 s apart, 24 requests (limit set at 30). NOT confirmed from their docs: docs.honeypot.is and honeypot.is are outside the
  session allowlist (the web-fetch agent got ENOTFOUND; no other host or proxy tried). The endpoint is the one named in the
  task and it answered, and the field names below come from the two saved samples, not from documentation.
- Fixtures: tests/fixtures/bl22_honeypotis_hit.json (EDEL, 200) and bl22_honeypotis_miss.json (USDL, 404). Not used by any test yet.
- Response shape (hit): simulationSuccess (bool), honeypotResult.isHoneypot (bool), simulationResult.buyTax / sellTax /
  transferTax (numbers, percent) + buyGas / sellGas, flags[], summary.risk / riskLevel, contractCode.{openSource,isProxy,...},
  router (address), pair.pair.{name, address, type e.g. "UniswapV3", token0, token1}, pair.liquidity, pair.createdAtTimestamp,
  pairAddress, chain.id "8453", token.totalHolders, optional holderAnalysis (only on EDEL). Miss: HTTP 404 and
  {"code":404,"error":"pair not found"}; no simulation, no taxes, no pool type.
- Rate limit: every response carries x-ratelimit-limit: 50, x-ratelimit-remaining and x-ratelimit-reset (epoch seconds, moves
  in 5 s steps). remaining never went below 48 at 1.2 s pacing; no 429 and no retry-after seen. The window is unknown; the
  limit is per what, unknown. Not stress-tested (30 requests max by rule).
- Latency: median 266 ms, min 227, max 597 (the first request), 24 requests, all through the session proxy.
- Result per token (age = minutes since the desk's first VERIFY row at probe time; GoPlus = fields still pending at the
  token's last WATCH row, i.e. what GoPlus never filled; all 23 had honeypot pending):

| # | set | symbol | address | age min | HTTP | sim / honeypot / buy-sell-transfer | pool, router | ms | GoPlus pending |
|---|---|---|---|---|---|---|---|---|---|
| 1 | timeout new | USDL | 0x78e8cf65 | 194 | 404 | no | n/a | 597 | honeypot |
| 2 | timeout new | USOE | 0x6aa7ae39 | 204 | 404 | no | n/a | 493 | honeypot, taxes, lp |
| 3 | timeout new | WOTF | 0x3c4ef2aa | 209 | 404 | no | n/a | 286 | honeypot, taxes, lp |
| 4 | timeout new | DTCC | 0x9f8f6063 | 214 | 404 | no | n/a | 274 | all 7 |
| 5 | timeout new | FTOF | 0x9daed97d | 214 | 404 | no | n/a | 253 | honeypot, taxes, lp |
| 6 | timeout new | F O T F | 0x417f3f86 | 219 | 404 | no | n/a | 263 | honeypot, taxes, lp |
| 7 | timeout new | RALLY | 0x19640000 | 219 | 404 | no | n/a | 235 | honeypot |
| 8 | timeout new | GTA 6 Coin | 0x08bb8b57 | 219 | 404 | no | n/a | 275 | all 7 |
| 9 | timeout new | I O F | 0x079ba9bd | 219 | 404 | no | n/a | 242 | honeypot, taxes, lp |
| 10 | timeout new | NAFF | 0x32ae5ad4 | 224 | 404 | no | n/a | 257 | mint, freeze, honeypot, risk |
| 11 | timeout new | WWR | 0x09e43249 | 224 | 404 | no | n/a | 280 | all 7 |
| 12 | timeout new | USOE | 0x732e3ef5 | 229 | 404 | no | n/a | 270 | honeypot, taxes, lp |
| 13 | timeout new | AODF | 0xdfb87571 | 239 | 404 | no | n/a | 254 | honeypot, taxes, lp |
| 14 | timeout new | AOP | 0x6d7c2b48 | 259 | 404 | no | n/a | 266 | honeypot, taxes, lp |
| 15 | timeout old | RIPPLEEFFECT (zora) | 0x6034ecf6 | 364 | 404 | no | n/a | 240 | honeypot, taxes, lp |
| 16 | timeout old | STRATO | 0x9b67763e | 1919 | 404 | no | n/a | 267 | honeypot, taxes, lp |
| 17 | timeout old | ADP | 0x83acfed3 | 3184 | 404 | no | n/a | 227 | honeypot, taxes, lp |
| 18 | timeout old | AOFT | 0x2ba6d2a0 | 4739 | 404 | no | n/a | 262 | honeypot, taxes, top10, lp |
| 19 | timeout old | TRONG | 0x64a968da | 6140 | 404 | no | n/a | 234 | honeypot, taxes, lp |
| 20 | timeout old | NOTF | 0x2f747851 | 7790 | 404 | no | n/a | 333 | honeypot, taxes, lp |
| 21 | baseline buy | KOR | 0x2e397d2f | 700 | 404 | no | n/a | 254 | honeypot, taxes, lp |
| 22 | baseline buy | AgenC | 0x82e9ed15 | 600 | 200 | yes / false / 0-0-0 | UniswapV3, 0x26266... | 266 | mint, freeze, honeypot, risk (GoPlus finally answered 2.5 h in: SKIP mint_open) |
| 23 | baseline buy | EDEL | 0xfb31f85a | 560 | 200 | yes / false / 0-0-0 | UniswapV3, 0x26266... | 308 | honeypot (ended goplus_timeout) |
| 24 | baseline buy | RIPPLEEFFECT | 0x6034ecf6 | 365 | 404 | no | n/a | 281 | same token as #15, probed twice by mistake |

- Hit rate = a successful buy + sell simulation with taxes. Overall 2 of 24 requests (8.3%), 2 of 23 distinct tokens (8.7%).
  By set: the 20 timeout-set requests (14 newest, 6 older) 0 of 20; the 4 baseline BUYs 2 of 4 (AgenC, EDEL). EDEL also
  ended goplus_timeout on the desk, so one real goplus_timeout token (1 of 21 distinct timeout tokens) got an answer.
- Under 3 h old: no sample. A goplus_timeout is by definition 3 h after first sight, so every token probed was at least
  194 min old (a fresh-token sample would need tokens still on the watchlist, a different pick).
- By pool type (only hits report one): UniswapV3 2 of 2 hits; v2, v4, Zora and other: 0 hits, but a 404 gives no pool type
  and the logs do not carry one, so 21 of 22 misses are unlabelled. The one tag we have is RIPPLEEFFECT (launchpad=zora):
  miss. Reading: honeypot.is probably indexes only the classic router pools (both hits used the Uniswap V3 SwapRouter02);
  our new tokens are mostly other shapes. NOT shown by this probe, a guess.
- Against GoPlus: honeypot was unfilled at GoPlus for all 23 tokens at their last WATCH row (AgenC was later answered by
  GoPlus: SKIP mint_open after 2.5 h); taxes were also pending for most (see the last column). honeypot.is filled honeypot
  + taxes for 2 of 23. In this sample it would have rescued 1 timeout token (EDEL).
- Caveats: n = 24, one moment in time, one IP (shared datacenter proxy); the sample is not random (newest timeouts plus
  older ones, spread by index); pool types of misses unknown; EDEL's pool is from Nov 2025 (an old token, not a fresh
  launch), AgenC's pool had 1.7 USD reserve value (pair.liquidity, unit unverified), so a "hit" says nothing about tradability;
  taxes 0/0/0 are simulated at one moment and a late-set tax would not show; no GoPlus call was made in this probe (the
  GoPlus column is from the logged WATCH rows only); docs unread.
- Decision for the owner: honeypot.is as a second source only pays if the new-launch pools it misses are Uniswap V4 or
  Zora. A cheap step 2 would be to log the DexScreener pool/dex label on those leads and re-probe 10 fresh WATCH tokens.
- Closed 2026-10-29 (the owner, DECISIONS): honeypot.is dropped after this step 1, no step 2; logging the DexScreener
  pool/dex label moves to BL-10.

## BL-24 rule replay
- Read-only, docs only, 2026-10-30. Question: if an unknown honeypot + buy/sell tax were allowed (as BL-21 allows an unknown
  LP lock), how many desk tokens would have passed, when, and what happened to them? Scripts ran from the session scratchpad
  with data/ read-only; nothing under core/, adapters/, config.py, workflows, tests or data/ changed.
- Window: 2026-10-28T23:25Z (BL-21 live) to 2026-10-30T04:03:10Z (last TICK row at the pull, ea124c7). desk VERIFY rows only.
- Naive count (chat): 147 of 219 watched desk tokens had a WATCH row whose pending list was a subset of {honeypot,
  buy_tax_pct, sell_tax_pct, lp_locked_pct}. Reproduced exactly for the 2026-10-30T03:53:11Z end; also 147/219 at 04:03:10Z.
  136 had all 4 pending, 10 honeypot only, 1 honeypot + buy_tax_pct.
- Variant A: the pass time is the first desk check where every rule is met except fields pending among those 4. A check that
  logged WATCH with such a pending list got past hard_skip()'s missing-data gate with nothing else missing. But hard_skip()
  stops there (goplus_not_ready), so no later rule ran and the row logs no values. Rule order mapping (core/verify.py):

| # | rule | input at the pass time | treated as |
|---|---|---|---|
| 1 | fetch_error / goplus_partial | WATCH row = neither | proven pass |
| 2 | missing data gate | pending subset of the 4 fields, nothing else missing | proven pass |
| 3 | honeypot | pending | allowed unknown (the variant) |
| 4 | mint_open, freeze_or_blacklist, hard risk_flags | known to GoPlus then, value not logged | GoPlus value now (step 3b), as a proxy |
| 5 | nobody_can_sell (sells_1h == 0) | known, not logged | unknown, assumed pass |
| 6 | liquidity >= 50k | a WATCH row proves it (a low pool logs "liquidity" instead) | proven pass |
| 7 | lp_locked (known lock < 90) | pending at the pass (BL-21: unknown is allowed) | pass; GoPlus now proxy where it reports a lock |
| 8 | dev_pct, top10_pct | known then, not logged | GoPlus value now, as a proxy |
| 9 | too_young / too_old | age_min not logged | back-computed: pass time minus DexScreener pairCreatedAt (step 3); unknown when no pairCreatedAt |
| 10 | buy_tax / sell_tax | pending | allowed unknown (the variant) |

  Not modelled: EXECUTE limits (MAX_OPEN 4, cash), so a pass is not a buy; the desk could not have held most of these at once.
- Naive 147 vs Variant A ceiling 137, every difference: 10 tokens had their first qualifying WATCH row before the window
  (2026-10-28 22:38 to 23:23 UTC, before BL-21 went live), so their pass time is outside it. No other difference: no
  candidate had a SKIP or PASS row before its pass row (no 24h re-sightings), and a lead pending only lp_locked_pct never
  logs WATCH since BL-21. The naive count is the ceiling at the missing-data gate; rules 4, 5, 8, 9 are what it skips.
- Why the wait: the first pending list held top10_pct for 139 of 147 (mint_open or dev_pct for 79); top10 is
  usually the last of the other fields GoPlus fills, so the 4-field state starts about 100 min after first sight.

| Variant A pass | ceiling (137) | estimate (104) |
|---|---|---|
| 2026-10-28 (from 23:25) | 2 | 1 |
| 2026-10-29 | 122 | 96 |
| 2026-10-30 (to 04:03) | 13 | 7 |
| minutes first_seen -> pass, p25 / median / p75 | 95 / 105 / 115 | 95 / 105 / 115 |

  Ceiling range: min 0 (8 tokens qualified at their first check), max 170 min. First pass 2026-10-28T23:38:14Z, last
  2026-10-30T03:03:11Z.
- Later answers in the logs: none of the 147 ever got honeypot + taxes from GoPlus (0 goplus_ready_at). 131 of the 137 ended
  goplus_timeout, 5 SKIP liquidity while still watched (pool pulled 15 to 75 min after the pass time: 3 USDF, NAFF, SUZ),
  1 still WATCH at the end (CDXR). So the logs prove no candidate wrong on honeypot or tax: there is no answer to judge by.
- Step 3, DexScreener now (2026-10-30 04:05 UTC): batched GET /tokens/v1/base/<up to 30>, 150 addresses (147 candidates +
  3 baseline Oct 29 picks not among them), 5 requests, 1 s apart, all HTTP 200, no retry. Best-liquidity Base pair per
  token (adapters.base pick_pair). Dead = no pair or liq_usd < 50,000 (SKIP_IF min_liquidity_usd); alive = the rest.
  128 of 137 trade on Uniswap v4 pools, 3 Aerodrome, 1 v2, 1 v3, 4 no pair.
- Age (rule 9) from pairCreatedAt: 125 ok (median 1.9 h at the pass), 5 too_old (pools 57 to 351 days old: EDEL 351 d,
  SR 206 d, RALLY 144 d, CDXR 126 d, ESHARE 57 d), 1 too_young (SUZ 7.8 min), 6 unknown (4 no pair, USDL and Anthropic with
  no pairCreatedAt; assumed pass). The pair the desk used then may not be today's best pair, so the age is a proxy.
- Step 3b, GoPlus now (2026-10-30 04:05:58 to 04:10:30 UTC): one token_security call per in-window candidate through the
  adapter's own Client, GOPLUS url and goplus_facts() (imported, unchanged), 2 s apart, 137 calls in 272 s. 133 code 1,
  4 code 4029 (calls 82, 86, 90, 91; never 3 in a row, so the run went on; not retried). 4029 is very likely GoPlus's
  rate-limit code (see Open items).
  - Honeypot + taxes answered now: 0 of 133. 124 still answer is_honeypot "0" with cannot_sell_all, buy_tax and sell_tax
    empty (so the adapter keeps honeypot pending); 9 have taxes (0/0, one 0/2%) but still no cannot_sell_all. So the asked
    breakdown (honeypot yes/no, taxes vs SKIP_IF) is empty: GoPlus never answers these fields for these tokens, hours to a
    day later.
  - Fields known at the pass time, value now vs SKIP_IF (first kill in hard_skip() order): mint_open 13, freeze_or_blacklist
    11, top10_pct > 35 6, lp_locked known < 90 2 (HEXN, TRUMPKIN), risk flag 0, dev_pct 0; clean 101. Known now: mint, freeze,
    dev, flags 133 of 133, top10 132, LP lock 2. top10 median 1.0% (max 100), dev median 1.0% (max 67).
    These fields were already filled at the pass time, so Variant A would most likely have skipped these 32 at the pass
    (same value), not bought them. They lower the estimate; they are not "passed early and proven wrong".
- Three numbers:

| | tokens | how |
|---|---|---|
| Variant A ceiling | 137 | first 4-field WATCH row inside the window |
| estimate after age + GoPlus now | 104 | minus 6 age (5 too_old, 1 too_young), minus 27 more killed now on mint/freeze/top10/LP (32 kills, 5 also too_old) |
| proven wrong (honeypot or tax over SKIP_IF, from the logs or now) | 0 | no candidate has any honeypot/tax answer to judge by |

  The 104 still carry unknown honeypot, taxes and sells_1h at the pass time, and 3 have no GoPlus answer now (4029).
- Outcome buckets now (DexScreener):

| set | n | alive | of which 0 sells in 24 h | dead: no pair | dead: liq < 50k |
|---|---|---|---|---|---|
| Variant A ceiling | 137 | 117 | 72 | 4 | 16 (median 7.8 USD, max 3,520) |
| estimate | 104 | 98 | 72 | 0 | 6 |
| baseline Oct 29 picks | 19 | 12 | 0 | 0 | 7 |

  Alive median priceChange h24: ceiling -0.35%, estimate -0.07%. 72 of the 98 alive estimate tokens show buys but 0 sells
  in the last 24 h with 75k to 240k USD liquidity, mostly copycat fund/ticker names (DTCC, ADOF, USOE, AODF...): that is
  the pattern of a token nobody can sell, which is exactly what a honeypot check would answer. Under Variant A only the
  sells_1h rule stands in the way, and its value at the pass time is not logged.
- Baseline: it bought 19 of the 137 candidates in the window (23 baseline buys), 12 of them before the Variant A pass time
  (top10 etc. still pending). 18 closed, all time_stop, -7.15 USD on 96.28 USD of tickets (about -7.4%, about the round-trip
  friction); USDL still open. Of the 104 estimate tokens it bought 5 (DOSA, ADFO, KOR, NTDA, AOP), all time_stop,
  -1.75 USD on 26.52 USD. Its 4 non-candidate buys made +8.18 USD (Kek +9.25). The candidates did not move in 45 min.
- Caveats: VERIFY rows log no price, so there is no P&L for candidates (only the baseline's real paper fills). "Alive now" is
  not "sellable": a honeypot can show sells from whitelisted wallets, and 0 sells in 24 h is a warning sign, not proof.
  GoPlus values now may differ from values at the pass time (taxes can change, honeypot status can flip, holders move).
  Age uses today's best pair. The sample is about 1.2 days (28.6 h), one market, many copycat launches from a few
  deployers. No GoPlus or DexScreener call was retried (4029 x4 left unanswered).

## BL-23
- Machine cards (always real, from data/daily/*/tasks.csv only, no request) for the locked BL-13 mock, written on the
  UTC day roll right after the BL-8 money report: report.json["machine"] plus data/tokens_24h.json. Code: core/report.py
  machine(), run_machine(); hook in core/tick.py. By hand, python -m core.report also writes them for today so far
  (runs.expected = the :03 :08 ... slots already passed, ticks_per_hour ends at the current hour, totals not stored).
- Definitions (desk book only; the baseline is not in the funnel):
  - scan = distinct tokens with a desk VERIFY row in the window; gate = scan tokens with no gate kill;
    safety = distinct tokens with a PASS row; buy = desk EXECUTE BUY rows; exits_open = desk open positions at the roll
    (the closing equity entry the roll already keeps in state; desk book when there is none, e.g. by hand).
  - kill = a final desk VERIFY SKIP row (WATCH and RETRY are not kills), keyed by its full reason (missing:liq_usd etc.).
    gate = the row has goplus=skipped, or its reason is liquidity, fetch_error_x3 or missing:<DexScreener field>
    (symbol price liq_usd age_min sells_1h). safety = everything else (goplus_timeout, GoPlus fields, honeypot, taxes,
    lp_locked, too_old / too_young, missing:<GoPlus field>, goplus_partial). too_old/too_young use DexScreener facts but
    are rules checked after GoPlus, so they count as safety.
  - kill_log = the last 10 kills of the day, newest first {ts, symbol, stage gate/safety, reason}.
  - last_tick = ts + seen new checked dex_only goplus passed bought requests secs of the last TICK row of the day.
  - runs = {ticks, expected 288, rate in %}; tick_secs = median and max of secs=; ticks_per_hour = 48 entries
    {hour "YYYY-MM-DDTHH", n} for the 48 hours ending at the roll (closed day + the day before), missing hours 0.
  - tokens_24h.json = {day, as_of, tokens}: per token the latest desk VERIFY or EXECUTE row of the day (never a baseline
    row): symbol, address, verdict = its action (SKIP WATCH PASS RETRY BUY), reason, checks = its desk VERIFY rows that
    day, last = its ts, lp_unknown from that row. Newest first, one token per line, overwritten at each roll.
- Windows: funnel, kills and kill_log cover the closed UTC day. funnel_since and kills_since are running totals since the
  report's "since" day (BL-8 live, Gate B's start), kept in the state's report key "totals" {through, funnel, kills}.
  Each closed day is added once (through = last day added; days <= through are never read again), so a retry or a
  second call never double-counts and the roll no longer re-scans old files (BL-16 will move them out). First run:
  backfill from "since" with the daily files that exist. A day missed by a failed card run is still added later
  (every day after through, up to the closed day). funnel_since sums the daily counts: token-days, not distinct tokens.
- Isolation: run_due() writes the money cards first and marks machine_due; the tick then runs run_machine() in its own
  try. It builds the cards, adds them to report.json (refused if report.json is for another day), writes tokens_24h.json,
  and only then stores the totals and clears machine_due. A failure: report.json keeps the money cards (no "machine"
  key), tokens_24h.json keeps its old day (stale is visible), TICK detail machine=error:<type> right after report=,
  totals unchanged, retried next tick, the run ends red after the TICK row. If the money report itself fails, the
  cards wait for it (machine=none). TICK keys: desk keys unchanged and first, then bl_* report machine bl_error.
- Tests: tests/test_machine.py (19): shapes, gate/safety split, kill_log order, 48 h buckets with 0s, tokens latest desk
  row (baseline ignored), overwrite, totals backfill / idempotent / run twice / failed write, report day check, tick
  roll, failure isolation + retry, report failure waits, by hand, desk unchanged (cards on vs skipped vs broken).
  169 pass on 3.13 (150 before).
- Check vs chat on a scratch copy of data/daily/2026-10-29 (machine() for the closed day, since = 2026-10-29):

  | 2026-10-29 | chat | BL-23 |
  |---|---|---|
  | TICK rows (runs.ticks) | 288 | 288 (rate 100.0%) |
  | tick_secs median / max | 13.7 / 22.6 | 13.7 / 22.6 |
  | desk VERIFY tokens (funnel scan) | 1,229 | 1,229 |
  | SKIP rows (kills gate + safety) | 1,212 | 1,069 + 143 = 1,212 |
  | SKIP with goplus=skipped | 1,069 | 1,069 (all gate) |
  | liquidity / goplus_timeout / missing | 986 / 133 / 83 | 986 / 133 / 83 (73 liq_usd, 5 price, 4 symbol, 1 age_min) |
  | mint_open / top10_pct / too_old | 8 / 1 / 1 | 8 / 1 / 1 (safety) |
  | PASS (funnel safety) | 0 | 0 |
  | desk BUY (funnel buy) | 0 | 0 |

  Funnel gate = 160 (1,229 minus 1,069 gate-killed tokens). tokens_24h: 1,229 tokens, 1,212 SKIP + 17 still WATCH at
  day end, 2 with lp_unknown. No gap with chat.
- Live smoke (scratch copy, books' day set back to 2026-10-29 to force the roll, one real tick 2026-10-30 03:52 UTC):
  report=ok machine=ok, 9 requests, 2.8 s; report.json 7,680 B (machine key 5,485 B), tokens_24h.json 233,134 B
  (1,229 tokens); card build 0.05 s. tokens_24h.json is overwritten once a day, so git grows ~230 KB per day from it;
  BL-16's archive should cover it.

## BL-25
- Logging only (the owner, 2026-10-30, DECISIONS): DexScreener trade facts at the END of the row detail, existing keys and
  their order unchanged. No rule reads them; verdicts, reasons, row counts, state, requests and TICK keys are unchanged.
- Keys, in this order: b1 s1 b24 s24 liq dex.
  - b1 / s1, b24 / s24 = buys / sells in the h1 / h24 window, summed over every Base pair (chainId "base") that holds our
    token. Side logic (same as total_sells_1h): our token is baseToken -> b = txns.<w>.buys, s = txns.<w>.sells; our token
    is quoteToken -> the mirror, b = txns.<w>.sells, s = txns.<w>.buys. So s1 equals the sells_1h the rules already use
    (unit test with a quote-side pair; live smoke 34 of 34 leads equal).
  - liq = liquidity.usd of the chosen pair (pick_pair: the deepest Base pair), rounded to whole USD.
  - dex = the chosen pair's dexId plus its first vN label (uniswap:v4, uniswap:v3, uniswap:v2, aerodrome, aerodrome:v2);
    other labels (CL, DLMM) are dropped, characters outside [A-Za-z0-9_.-] become "_" so it stays one detail token.
    Asked as "pool"; named "dex" because the baseline BUY row already has pool= (BL-8 eligible-pool prefixes, used by the
    replay), and a second pool= would break key=value parsing. A Zora coin shows its pool's dex (e.g. uniswap:v4), not the
    launchpad; launchpad= stays on BUY rows as before.
  - "na" when DexScreener didn't give the value (no Base pair with the field, odd shape, no liquidity, no dexId), never 0;
    a real 0 stays 0. Any error while building the keys gives all "na" and never stops the check.
- Code: adapters/base.py trade_facts(pairs, pair, address), called in facts() on the pairs the token-pairs request already
  returned (no request added), kept as facts["trade"] and dex["trade"]. core/verify.py TRADE_KEYS + trade_detail(facts):
  the "k=v" list, [] for a check with goplus_skipped. core/leads.py appends it at the very end (after until= on SKIP rows).
  core/desk.py buy(..., buy_note="") appends buy_note on the BUY row only; core/baseline.py passes the pick's keys there.
  The BL-8 note was not reused: desk.buy() also writes it on its SKIP rows (max_open, loss_halt, size).
- Which rows get the keys:

| row | keys |
|---|---|
| desk VERIFY WATCH, PASS | yes |
| desk VERIFY SKIP after GoPlus (honeypot, mint_open, top10_pct, too_old, goplus_timeout, goplus_partial ...) | yes |
| desk VERIFY RETRY fetch_error and SKIP fetch_error_x3, GoPlus side (got past dex_skip) | yes (the owner) |
| desk VERIFY gate kills (goplus=skipped: liquidity, DexScreener fetch_error, missing:<DexScreener field>) | no, unchanged |
| baseline EXECUTE BUY | yes, after launchpad= |
| baseline EXECUTE SKIP (no_eligible, max_open, loss_halt, size), desk EXECUTE, EXIT, TICK | no, unchanged |

- Tests: tests/test_trade.py (10): sums across 2 pairs with the quote-side mirror, non-Base and other-token pairs ignored,
  s1 == total_sells_1h and == facts()["sells_1h"] on the real facts() (quote-side pair included, still 2 requests); "na"
  for missing fields and odd shapes (txns a list, window None, strings, inf) without raising, a real 0 stays 0; dex labels
  for v2 / v3 / v4 / aerodrome / aerodrome:v2 / Zora / zora:v4 / odd names; row selection (WATCH, PASS, SKIP, GoPlus RETRY,
  fetch_error_x3 after until=; gate rows unchanged; baseline BUY only, its BL-8 pool= kept); desk unchanged: the BL-8
  DeskUnchangedTest ticks with the keys on and off give the same rows once the 6 trailing keys are stripped, the same
  state (desk, baseline, leads, report) and the same TICK keys, requests included (secs excluded). Old tests: the BL-21
  exact-detail assertion now ends with the keys ("na", no trade facts in that fixture); the fake facts() in
  tests/test_tick.py passes "trade" into dex like the real one. 179 pass on 3.13 (169 before).
- Live smoke 2026-10-30 (scratch copies, nothing under data/):
  - Tick 1, 04:27:48 UTC, lead memory cleared: 20 leads, 20 checked, 19 gate kills (liquidity, no keys), 1 WATCH with keys,
    baseline due, eligible 0. 25 requests in 5.9 s, all HTTP 200: scan 3 (DexScreener 2, GeckoTerminal 1), DexScreener
    token-pairs 20, GeckoTerminal age 1, GoPlus 1. Same per-lead count as before BL-25 (1 DexScreener + GoPlus when needed
    + GeckoTerminal only without pairCreatedAt). s1 == sells_1h on 20 of 20.
  - Tick 2, 04:28:25 UTC, 7 BL-24 candidates put on the watchlist as due: 11 checked, 9 WATCH rows with keys, 3 deferred
    (GoPlus cap 9), baseline BUY CDXR with keys. 27 requests in 19.5 s, all HTTP 200 (DexScreener 16, GeckoTerminal 2,
    GoPlus 9). s1 == sells_1h on 14 of 14.
  - An earlier run on a copy of the live state checked 3 (1 WATCH with keys, 2 gate kills without): 7 requests, 2.5 s.
  - Examples:

    WATCH ONDO 0xe354f5ea... pending=top10_pct b1=35 s1=0 b24=35 s24=0 liq=81287 dex=uniswap:v2
    WATCH AODF 0x12b92258... pending=honeypot,buy_tax_pct,sell_tax_pct,lp_locked_pct b1=0 s1=0 b24=4 s24=0 liq=129641 dex=uniswap:v4
    WATCH I O F 0x079ba9bd... pending=honeypot,buy_tax_pct,sell_tax_pct,lp_locked_pct b1=120 s1=78 b24=541 s24=370 liq=271782 dex=uniswap:v4
    WATCH SI 0xfe800187... pending=mint_open,freeze_or_blacklist,honeypot,risk_flags b1=88 s1=84 b24=288 s24=235 liq=159942 dex=uniswap:v3
    BUY (baseline) CDXR 0x40aaf754... ... desk=WATCH launchpad=other b1=8 s1=4 b24=2215 s24=2079 liq=1175326 dex=uniswap:v4

- Size: the keys add 47 to 55 bytes per row (mean 50.7 over the 12 smoke rows with keys). Rows that would carry them on
  2026-10-29: 1,630 (1,611 desk VERIFY rows without goplus=skipped: 1,468 WATCH + 143 SKIP, plus 19 baseline BUY). So about
  1,630 x 50.7 = ~83 KB (81 KiB) more per day, +14% on that day's tasks.csv (584,217 B). Include it in BL-16's archive plan.
- What the replay will do (after about a week of rows): for desk tokens past the gate, set s1 / s24 / b24 at their checks
  against what happened later (GoPlus honeypot / tax answers when they come, DexScreener liquidity pulls, the baseline's
  fills and exits on its BUY rows with keys), to choose a real-sells rule (for example a minimum s24, or s24 / b24) and see
  how many of the honeypot-pattern tokens (BL-24: 72 of 98 alive with 0 sells in 24 h) it would catch. Only then does the
  owner decide on a rule; BL-25 changes no threshold.
- Caveats: DexScreener counts are trades, not distinct wallets, and a honeypot can show sells from whitelisted wallets, so a
  real-sells rule is a filter, not proof. h24 on a pool younger than 24 h is its whole life. Only Base pairs DexScreener
  lists for the token are summed.

## First desk trade
- SI 0xfe8001877721d8f8cf070cee3c042b6a01b0e68f (Uniswap V3 pool, launchpad other), 2026-10-30, from
  data/daily/2026-10-30/tasks.csv and trades.csv. The first desk PASS and the first desk trade; section 11 said 0 PASS before.
  No rule or config change (the owner).
- VERIFY: first seen 02:03:11 UTC, WATCH goplus_not_ready for checks 1 to 10 (pending fields shrank from 9 to mint_open,
  freeze_or_blacklist, honeypot, risk_flags), PASS at check 11, 04:48:13 UTC, goplus_ready_at=04:48:13 (2 h 45 min after
  first seen, inside the 3 h watch). BL-25 keys on the PASS row: b1=92 s1=94 b24=317 s24=267 liq=159111 dex=uniswap:v3.
- EXECUTE: BUY 04:48:13, 5.00 USD, quote 0.002903, fill 0.00299009 (3% friction, liquidity 159,111 USD), qty 1,672.19.
- EXIT (58 desk EXIT rows, HOLD peak +67.05%):

| ts (UTC) | rule | qty | intended | quote | fill | friction | liq_usd | usd | pnl_usd |
|---|---|---|---|---|---|---|---|---|---|
| 06:08:13 | ladder_1 (pct=+47.35) | 551.82 | 0.004186126 | 0.004406 | 0.00427382 | 3% | 196,298 | 2.358392 | +0.708392 |
| 09:38:12 | liq_pull (pct=-92.16) | 1,120.37 | 0.0002343 | 0.0002343 | 0.000222585 | 5% | 43,476 | 0.249377 | -3.100623 |

- Result: -2.392231 USD on 5.00 (-47.8%) after friction; desk 47.61 USD at liquidation value. One close (ladder_1 is not a
  close), Gate B 1/100. The last HOLD was +66.22% at 09:33:12; five minutes later the pool had lost about 73% of its
  liquidity (159k at entry, 196k at the ladder, 43k at the pull) and the price 92%, between two ticks, so the trail (25%)
  never had a tick to fire on. A 5-minute tick cannot exit a pull faster than this; the logged stop slippage is 0
  (intended = quote) because liq_pull logs the quote it saw.
- The baseline held the same token: random BUY 04:33:13 (desk=WATCH, seed 202610300433, pick 1 of 2), 5.07 USD at fill
  0.00303541, sold in the same two ticks (ladder_1 +0.683073, liq_pull -3.149969). So the first desk trade gave no edge
  over the baseline either way.

## BL-13
- site/index.html: one self-contained page (inline CSS and JS, no external script, font or stylesheet), built from the locked
  mock docs/mock/bl13-mock.html (kept byte-for-byte as the owner sent it, sha256 c9b7db30...8498b1, never under site/). Same
  CSS tokens, card order, colours, dark mode (prefers-color-scheme and data-theme) and layout: one column to 919 px, from
  920 px money left, machine right, token table full width. The Live/Sample switch, every SAMPLE number and the mock's
  baked-in token/hour arrays are gone.
- Data: at page load it fetches exactly two URLs, https://raw.githubusercontent.com/Zirqo-Dev/baseline-desk/main/data/report.json
  and .../data/tokens_24h.json (fetch cache no-cache, each in try/catch, every card rendered in its own try). No query
  parameter, hash or setting changes where data comes from (the owner). Checked 2026-10-30 with curl -I: report.json HTTP 200,
  access-control-allow-origin: *, cache-control: max-age=300, content-type text/plain (fetch().json() does not care);
  tokens_24h.json HTTP 404 with access-control-allow-origin: * (not written until the first BL-23 roll). raw.githubusercontent
  caches up to 5 min, so the page can lag the repo by that much.
- Field map (every read goes through a reader R / M / B / EQ / HR / KL / TF / TK; tests/test_site.py checks each key):

| card | reads |
|---|---|
| terminal (last tick) | machine.last_tick ts seen new checked dex_only goplus passed bought requests secs; machine.runs ticks expected rate, machine.day |
| header | day, as_of, machine.day, machine.last_tick.ts (Live when within 26 h, else Stale; Offline when report.json failed), tokens day; under the dot "daily snapshot · as of HH:MM UTC, Mon D" from report.json as_of (the site changes only at the daily roll, so no "every 5 min") |
| paper equity | start_balance, desk value_usd net_pnl_usd net_pnl_pct multiple open equity_daily[day, value_usd], baseline value_usd equity_daily |
| desk vs random | per book value_usd net_pnl_usd closes win_rate max_dd_pct stop_slip_pct exits_by_rule; desk_lead_usd |
| Gate B | gate_b desk_closes target kill |
| how trades ended | per book exits_by_rule (share of that book's SELL rows; ladder sells are partial) |
| kill funnel | machine.funnel_since from to scan gate safety buy, machine.funnel.exits_open |
| why they died | machine.kills_since from to gate safety (top 3 reasons each) |
| kill log | machine.kill_log ts symbol stage reason |
| is it running | machine.runs, machine.tick_secs median max, machine.ticks_per_hour hour n |
| tokens | tokens_24h.json day as_of tokens[symbol address verdict reason checks last lp_unknown] |

- Honest empty states, never a made-up number: report.json failed (red "Couldn't load", every card, header Offline); no
  "machine" key (the 4 machine cards and the terminal say the cards come at the 00:03 UTC roll); tokens_24h.json 404 (says so);
  0 closes (win rate "—"); no hard_stop/trail sell (stop slippage "—"); no sells in either book ("No closed trades yet").
  A dash means no number. Report day and as_of, machine day and tokens day are on the page (header and footer).
- Choices inside the mock's look (no number in a label that config.py owns, so labels never go stale): "Liquidity under the
  floor" (not $50k), "ladder 1" / "ladder 2" (not +40% / +100%), "GoPlus silent until the watch ran out". The run pill says
  "Running ✓" when the report day's run rate is at least 90% (Gate A), else "Short day <rate>". A ▲ marks the better book on a
  row only where both have a number. Safety kills in the token table = SKIP whose reason is not a gate kill (report.GATE_KILLS).
  Token symbols link to dexscreener.com/base/<address> (only for a 0x + 40 hex address).
- Deploy: .github/workflows/pages.yml (upload-pages-artifact@v3 path site, deploy-pages@v4; permissions contents read,
  pages write, id-token write; concurrency group pages, no cancel). Triggers: push to main touching site/** or the workflow
  file, and workflow_dispatch. Bot "data: tick" commits touch data/ only, so they never deploy. One-time owner setting:
  Settings -> Pages -> Source = GitHub Actions. tick.yml, core/, adapters/ and data/ unchanged.
- Tests: tests/test_site.py (11): builds report.json + tokens_24h.json with report.on_roll, run_due and run_machine from
  fixture rows (both books, ladder and hard_stop sells, a PASS with lp_unknown, a desk BUY) and checks every key path the page
  reads exists (report, machine, both books, equity_daily, ticks_per_hour, kill_log, tokens, tokens file); the page's exit
  rules equal report.RULES and its gate set equals report.GATE_KILLS; exactly one fetch() call, called only with the two data
  URL constants; no other URL in the page but dexscreener.com/base/ and the SVG namespace; no XMLHttpRequest, import(),
  location, URLSearchParams, storage, sockets, beacons, external script/link/@import/url(); no SAMPLE text or mock sample
  numbers; the mock lives under docs/mock and site/ holds index.html only. Checked that adding a fetch to another URL, or
  location.search on a data URL, fails 2 tests. 190 pass on 3.13 (179 before).
- Local smoke 2026-10-30 (python -m http.server on site/, Playwright page.route serving scratch files for the two raw URLs,
  Chromium, 390x844 and 1280x900, nothing under data/ changed):
  - (a) today's real files: report.json for 2026-10-29 without "machine", tokens_24h.json 404. Money cards real (desk 50.00,
    random 51.96, 19 closes, desk 1.96 behind, Gate B 0/100, exits time stop 86% for random, desk "—"); terminal and the 4
    machine cards show the "first written at the 00:03 UTC day roll" state; token card shows the 404 state. Requests: the page
    plus the two data URLs only. Console: the browser's own 404 line only, no script error. No horizontal scroll at 390 px.
  - (b) scratch report.json + tokens_24h.json from core/report.py on_roll + run_due + run_machine on a copy of data/ in the
    scratchpad (day 2026-10-30 so far, as if rolled at 2026-10-31 00:03): desk 47.61 (-2.39, -4.8%, 1 close, max_dd 9.8%),
    random 49.93 (28 closes), desk 2.32 behind; funnel since 2026-10-29 1,760 / 229 / 1 / 1; kills gate liquidity 1,395,
    safety goplus_timeout 169; 155/288 ticks (a part day, so "Short day 53.8%" and empty later hours); 531 tokens, chips
    All 531 / Watching 30 / LP unknown 2 / Safety kills 38 / Skipped 500. Requests: the page plus the two data URLs only; no
    console error. Also checked: dark mode at 1280 px, and both fetches aborted (every card shows the red "Couldn't load" state,
    header Offline).
- Not checked from here: the live site (the session's proxy refuses zirqo-dev.github.io) and the first Pages run.

## BL-26
- Outcome labels, logging only (the owner, 2026-10-30, DECISIONS): what happened to a token +1 h, +6 h and +24 h after its
  first sight, for every desk check past dex_skip() (WATCH, PASS, non-gate SKIP, GoPlus-side RETRY and fetch_error_x3: the
  rows BL-25 gives trade keys). Gate kills, baseline rows and trades get none. No rule, config value, verdict or exit reads
  them. Code: core/labels.py, adapters/base.py token_batch(), hook in core/tick.py. No backfill of past days.
- Endpoint (probe 2026-10-30, 11 read-only requests, all HTTP 200): DexScreener /tokens/v1/base/<a,b,...> takes up to 30
  addresses (bucket ds_pairs, 300/min) and answers ONE pair per token, the deepest (30 addresses -> 30 pairs; CDXR has 18
  Base pairs on /token-pairs/v1 and got 1). The owner chose option A (batched, deepest pair only) over per-token calls (B) or
  a hybrid (C).
- outcomes.csv (data/daily/<day>/, the day of the label): ts symbol address first_seen horizon age_min first_price price
  chg_pct liq pair_b1 pair_s1 pair_b24 pair_s24 dex gone.
  - pair_b1 pair_s1 pair_b24 pair_s24 are the deepest pair's counts only (BL-25's trade_facts() run on that one pair, quote
    side mirrored). BL-25's b1 s1 b24 s24 on VERIFY rows are sums over every Base pair. They are equal for single-pair tokens
    (7 of 8 in the probe) and lower here for multi-pair tokens: do not compare them one-to-one in a replay.
  - price: pick_pair() on the answer (the deepest Base pair, quote side inverted like facts()), the same rule as first_price,
    so chg_pct = (price / first_price - 1) x 100 compares the deepest pool each time. If the deepest pool changed since first
    sight, the row is still logged and dex shows which pool it is. liq and dex = that pair (BL-25 format, e.g. uniswap:v4).
  - first_price: the facts() price of the check that IS the first sight (first_seen = that check's time); "na" when the
    check came later (tokens already on the watchlist at deploy, or a token re-added after a failed labels step).
  - gone=1 (price, chg_pct, liq and pair counts "na") when the answer has no Base pair for the token. "na" for any value
    DexScreener didn't give, never 0; a real 0 stays 0.
  - first_seen = the watchlist's first_seen, else the check time. A token seen again after its 24 h skip (a new watch cycle)
    starts a new set of labels with its new first_seen (the owner); one re-checked while its entry is still pending keeps
    the old first sight.
- Timing: due at first_seen + 1 / 6 / 24 h, taken on the first tick at or after it; age_min = the real age (a late label is
  logged late, never skipped). Oldest due first; at most 2 requests of 30 addresses per tick; every due horizon of a fetched
  token is written in that pass; the rest wait. A failed request (429, 5xx, timeout, 4xx, odd body) leaves its labels due.
- State key "labels" = {"pending": {address: {symbol, first_seen, first_price, done}}}; an entry leaves after its +24 h label
  or at first_seen + 26 h (a +24 h label still missing then is lost). Smoke: ~190 B per entry; about 150 entries at a time,
  ~28 KB in state.json.
- Tick: verify_and_buy() notes each check past the gate as a plain dict (no request, no I/O). After the desk and baseline
  steps and their saves, labels.run() runs in its own try: add sights, prune, take due labels, write rows, save. An error logs
  labels=error:<type>, leaves due labels for the next tick and ends the run red after the TICK row (as BL-23's machine step);
  that tick's new sights are lost (a watched token comes back on its next re-check, with first_price "na"). TICK detail ends
  with labels=ok|none labels_due labels_done labels_calls (existing keys and order unchanged); requests= includes the label
  calls. tick.yml already runs git add data/, so outcomes.csv is committed with no workflow change.
- Tests: tests/test_labels.py (20): not before +1 h, late labels with real age and both horizons in one request, each label
  once then the entry leaves, first_price and chg_pct; 70 due -> 2 calls of 30 oldest first, 10 wait, next tick 1 call;
  FETCH_ERROR / None / odd body stay due; the real URL; gone (no pair, non-Base pair); "na" vs 0; quote-side pair mirrored
  equals trade_facts() on that same pair; price from pick_pair (deep pool over a thin one); pruning at 26 h; bounded state;
  in the tick: only past-gate checks get entries (PASS, safety SKIP, WATCH; not the gate kill), watched first_seen kept,
  first_price only at first sight, TICK keys at the end, labels error never stops a PASS + BUY and the TICK row, a failed
  step is retried next tick; desk and baseline unchanged (the BL-8 three-tick run with labels on, off and broken: same desk
  and baseline rows, same desk, leads and baseline state, same TICK desk and bl_ keys). Old test: the BL-7 heartbeat test
  now ends with the 4 labels keys. 210 pass on 3.13 (190 before).
- Live smoke 2026-10-30 (scratch copies of data/, real requests, nothing under data/ changed):
  - Tick on a copy of the live state: 36 requests in 21.2 s, 10 checked, labels=ok due 8 done 8 in 1 call (tokens on the
    watchlist had an older first_seen, so their +1 h was due at once: the first live tick will show the same).
  - Smoke only: pending seeded from the day's 82 past-gate tokens with their real first_seen (no code path does this):
    next tick 26 requests in 18.9 s, labels due 89, done 86 in 2 calls, 3 waited; the tick after: due 3, done 3, 1 call,
    8 requests in 2.6 s. 97 rows: 71 x 1h, 26 x 6h, 0 gone. A label request takes about 0.4 s.
  - After the first_price fix, a fresh copy: 2 new first sights kept their price, 2 watched tokens got "na".
  - Sample rows:

    ARCHAX,0xb4006d47...,2026-10-30T12:18:13Z,1h,171,9.731e-05,9.731e-05,0.0,96341,0,0,3,0,uniswap:v4,0
    CDXR,0x40aaf754...,2026-10-30T01:43:11Z,6h,806,na,0.02006,na,1175326,0,0,2,3,uniswap:v4,0

    (ARCHAX's first_price there is from before the fix; it is "na" now for a token already watched.)
- Size: 142 B per row. About 130 past-gate tokens a day (82 by 15:00 UTC on 2026-10-30) x 3 horizons = ~390 rows, ~55 KB
  per day of outcomes.csv. Requests: about 1.4 labels due per tick on average, so 0 or 1 label request per tick in normal
  running, 2 at most.

## BL-28
- Real depth, logging only (the owner, 2026-10-30, DECISIONS): qliq = the USD value of the chosen pair's OTHER side (ETH,
  WETH, USDC, ZORA...), the side that is not our token. Why: in the first 76 outcome labels, 44 had liq / price close to
  0.99e9 (mostly uniswap:v4), what a pool holding ~99% of a 1B supply looks like, so liq (DexScreener liquidity.usd, both
  sides) can meet the 50k floor on the token's own side. Measured here; no rule, config, verdict or exit reads qliq.
- Formula (base.quote_liq(), from the pair already fetched, no request): our token as baseToken -> liquidity.quote x
  (priceUsd / priceNative) (the quote token's USD price); our token as quoteToken -> liquidity.base x priceUsd (priceUsd is
  the base token's price; "liquidity.base the same way" in the brief would have been the wrong price for that side). Rounded
  to whole USD. "na" when an input is missing or 0 or the shape is odd (never raises); a real side worth under 0.50 USD
  rounds to 0 and stays 0 (live: 21.66 ZORA = 0.13 USD), like BL-25's real 0 (Claude Code, flagged to the owner).
- Same pair as liq: pick_pair() (the deepest Base pair) on VERIFY and baseline BUY rows; the batch answer's one pair on
  outcome labels (BL-26).
- Rows: TRADE_KEYS gains qliq after dex, so every row that has BL-25's keys ends with qliq=<n|na> (desk VERIFY rows past
  dex_skip(): WATCH, PASS, non-gate SKIP, GoPlus-side RETRY and fetch_error_x3; baseline EXECUTE BUY). Gate-killed rows
  unchanged. outcomes.csv gains a last column qliq (after gone; "na" on gone rows).
- Header rule (the owner): a file never mixes column counts. labels.header() appends with the columns the day's file already
  has; outcomes.csv for 2026-10-30 was started with the BL-26 header, so it keeps 16 columns to the end of its UTC day and
  qliq starts with the next day's file (17 columns). A reader must take the columns from each file's header.
- Fixtures: asci / ETH (uniswap v4): liq 9,494, qliq 4,648 (1.9405 ETH x 2,395); BRETT / WETH (aerodrome): qliq 344,266
  (143.7995 WETH x 2,394). In both the quote side is the non-token side.
- Live check of the formula (raw batch pairs, 2026-10-30): v2 CORTEX / WETH both sides ~1.66 of 3.31 USD (qliq 2);
  CDXR / USDC v4: liquidity.usd 1,175,326 = 58,583,845 CDXR x 0.02006 (~1,175,192) + 27.22 USDC (qliq 27); ONDO / ZORA v4:
  4,774 ZORA = 29 USD against 112,838 liquidity.usd.
- Survey, 2026-10-30 ~20:00 UTC (the day's 140 desk tokens past the gate, read with 5 batch requests, smoke only):
  139 measured, 1 "na"; qliq < 10% of liq: 105 (median qliq / liq 0.0002). Of the 111 with liq >= 50,000 (the floor),
  97 have qliq < 10% of liq and 96 have qliq < 5,000 USD. By pool: uniswap:v4 94 of 98 under 10%, uniswap:v3 10 of 12,
  aerodrome 1 of 6, uniswap:v2 0 of 20. Other side: WETH 99, ZORA 25, USDC 11, ETH 2, USDT 1, VIRTUAL 1.
  Lowest: USOILS liq 5,018,916 qliq 2; OILS 500,282 / 2; AODF 161,153 / 1. Highest: I O F 223,356 / 205,749 (0.92).
  So for most v4 tokens past the gate the liquidity floor is met on the token's own side. No rule change (the owner decides
  after the replay).
- Tests: tests/test_qliq.py (9): both fixtures, token-as-quote (base x priceUsd), "na" vs 0 (missing, 0, odd inputs, other
  token; a real 0.13 USD side gives 0), no request added in facts(), qliq after dex on a PASS row and not on a gate kill,
  the outcomes header rule (old-header file keeps 16 columns, the next UTC day's file has qliq last; gone -> "na"), and the
  BL-25 three-tick desk/baseline run with qliq vs the six BL-25 keys only (TRADE_KEYS patched): same rows once qliq is
  stripped, same state and TICK keys (requests included). Old tests: BL-25's exact trade dicts and strip_trade() take the
  seventh key; the BL-21 exact detail ends with qliq=na; the BL-26 gone test covers qliq. 219 pass on 3.13 (210 before).
- Live smoke (scratch copy of data/, real requests, nothing under data/ changed): tick 26 requests in 19.0 s (13 checked),
  then 4 requests in 1.5 s; 9 rows with qliq, 0 gate rows with it, request count per check unchanged. Examples:

    WATCH COTF  liq=2613982 qliq=115 dex=uniswap:v4
    WATCH CELR  liq=114713  qliq=2   dex=uniswap:v4
    WATCH TOX   liq=99727   qliq=20  dex=uniswap:v4

- Size: about 9 bytes per row with the keys (" qliq=" + ~3 digits) x ~1,630 rows a day = ~15 KB/day on tasks.csv, and
  ~4 bytes x ~390 label rows = ~2 KB/day on outcomes.csv.

## BL-29
- Replay harness for the Nov 6 review, offline and by hand only (the owner, 2026-10-30, DECISIONS): tools/replay.py.
  Not in tick.yml, not imported by core/ (test), never imports core/tick.py (test), no network (test with a blocked
  socket), writes nothing (test: no file changes), stdout only. Stdlib; reuses core/friction.py (buy_fill, sell_fill),
  core/report.py (_kv detail parser, GATE_KILLS) and core/labels.py (HORIZONS). It shows evidence; it never picks a rule.
- Run: `python -m tools.replay` (every day there is), or `python -m tools.replay --from 2026-10-31 --to 2026-11-06`;
  `--root DIR` reads another copy of the repo's data/. Days are UTC folder names.
- Reads: tasks.csv of the window; trades.csv and outcomes.csv of the window plus 2 days after it (a lot bought on the last
  day can still close, a late sighting can still get its labels). Each outcomes.csv by its own header (16 columns before
  BL-28, 17 with qliq).
- Unit (the owner): one sighting = address + first_seen (the row's first_seen=, else its ts, as core/labels.py). A sighting
  past the gate = a desk VERIFY row without goplus=skipped and not a gate kill (report.GATE_KILLS). It is in the window when
  its first such row is. A token seen again after its 24 h skip is a second sighting. Header line: number of sightings,
  how many have a qliq row, number of on-time labels.
- Buckets: by the sighting's FIRST VERIFY row that carries qliq (BL-28, from 2026-10-30 ~19:58 UTC): qliq na / <1k / 1k-5k /
  5k-25k / >=25k USD; and that same row's s24 (BL-25, summed over every Base pair): na / 0 / 1-9 / >=10. A sighting with
  no qliq row is "na" in both (so every sighting before BL-28 is "na").
- Table 1a / 1b (outcomes by qliq / by s24): per horizon (+1, +6, +24 h) and bucket, from outcomes.csv labels with
  |age_min - horizon| <= 30 min only (drops late labels, e.g. the deploy-time ones). n = labels (one per sighting and
  horizon); gone% = labels with gone=1; n_chg = labels with a chg_pct (first_price can be "na"); med_chg% = median chg_pct;
  <=-50% = share of n_chg with chg_pct <= -50; pair_s1>0% = share of non-gone labels with pair_s1 > 0 (BL-26: the deepest
  pair only; BL-25 sums and pair_* counts are never in one column). Fewer than 20 labels in a cell: "n too small" and no
  numbers.
- Table 2: every desk PASS row of the window: ts, symbol, its qliq and s24, and the on-time chg_pct at +1/+6/+24 h ("gone"
  when the label says so, "na" when there is no on-time label).
- Table 3: every desk and baseline lot bought in the window (BUY plus its SELLs, open when no closing SELL is read yet).
  liq = the logged entry liquidity; qliq = qliq@entry (baseline: its EXECUTE BUY row; desk: the PASS VERIFY row at the same
  ts). Re-pricing (option A, the owner): the BUY quote (trades.csv intended_price) through friction.buy_fill() at qliq gives
  a new quantity; each SELL sells the same share of the position at its logged quote (fill / (1 - friction%), as
  core/report.py) through friction.sell_fill() at the SAME entry qliq (exits log no qliq); new_pnl next to the logged pnl.
  fric% / new% = logged / re-priced entry friction. Caveats printed in the table header: side_pct() is a step function
  (thin under 100k USD, plus the Clanker fee), so with qliq almost every trade goes 3% -> 5% and no price impact is
  modelled; and option A can understate exit friction when the pool thinned after entry.
- size/qliq% (the owner) = trade USD / qliq@entry x 100: a plain ratio of two logged numbers, not a friction model. Under the
  table: trades with size above 10% of qliq and above 100% of qliq, and per book the logged P&L and, for the re-priced
  trades, logged -> re-priced P&L ("n too small" under 20). "na" everywhere qliq was not logged (every trade before BL-28).
- Self-check on the real trades (2026-10-30): re-pricing each lot with its own logged liquidity per side reproduces the
  logged P&L of all 38 lots within 5e-7 USD (float rounding). With the entry liquidity carried to the exits (option A's
  rule, logged liq instead of qliq), 4 lots differ, the ones whose exit liquidity crossed 100k: Kek 9.25 -> 9.03,
  SI desk -2.39 -> -2.39 (-2.3922 -> -2.3870), SI baseline -2.47 -> -2.46, TRMX 6.36 -> 6.48.
- Smoke 2026-10-30 ~20:00 UTC (all days, 2026-10-23 .. 2026-10-30): 2,066 sightings, 16 with a qliq row, 71 on-time labels.
  Only one cell reaches n 20 (1h, "na" bucket, n 53: gone 0%, n_chg 39, median chg 0.0%, <=-50% 0%, pair_s1>0 45.3%);
  every qliq / s24 bucket is "n too small". Table 2: SI only (before BL-26, no labels). Table 3: 38 lots (desk 1, baseline
  37), 0 with qliq, so 0 re-priced and the size counts are 0 / 0. BL-28 went live at the 19:58 UTC tick, so the tool is
  tested here, not the question; the Nov 6 run needs about a week of qliq rows.
- Tests: tests/test_replay.py (12): bucket edges; the first qliq row decides (an earlier row without qliq and a later one
  with another qliq are ignored), gate kills and baseline rows out, no-qliq sighting "na"/"na"; a re-sighting is a new unit;
  the 30-min filter (29 and 91 out, 30/60/90 in, late deploy labels out, "na" age out); 16- and 17-column files read side by
  side, labels from the day after the window; cells under 20 "n too small"; cell stats with "na" chg, gone and pair_s1;
  all-"na" cells; re-pricing equals core's buy_fill/sell_fill by hand (Clanker 6%), equals the logged P&L at the logged
  depth, "na" without qliq; entry qliq from the desk PASS row and the baseline BUY row, size/qliq 12.5 / 25 / na, the two
  counts and the header text in the CLI output; no network and no file change; no core.tick import, no tools import in core/.
  231 pass on 3.13 (219 before).

## BL-27
- Live view, display only (the owner's approved mock, reviewed in chat; no mock file in the repo). core/live.py writes
  data/live.json at the end of every tick; site/index.html reads it. No rule, verdict, exit or config reads live.json,
  and it makes no request (it imports no adapter; test).
- Shape (about 1 KB, json indent 1; 985 B on the live smoke with one open position):
  - as_of = tick time (UTC ISO).
  - last_tick = ts + seen new checked dex_only goplus passed bought requests secs, the TICK row's own values, plus
    exits = desk EXIT SELL rows this tick (exits.check() now returns its SELL count; the TICK row is unchanged but for live=).
  - today = day, ticks (desk TICK rows in today's tasks.csv + 1 for this tick's row, written right after live.json),
    expected (report.slots(now): the :03 :08 ... slots passed, the formula python -m core.report already used),
    errors (rows with action ERROR today). An off-schedule run (e.g. a smoke) can push ticks one above expected.
  - exit = hard_stop_pct time_stop_min time_stop_min_pct trail_pct from config.EXIT, so the site's chips never go stale.
  - books.desk / books.baseline = cash_usd, value_usd (report.liq_value, the report.json basis), open = positions oldest
    first: symbol, address, entry_ts, age_min (floored, never negative), cost_usd (the remaining qty's cost: desk.sell()
    already lowers cost_usd on a ladder sale), value_usd (remaining qty x friction.sell_fill(last price, last liquidity
    else entry liquidity, launchpad), as liq_value), value_pct (value vs cost), price_pct (last price vs entry fill, the
    EXIT row's pct=), liq_usd (last, rounded), qliq (entry_qliq, null when not known), ladders_done (names "ladder_1",
    "ladder_2", from the state's 0-based indexes), time_stop_at (entry_ts + time_stop_min, always written).
  - USD values rounded to 4 decimals, % to 2.
- Source: the books are re-read from data/state.json after this tick's saves (what the next tick will see), not the
  in-memory objects; the counts come from the tick's heartbeat dict.
- qliq choice (the owner): desk.buy() stores "entry_qliq" on a new position, from facts["trade"]["qliq"] (the BL-28 value
  already logged on that tick's VERIFY / baseline BUY row), only when it is a number; "na" or missing -> null. Both books
  (both buy through desk.buy()). Positions bought before BL-27 have no key -> null. Display only: a test runs the same
  ticks with entry_qliq real and forced to None and gets identical verdict rows, row details, trades.csv and state
  (entry_qliq aside).
- In the tick: after the labels step and after requests/secs are counted, just before the TICK row, live.run() in its
  own try. TICK detail ends with live=ok | error:<type> | none. A failure keeps every desk and baseline result, the next
  tick rewrites the file anyway (retry), and the run ends red after the TICK row (raised after labels). A crashed tick
  (reason error:<type>) writes no live.json and logs live=none (the owner): the old file stays and the site's age badge
  turns Stale, which is the truth. Writes are atomic (tmp + replace).
- Site (BL-13 page, the owner's order and look):
  - Left column, phone order: Baseline PAPER header card, tick-log terminal, Paper equity, Desk vs random, Open now,
    Gate B, How trades ended. Right column and token table unchanged.
  - Third fetch: LIVE_URL = raw.githubusercontent.com/.../main/data/live.json, same rules (fetch cache no-cache, no
    override, no query or hash switch). report.json and tokens_24h.json load once. live.json is fetched again every 5 min
    while document.visibilityState is visible (checked by a 1-minute timer), not while hidden, and once when the tab turns
    visible again. A failed re-fetch keeps the last good live.json (its age then shows Stale); a good one replaces it.
  - Header: with live.json, Live when as_of is 15 min old or less, else orange Stale; small line "tick HH:MM UTC · N min
    ago". Without it (404 or failed), the BL-13 daily-snapshot badge, unchanged. The ages are recomputed every minute, so a
    tab left open never shows a false Live.
  - Terminal: from live.json last_tick (title "baseline · tick.log · live", an "exits N desk sells" line first, footer
    "today X / Y ticks · Z errors", pill "Running ✓" or orange "No tick for N min" over 15 min); else report.json
    machine.last_tick as before.
  - Open now: header "Open now" + "live · tick HH:MM UTC"; groups Desk (solid mark) and Random (dashed) with "$value now ·
    $cash cash · n open"; per position the symbol (DexScreener link), value_pct big and coloured, "in HH:MM UTC · N min ·
    liq $X · real $Y", "$cost → $value · price ±x%", chips "ladder N sold" (green), then "time stop HH:MM UTC unless +10%"
    while no ladder has sold or "trail −25% from peak" once one has (core/exits.py switches from the time stop to the trail),
    "stop −25%", and orange "ticket N% of real depth" when cost/qliq > 5% ("no real depth ($0)" when qliq is 0). Numbers in
    chips come from live.json "exit". Empty states as approved; footnote on the liquidation basis; 404 -> "No live.json yet
    (HTTP 404)…", failed fetch -> red state.
  - Equity caption "N daily closes" -> "N days logged". The foot line names data/live.json and its tick time.
- Repo growth: live.json is rewritten every tick and the bot commits it each tick: ~1 KB per tick, ~290 KB/day of git
  history before compression. Added to the BL-16 open item.
- Tests: tests/test_live.py (16): shape and key order, values equal report.liq_value (and cash + positions = value),
  Clanker friction value, price_pct equals the EXIT row's pct=, last liquidity, ladder partial cost basis and
  "ladder_1", time_stop_at, age never negative, qliq known / "na" / pre-BL-27 null, today counts (TICK + 1, slots,
  ERROR rows), empty books and the file on disk, no adapter import; in the tick: written every tick with the TICK
  row's counts and exits = 1 on a hard stop, error keeps the PASS + BUY + TICK rows and the position, live=error then
  ok next tick, crash writes no file (live=none, old file kept), same requests with live on or off; desk and baseline
  rows, trades and state equal with live on vs broken; entry_qliq changes no decision. tests/test_site.py (+3): every
  live.json key the page reads (L, LB, LP readers) exists in a file built by core/live.py; exactly three data URLs, one
  fetch(), four load( (function + 3), only live.json re-fetched, one setInterval, visibility check. Updated: the TICK
  key lists in test_tick / test_labels (live last). 250 pass on 3.13 (231 before).
- Smoke 2026-11-01 (scratch copy of data/, nothing under data/ changed):
  - One real tick 12:40 UTC: live=ok, 10 requests, 3.0 s; live.json 985 B: desk 47.6078 cash, nothing open; baseline
    NAFF (bought 12:28, before BL-27, so qliq null) cost 5.00 -> value 4.7017 (-5.97%), price -3.06%, liq 30.0M,
    time_stop_at 13:13:13Z; today 153 ticks / 152 slots (the smoke tick is extra), 0 errors.
  - Browser (Chromium, Playwright routes for the three raw URLs, real report.json and tokens_24h.json, scratch live.json
    built by core/live.py with a ladder_1 position and a thin-qliq position): 390 and 1280 px, no horizontal scroll, no
    script error; requests = the page + the three data URLs. States: fresh (Live, "Running ✓"), 40 min old (Stale,
    "No tick for 40 min"), live.json 404 (BL-13 badge and terminal, honest 404 state in Open now), aborted fetch (red
    state). Refresh with a fake clock: live.json 1 fetch at load, still 1 at 4 min, 2 at 5 min, still 2 after 16 min
    hidden (badge redrawn to Stale 21 min meanwhile), 3 right after turning visible; report.json and tokens_24h.json 1 each.

## BL-3
- Alerts channel, display only: core/alerts.py sends Telegram Bot API sendMessage (stdlib urllib, POST, 10 s timeout) to
  the owner's phone. No rule, verdict, exit, report or live.json reads or changes anything in it.
- Secrets: env TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (repo secrets). tick.yml passes them to the Tick step only;
  alert-test.yml to its send step only. Either missing or empty (an unset secret arrives as ""): alerts=off, no request,
  nothing queued, state untouched.
- Public logs, hard rule: the token, the chat id, the request URL and Telegram's reply (it names the owner) are never
  printed, logged, written to data/ or raised. deliver() catches every exception inside itself and returns only "ok",
  "error:NotOk" (a reply without ok:true) or "error:<ExceptionTypeName>"; the exception object never leaves it and
  str(exception) is never used (urllib's HTTPError/URLError carry the URL, so the token; JSONDecodeError quotes the reply).
  The reply is read only for its "ok" field. No bot username anywhere in the repo.
- Messages, plain text (no parse_mode, so w_COIN or [BSTONK] can never break a send), link previews off. Fake values:
  - BUY:   "Baseline desk BUY SI / $5.00 at 0.00299009 / liq $159,111 · real depth $4,321 / https://dexscreener.com/base/0x..."
  - SELL:  "Baseline desk SELL SI · ladder_1 / price +47.35% · pnl +$0.71 / desk cash after $47.61"
  - daily: "Baseline daily · 2026-10-31 / desk $47.61 · random $27.62 · desk lead +$19.99 / Gate B 1/100 desk closes /
            closes that day: desk 0 · random 22 / runs 288/288 · ERROR rows 0" (the 2026-10-31 report.json:
            baseline closes 61 since BL-8, closes_day 22)
  - error: "Baseline run RED · 2026-11-01 18:28 UTC / labels error:URLError" (one line per failed step; a crash reads
            "crash error:JSONDecodeError")
  - TEST:  "TEST · Baseline desk BUY TEST_COIN / $5.00 at 0.00123456 / liq $123,456 · real depth $4,321 / https://dexscreener.com/base/0x000..."
  ("/" marks a line break.)
- Sources, this tick only, no data request (a test shows the same request count with alerts on or off):
  - BUY: desk trades.csv BUY rows at this tick's ts (usd, fill_price, liq_usd); qliq from the same tick's desk VERIFY PASS
    row (BL-28 qliq=), "na" when not there.
  - SELL: desk trades.csv SELL rows at this tick's ts (reason, pnl_usd of that sale) and the EXIT SELL row's pct= (the
    price move vs the entry fill). Desk cash after each sale = the saved desk cash + this tick's buys - the proceeds of
    the later sales this tick (exact, since buys come after exits); "na" when the state can't be read.
  - daily: the report dict run_due() just wrote (only when report=ok, so never after a failed report): desk/baseline
    value_usd, desk_lead_usd, gate_b (running desk closes), "closes that day" = closes_day of both books (never closes); runs = desk TICK rows of the closed day / 288; ERROR rows
    of the closed day (both books), from that day's tasks.csv.
  - error: the steps that end the run red: crash, report, machine, baseline (first baseline error), labels, live, each
    with its exception type name. At most one per hour (state last_error = when the last one was queued). On a crash
    with an unreadable state.json the throttle comes from the TICK rows instead (a red TICK row with alerts_sent>0 in the
    last 60 min, today and yesterday), so a broken state can't send one alert every 5 min; the broken file is never
    written by alerts.
  - Baseline (random book) trades never send anything.
- Order and queue: new alerts this tick = error, then SELLs, then BUYs, then daily, appended to state "alerts".queue
  ({kind, at, text}); more than 20 waiting drops the oldest; one older than 60 min is dropped; up to 3 sends per tick,
  oldest first, stopping at the first failure (at most one 10 s timeout per tick; the rest wait). State is written only
  when the queue or last_error changed, so a tick that sends everything it built leaves state.json untouched. Texts in
  the queue carry only desk numbers (symbol, address, prices), nothing secret.
- Quiet hours 22:00 to 07:00 America/Toronto (zoneinfo, DST-safe), decided at send time: disable_notification=true;
  never dropped or delayed for quiet hours.
- In the tick: after live.json (and its live=), before the TICK row, also on a crashed tick (live=none there), in its own
  try. TICK detail ends with alerts=ok|off|none|error:<type> alerts_sent=<n>: off = no secrets, none = nothing to send,
  ok = at least one sent and none failed, error = a send (or the state write) failed. An exception out of alerts.run()
  logs alerts=error:<type> alerts_sent=0. Alerts never raise out of the tick and never make the run red; the red steps
  still re-raise as before. secs (counted before live) does not include the sends (up to 3 x 10 s worst case).
- alert-test.yml: workflow_dispatch only, main only, permissions contents: read, checkout with persist-credentials:
  false, timeout 2 min; runs python -m core.alerts --test, which sends one TEST message shaped like a desk BUY (quiet
  hours apply), writes nothing, commits nothing and prints only ok, off or error:<type>; exit 0 on ok, 1 on off or
  error, so a missing secret shows red. No set -x, env dump or debug output in tick.yml or alert-test.yml (test).
- tests/__init__.py pops both env names, so the suite can never send a real message whatever the shell exports.
- Tests: tests/test_alerts.py (32): the four formats and the TEST text; closes that day = closes_day for both books
  (a report with different closes and closes_day per book); plain text with w_COIN*[BSTONK]_`x` sent
  verbatim, no parse_mode, POST, 10 s, link previews off; quiet hours on both sides of the 2026-11-01 and 2026-03-08 DST
  changes (same UTC time, other answer) and disable_notification; no secrets (missing or empty, either one) = no request,
  alerts=off, no state key; BUY and SELL alerts from real tick rows (qliq, cash after); baseline trades send nothing;
  daily after the day-roll report (runs 1/288) and none after a failed report (error alert instead); one error alert
  per hour naming the step; crash alert with a broken state (once per hour via TICK rows, no live.json, state file
  untouched) and with a readable one; a failing send (URLError) keeps every TICK key but alerts, every row, the state,
  live.json, report.json and trades.csv equal to alerts off, and no run red; alerts.run raising -> alerts=error, run
  green; retry next tick and the 1 h drop; max 3 sends and 20 waiting; stop at the first failure; same request count.
  Leak test with a fake token, chat id, owner name, handle and bot username in every Telegram answer (ok, HTTPError,
  URLError, timeout, ok:false, garbage): none in stdout (log=print), stderr, any file under data/ (tasks.csv,
  trades.csv, state.json, live.json, outcomes), or the formatted traceback of the run's raised error; the --test path's
  stdout and stderr hold only ok/off/error:<type>, and it changes no file in the repo. Workflows: alert-test.yml is
  dispatch-only, contents: read, no write anywhere, no commit or push; the secrets appear only in the Tick step and the
  send step; no debug output. Updated: the TICK key lists in test_tick / test_labels (alerts last). 282 pass on 3.13.
  A mutation check (str(e) in the result, printing the reply, parse_mode, no quiet hours, 4 sends) fails the suite each time.
- Smoke 2026-11-01 (scratch copy of the repo and data/, secrets unset, nothing under data/ changed): one real tick
  alerts=off alerts_sent=0, live=ok, 33 requests, 20.6 s, no "alerts" key in state; python -m core.alerts --test printed
  "off", exit 1.

## External review 2026-11-02
- Desk: 1 PASS and 1 trade since Oct 23 (SI, -2.39 USD); the funnel is dominated by WATCH goplus_not_ready and SKIP liquidity; Gate B is unreachable as built.
- The random book is not a fair yardstick: it buys stables, wrapped assets and same-symbol clones at new addresses, has no age filter in eligible(), and sits near the MIN_TICKET cash floor. Gate B comparisons are on hold until R3.
- Outcome labels undercount deaths: many +1 h labels show qliq < 100 USD with gone=0 and a price still above -50% (likely a leftover dust pair). No depth rule from these labels until R2.
- Fills on fiction: paper tickets are above 10% (some above 100%) of real depth; friction is a 3/5% step with no price impact (R4).
- Kill-test: the launch desk enters after deployers and snipers on a 5-min clock, so it is exit liquidity. Parked as a money candidate, kept logging.
- Proposed direction: survivor momentum on real depth, backtest first; no 45-min time stop in the new strategy.
- Execution risks for later: a DEX stop is not a resting order (SI fell -92% within one tick); public Actions logs mean no key on Actions; a dead-man rule and a flatten-only kill switch are needed.

## BL-30
- R2 label fix, logging only (the owner, 2026-11-05, DECISIONS): no rule, config value, verdict or exit reads dead,
  sight_pair, label_pair, sight_qliq, best_qliq, pair_age or token_age. Code: core/labels.py (DEAD, dead(), best_qliq(),
  sight(), row()),
  adapters/base.py ages(), core/verify.py TRADE_KEYS, tools/replay.py. No existing threshold in core/config.py changed.
- Why: the +1/+6/+24 h label reads the ONE pair /tokens/v1 answers (the deepest), so after the real depth is pulled a
  drained or leftover dust pair can answer with gone=0 and a stale price. Chat counted 2,543 of 3,261 live labels with
  gone=0 and label qliq < 100 USD (recount here 2026-11-05 ~13:20 UTC, every label with a qliq column: 2,536 of 3,346). Seen in
  the replay: sightings with qliq ~34,000 USD at first sight whose +1 h label is a uniswap:v2 pair with liq 3-4 USD,
  qliq 2 and chg_pct +30% to +42% (NOTF, ARWW, USOE, USDP, LOIRD...). Old files log no pair address, so whether that v2
  pair is the sight pool drained or a second pool cannot be told; today /token-pairs/v1 lists ONE Base pair for each of
  25 such tokens checked (PAD, UKRO, DFUS, USOIL XCHA, BOOTR, TRUMP2028, SI...), so mostly the same pool drained (or the
  other pool delisted). From 2026-11-06 sight_pair vs label_pair answers it.
- Dead flag: core/labels.py DEAD = {"floor_usd": 100, "share": 0.10}, the one place the two numbers live (the replay
  imports dead()). dead(gone, label_qliq, sight_qliq), where label_qliq = best_qliq (the owner, option A, 2026-11-05:
  a holder can sell into any pair):
  - 1 when gone = 1; 1 when the label qliq is known and < 100 USD, whatever the sight qliq;
  - "na" only when the answer depends on a missing number: label qliq "na", or label qliq >= 100 and sight qliq "na";
  - else 1 when label qliq < max(100, 0.10 x sight qliq), 0 otherwise. The exact edge is alive (200 vs sight 2,000 -> 0;
    199 -> 1; 100 vs sight 500 -> 0).
  - qliq 0 is a real value (BL-28: a near-empty side rounds to 0) and gives 1; only "na" (or an odd value) is missing.
- outcomes.csv: five columns after qliq: sight_pair label_pair sight_qliq best_qliq dead (22 columns). Header rule as
  BL-28: the 2026-11-05 file keeps 17 columns to the end of its UTC day; the 2026-11-06 file is the first with 22. Old
  files are never rewritten.
  - best_qliq = the highest quote_liq() over every Base pair of the token in the same /tokens/v1 batch answer (no new
    request); "na" when none is known, a real 0 counts. qliq stays the deepest pair's (BL-28 meaning unchanged) and
    label_pair stays the pair the other label columns describe (price, liq, pair counts, dex, qliq).
  - PAIR CAP: the batch answer holds ONE pair per token. Probe 2026-11-05 (read-only): 30 label addresses -> 20 pairs,
    exactly 1 per token found (10 gone); BRETT and DEGEN have 30 Base pairs each on /token-pairs/v1 and 1 each in the
    batch (BL-26's probe: CDXR 18 vs 1). So today best_qliq = qliq unless another token's pair in the same answer also
    holds this token, and a real pair behind a token-heavy deepest pair is not seen. Live check of the size of that gap:
    50 thin labels (label qliq < 100, gone 0: 25 from today's file, 25 that died after sight with sight qliq up to
    106k) re-read on /token-pairs/v1: all 50 have ONE Base pair, best over all pairs = the logged qliq, 0 reach 100 USD. Seeing every
    pair would need one /token-pairs/v1 request per label token (a new request: ~1.4 labels due per tick on average,
    bursts up to 60); not built (no new request, the owner).
  - sight_pair and sight_qliq = facts()["pair"] (pick_pair, the deepest Base pair at that check) and its qliq, stored in
    the state entry ONLY when the check IS the first sight (first_seen = now), like first_price; a later re-check never
    gives them. "na" for entries already pending at deploy (old state shape) and for a token re-added after a failed
    labels step. label_pair = the pairAddress of the pair the label read; "na" on gone rows (best_qliq "na", dead 1).
  - State: pending entries gain sight_pair (~68 B for a v4 id) and sight_qliq: ~+80 B per entry, ~+20 KB on state.json
    at ~250 entries. Rows: ~+125 B per label row x ~390-1,100 rows a day = ~50-140 KB/day more on outcomes.csv.
- Token age at sight: TRADE_KEYS gains pair_age and token_age after qliq, so every row with BL-25's keys ends with them
  (desk VERIFY rows past dex_skip() and baseline EXECUTE BUY rows); gate-killed rows unchanged. Whole minutes, floored
  (14.9 -> 14, so pair_age agrees with the age rule's strict comparisons), "na" when missing. Named apart from
  outcomes.csv age_min (minutes since first sight).
  - pair_age = age_min as facts() built it for the age rule: the chosen pair's DexScreener pairCreatedAt, else its
    GeckoTerminal pool_created_at (the request facts() already makes in that case).
  - token_age = the oldest creation time over every Base pair with our token in the /token-pairs answer already fetched,
    the chosen pair included (with its GeckoTerminal time when DexScreener had none), so token_age >= pair_age.
  - Both are pair (pool) creation times, NOT the token contract's creation: that would need a new request (explorer or
    RPC). ASSERTED, not verified here: Clanker and Zora create token and pool in the same transaction, so for them the
    first pool's time is the launch time. For other launches token_age can be later than the contract (a token deployed
    long before its first pool) and a pair DexScreener does not list is not counted.
  - Live example (smoke): SNDKc, a goplus_partial SKIP: aero pair liq 1.12M, qliq 197,616, pair_age=20785 (14 days),
    token_age=87425 (61 days): an older pool exists than the one now deepest.
  - Size: ~25 B per row x ~1,630 rows a day = ~40 KB/day on tasks.csv.
- Replay (tools/replay.py): Table 0 = death rate per horizon over the on-time labels of the window's sightings (same
  scope as Tables 1a/1b): n, gone, gone%, dead, na, dead%known = dead / (n - na), dead%n = dead / n ("na" counted alive:
  a floor), each horizon split by depth at sight: all / "<100" born thin (sight qliq < 100 USD, never had real exit
  depth) / ">=100" died after sight / "na" (sight qliq = the logged sight_qliq, else the join below). Tables 1a/1b gain
  dead% (of the known flags) and d_na; Table 2 marks a dead label with "*".
  - APPROXIMATION on old rows (flagged in Table 0 too): their dead uses the logged deepest-pair qliq, not best_qliq (not
    logged then). With the one-pair batch answer the two agree; they would differ only if the answer ever held more.
  - A file with a dead column: used as logged. An older row (16 or 17 columns): derived with the same dead() from gone,
    the label qliq (a 16-column file has none: "na") and the sight qliq = the qliq on the desk VERIFY row (past the gate)
    for that address whose ts IS the label's first_seen, never a later re-check (the owner, rule 5). With rule 1 the
    join only matters when the label qliq is known and >= 100. The join also reads the tasks.csv of the day before the
    window (a first sight on the eve of the window); nothing else changes in the window rules.
  - Under Table 0: rows with a logged dead, and the old rows split into decided without the join (gone, or label qliq
    < 100), label qliq na, and needed the join (sight qliq found / stayed na).
- Replay 2026-10-31 .. 2026-11-05 (run 2026-11-05 ~14:30 UTC; all old 17-column files, nothing logged yet, so every
  dead is the deepest-pair approximation): 1,194 sightings, 3,354 on-time labels, 3,237 of them for window sightings.

  | horizon | sight | n | gone | gone % | dead | na | dead % of known |
  |---|---|---|---|---|---|---|---|
  | +1 h | all | 1,169 | 0 | 0.0 | 831 | 1 | 71.1 |
  | +1 h | <100 born thin | 615 | 0 | 0.0 | 607 | 1 | 98.9 |
  | +1 h | >=100 died after | 554 | 0 | 0.0 | 224 | 0 | 40.4 |
  | +6 h | all | 1,126 | 0 | 0.0 | 916 | 2 | 81.5 |
  | +6 h | <100 born thin | 604 | 0 | 0.0 | 593 | 1 | 98.3 |
  | +6 h | >=100 died after | 522 | 0 | 0.0 | 323 | 1 | 62.0 |
  | +24 h | all | 942 | 74 | 7.9 | 820 | 6 | 87.6 |
  | +24 h | <100 born thin | 531 | 63 | 11.9 | 505 | 3 | 95.6 |
  | +24 h | >=100 died after | 411 | 11 | 2.7 | 315 | 3 | 77.2 |

  Sight "na": 0 at every horizon (every window sighting has a qliq row). Old rows 3,237 = 2,538 decided without the join
  + 9 label qliq "na" + 690 needed the join (690 found, 0 stayed "na"; without the eve-of-window day, 8 stayed "na").
  About half the labelled sightings were born thin: they never had 100 USD of real exit depth, so "dead" there means
  "never alive", not a death after sight. By first-sight qliq at +1 h: <1k 90.5% dead, 1k-5k 4.8%, 5k-25k 15.0%, >=25k
  72.2% (median chg +28.0%: the drained-pool case). By s24 at +1 h: 0 sells 89.4%, 1-9 39.4%, >=10 14.0%.
  Evidence only; no depth or sells rule from it (the Nov 6 review decides what to test next).
- Cross-check with chat (thin labels = gone 0 and label qliq < 100, every label with a qliq column, by the qliq of the
  FIRST desk VERIFY row per address since Oct 30): chat 1,675 born thin / 208 at 100-5k / 625 at 5k+; here, same method,
  1,675 / 208 / 627 (+31 with no qliq row), 2,541 labels: the 2 extra at 5k+ came in after chat's count. Table 0's rule
  (the row AT first_seen, the owner's rule 5) on the same labels: 1,643 / 207 / 627 and 64 "na": 33 labels whose first
  sight has no qliq row (first seen before BL-28 went live, 2026-10-30 ~19:58 UTC) move to "na". Table 0's own counts are
  smaller again because it holds on-time labels of window sightings only, alive ones included.
- Tests: tests/test_dead.py (31): the numbers in one place; gone (also with every other input missing); under the floor
  without a sight qliq; qliq 0 real (dead() and a real 0.20 USD side through row()); unknown label qliq, unknown sight
  qliq above the floor, odd shapes -> "na"; the share and floor edges (also as csv strings); sight(): first sight keeps
  pair + qliq (a real 0 kept), a re-check gives "na", missing -> "na"; rows: the dust pair (gone 0, price answers, qliq 2
  -> dead 1, both pairs and the sight qliq logged), the share edge through row(), gone row (best_qliq "na"), unknown
  label qliq, unknown sight qliq, an entry pending before BL-30; best_qliq: (a) a token-heavy deepest pair (liq 900k,
  qliq 2) with another pair at 100 USD exactly (sight 500) or 4,000 (sight 34,000) -> dead 0, and 2,000 vs sight 34,000
  -> dead 1, while qliq, liq and label_pair stay the deepest pair's; (b) every pair under 100 (2, 60, 99) -> dead 1;
  Base pairs only, "na" when none known, a real 0 counts; today's 17-column file keeps 17 and FIELDS has 22; ages:
  pair_age = floor(age_min) and token_age = the oldest pair (+90 min), no request added, GeckoTerminal age for both,
  "na" and never raises, floored; replay on an old 17-column file: the six cases (join dead at 400 vs sight 5,000 where
  the later re-check's 1,000 would say alive, dust, gone, unknown, real 0, exact edge alive), the join uses the row at
  first_seen only, the depth split (logged sight_qliq, join fallback, the 100 edge, a real 0, "na"; death_table groups),
  a logged dead is used as is, 16 columns -> "na" unless gone, the eve-of-window join, Table 0 lines, the approximation
  note and the counts in the CLI output.
  Old tests updated for the two new keys and the new columns (BL-25 exact dicts, BL-21 detail, BL-28 header/order and
  the desk-unchanged run now strips qliq + ages, BL-26 gone row and state keys, BL-29 16/17-column fixtures).
  313 pass on 3.13 (282 before).
- Smoke 2026-11-05 (scratch copy of data/, real requests, nothing under data/ changed): tick 28 requests in 19.7 s,
  13 checked, labels=ok due 5 done 5, live=ok alerts=off; the day's outcomes.csv kept 17 columns; every new row with
  BL-25's keys ended with pair_age token_age, 0 gate rows with them. Next tick: 1 first sight (SNDKc) stored with
  sight_pair and sight_qliq. Then, smoke only, one label pass on a copy holding only that entry with the clock moved
  +61 min (real data fetched at once) gave the 21-column row below (before best_qliq). After best_qliq: a fresh smoke
  tick (27 requests, 19.2 s, today's file still 17 columns) and the same fake-clock pass on its 2 first sights gave
  22-column rows, e.g. BOOTR uniswap:v2 qliq 97,457, sight_qliq 96,750, best_qliq 97,457, dead 0 (sight_pair =
  label_pair). No baseline BUY happened (random book cash 3.19 USD, under MIN_TICKET); its ages are covered by the tests.

    2026-11-05T13:16:29Z,desk,VERIFY,SNDKc,0xb200...a10c5,SKIP,goplus_partial,"until=2026-11-06T13:16:29Z b1=716 s1=862
      b24=15960 s24=17813 liq=1121736 dex=aero qliq=197616 pair_age=20785 token_age=87425"
    2026-11-05T14:17:43Z,SNDKc,0xb200...a10c5,2026-11-05T13:16:29Z,1h,61,973.25,973.22,-0.0,1121298,481,580,12541,14259,
      aero,0,197397,0x2836e062a3fe75943a7f4a552e2365b98e414e75,0x2836e062a3fe75943a7f4a552e2365b98e414e75,197616,0
    2026-11-05T14:27:41Z,BOOTR,0x1012...42c,2026-11-05T13:25:51Z,1h,62,1.426e-05,1.446e-05,1.4,194909,7,0,7,0,uniswap:v2,
      0,97457,0xf89fe45c65ba94680106577f19f4c3e1c5dc5b65,0xf89fe45c65ba94680106577f19f4c3e1c5dc5b65,96750,97457,0

## Nov 6 review replay
- python -m tools.replay --from 2026-10-31 --to 2026-11-06 (run 2026-11-06 ~17:00 UTC, offline, read-only): 1,436
  sightings, 4,152 on-time labels. Rows with a logged dead (the 2026-11-06 file, 22 columns): 473; older rows derived
  (deepest-pair approximation): 3,562 = 2,763 decided without the join + 9 label qliq "na" + 790 needed the join (790 found).

  | horizon | sight | n | gone | gone % | dead | na | dead % of known |
  |---|---|---|---|---|---|---|---|
  | +1 h | all | 1,422 | 0 | 0.0 | 971 | 1 | 68.3 |
  | +1 h | <100 born thin | 653 | 0 | 0.0 | 645 | 1 | 98.9 |
  | +1 h | >=100 died after | 769 | 0 | 0.0 | 326 | 0 | 42.4 |
  | +6 h | all | 1,385 | 0 | 0.0 | 1,120 | 2 | 81.0 |
  | +6 h | <100 born thin | 651 | 0 | 0.0 | 637 | 1 | 98.0 |
  | +6 h | >=100 died after | 734 | 0 | 0.0 | 483 | 1 | 65.9 |
  | +24 h | all | 1,228 | 92 | 7.5 | 1,046 | 31 | 87.4 |
  | +24 h | <100 born thin | 636 | 72 | 11.3 | 607 | 4 | 96.0 |
  | +24 h | >=100 died after | 592 | 20 | 3.4 | 439 | 27 | 77.7 |

  Sight "na": 0 at every horizon. Table 3: 64 random-book trades, logged -42.41 USD, re-priced at qliq@entry -45.87 USD;
  desk 0 trades in the window.
- Chat's check reproduced: in data/daily/2026-11-06/outcomes.csv to 16:38 UTC (rows stamped up to 16:38:59), 212 of 216
  dead labels with gone 0 and both pairs logged have sight_pair = label_pair: the sight pool itself drained, not a
  second pool (215 / 211 if the cut is strictly before 16:38:00).

## BL-31
- R3 random twin (the owner's Nov 6 decisions a-h, DECISIONS). Code: core/verify.py (universe_skip, sells_skip,
  age_skip, asset_kind, norm_symbol, ASSET, QUOTE_ASSETS), adapters/base.py (side_counts, facts() "asset"),
  core/desk.py (fixed ticket, cooldown, same-symbol holding), core/book.py (week, refill_due, refill),
  core/baseline.py (eligible, restart), core/tick.py (restart + weekly refill after the day roll), core/report.py
  (refills, per_trade, gate_b, net fields), core/config.py (TICKET_USD 5, REFILL_TO_USD 50, COOLDOWN_H 24; SIZE_PCT and
  MAX_TICKET "not read since BL-31"). No other threshold changed. No site, alert or workflow change. No new request.
- Same universe: the twin's pool = this tick's leads that got a facts() call (checked, or deferred by the GoPlus cap),
  minus tokens it held at the tick start, whose DexScreener side passes universe_skip():
  - shared with the desk (the same functions, called, not copied): dex_skip() = fetch_error, missing:<symbol | price |
    liq_usd | age_min | sells_1h>, liquidity (min_liquidity_usd 50k), asset:<kind> (new); sells_skip() = sells_1h 0
    (nobody_can_sell); age_skip() = pool age < min_age_min 15 or > max_age_h 48 (too_young / too_old). hard_skip() calls
    sells_skip() and age_skip() at their old place, so the desk's labels and outcomes are unchanged except asset:<kind>.
    BL-32's real-depth floors go into universe_skip(), so both books get them by one change.
  - not shared (GoPlus, no extra call for the twin): honeypot, mint_open, freeze_or_blacklist, hard risk flags, LP lock,
    dev_pct, top10_pct, buy/sell tax, goplus_partial, goplus_not_ready. The twin still buys tokens the desk's GoPlus step
    would kill or has not read (desk= tag on its BUY rows), so the BL-8 bias stays: Gate B stricter for the desk.
  - inherited from the desk's queue: only leads the desk fetched this tick (24 h skips, watchlist timing, MAX_CHECKS).
- Asset exclusion, both books, before any GoPlus call (f): adapters/base.py facts() sets "asset" =
  core/verify.py asset_kind(symbol, price, address, base_pairs, quote_pairs) right after the DexScreener step, from the
  /token-pairs answer already fetched (also in the twin's dex dict); dex_skip() turns a kind into the gate skip
  asset:<kind> after liquidity (a low-liquidity stable still logs "liquidity"); final 24 h skip, goplus=skipped, counted
  as a gate kill on the machine cards. A missing asset key is "" (a classification, not a number).
  - stable: normalized symbol contains USD, or matches 0-2 letters + EUR/GBP/CHF/JPY/CAD/AUD/SGD + 0-1 letter (tGBP, EURC,
    XSGD, jEUR; not EUREKA, AUDIO, CADDY), or is DAI/GHO/FRAX.
  - wrapped: price >= 100 USD (wrapped ETH/BTC and tokenized stocks), or (W|WT|CB|ST|WST|MS|R|WE|EZ|RS|T|L|SUPERO)?(ETH|BTC)B?
    (cbETH, wstETH, weETH, msETH, cbBTC, WBTC, superOETHb, and clones named that way at any price).
  - quote: ZORA by address (QUOTE_ASSETS; WETH, ETH and USDC/USDbC/DAI are already dropped at the scan), or the token is the
    quoteToken in more of its Base pairs than the baseToken (a tie is not).
  - HEURISTICS: "price >= 100 USD = wrapped" and "symbol contains USD = stable" hold for the fresh-launch universe only
    (pools 15 min - 48 h old); revisit at BL-34 and BL-39 before any wider or older universe. Known edge: "U.S DIVIDEND
    PROGRAM" normalizes to USDIVIDENDPROGRAM and counts as stable.
  - Would-have-cut count, desk leads Oct 31 - Nov 6 (read-only from tasks.csv; price = first_price in outcomes.csv, else
    the trades.csv BUY price; 4 tokens without a price checked by symbol only): of 8,322 distinct desk VERIFY tokens,
    1,419 got past the gate; the rule would have cut 82 of them: stable 53 tokens (488 VERIFY rows; TOKENUSDC 16, USDF
    10, USDP 7, USDA, USDG, USD1, GUSD, RLUSD, msUSD, nUSDb, jEUR...), wrapped 29 (228 rows: 7 by name, WBTC x4,
    cbETH, weETH, msETH; 22 by price, e.g. TOR 2,419, AODF 2,121, DIEM 1,772, cbZEC, SNDKc, TSLAc, AMZNc, MSTRc, wtCOIN
    145, UDTF x4 ~180, FOTF, AOTF, ADP ~151, USOE 711, NAFF 303, UODF 224), quote 0 (ZORA never seen; the pair side is not
    logged, so the quote-side rule cannot be counted from old logs). 255 gate-killed tokens also match by symbol; they keep
    their earlier gate reason. None of the 82 was a desk PASS. In the random book's 64 trades: msUSD x4, wtCOIN x4,
    USDF x2, USDR, USDL, tGBP, msETH, cbETH = 16.
- Fixed ticket (b): desk.size() = TICKET_USD (5) capped by liq_usd x max_ticket_share; SKIP size when that cap is under
  MIN_TICKET or cash < the ticket. With the 50k liquidity floor the cap is >= 500 USD, so every buy is 5.00 USD.
- Cooldown (c) and same-symbol holding (g), both books, in desk.buy()/desk.sell() (the twin uses the same code):
  every SELL (any rule, ladder_1/ladder_2 included) stores state "cooldown" {"a:<address>": until, "s:<SYMBOL>": until}
  in that book, until = sell ts + COOLDOWN_H; expired keys are pruned on the next SELL. entry_block() order: paused,
  loss_halt, holding (the address, or an open position with the same normalized symbol), reentry_cooldown, max_open.
  A symbol with no letter or digit (emoji only) keys on the address only. A twin pick blocked by the cooldown stays due
  (like max_open) and picks again next tick with a new seed.
- Refills (b): Book.refill(now, reason): amount = max(0, REFILL_TO_USD - cash) to cash, to day_start_value (so the loss
  halt never sees it as a gain and still halts at -30% of the refilled start) and to refills_usd; sets refill_week; one
  tasks.csv row book=<book> stage=BOOK action=REFILL reason=weekly|restart detail "amount= cash_before= cash_after=
  week=<ISO week>" (amount 0.00 is logged too). Not in trades.csv. Due on the first tick whose ISO week differs from
  refill_week (the Monday 00:03 UTC roll; a later tick if that one is missed), after report.on_roll() snapshots the day
  just closed and after both books' roll_day().
- Restart (b, h): core/baseline.py restart() once, when report state has no per_trade_since: the twin's refill
  reason=restart, both books get refill_week = this week, per_trade_since = the tick ts; the books are saved before the
  report state (a crash in between repeats it with amount 0, never loses it). The desk is not refilled at the restart.
  ticks_since_buy is kept (752 at deploy, so the twin is due at once).
- Report (b, h): per book refills_usd (at the report day's close; equity entries now keep refills_usd, older entries had
  none = 0) and per_trade {since, n, net_usd, net_per_trade_usd, net_per_trade_pct}: positions bought at or after
  per_trade_since, closed by the report day, net = the sum of their SELL pnl_usd (after friction, ladders included).
  net_pnl_usd = value - 50 - refills_usd; net_pnl_pct and multiple over 50 + refills_usd; peak and max_dd_pct on value -
  refills_usd (the twin's old 94.7% stays as history); desk_lead_usd = desk net - baseline net (the value gap when
  neither was refilled). gate_b = {since, desk_closes, baseline_closes, target 100, kill 200} from per_trade; "closes"
  keeps counting from "since" (2026-10-29) as before. value_usd, cash_usd and equity_daily stay plain values.
- Display (BL-43, chat's card; nothing changed here): the site and the Telegram daily summary print value_usd, so after a
  refill the twin shows ~50 USD and the equity chart steps up on refill days; the site's "Closes", win rate and avg
  win/loss still count from 2026-10-29; desk lead, net P&L and Gate B n/100 are already honest from report.json.
- Book name stays "baseline" (state key, book column, report key, site and alerts all key on it); docs call it the
  random twin.
- Tests: tests/test_twin.py (35): asset kinds (stables seen, wrapped by name and by price with the 100 edge, quote by
  ZORA and by side majority, launch names kept, never raises, norm_symbol), universe rules and order, the same
  function as the desk (a config or helper change moves hard_skip() and universe_skip() together), desk labels
  unchanged, eligible() through universe_skip(), real facts(): asset "" on a launch, stable symbol = gate skip with no
  GoPlus call, side counts (Base only), the desk row SKIP asset:wrapped goplus=skipped final 24 h; fixed ticket, cooldown
  by address and normalized symbol and its 24 h edge, a ladder sell starts it, pruning, per book, emoji symbol, holding
  by symbol per book; refills (week, due, amount, halt, row, amount 0); the tick: restart once (46.81, desk untouched,
  refill_week), the restarted twin buys 5.00, twin skips an asset and a young pool, Monday refill of both books after
  Sunday's snapshot (report of Sunday without Monday's refill, net -46.81, desk lead); report: per_trade from the
  restart only, refills never P&L, gate_b, max_dd net of refills, old equity entries. Old tests updated: fixed ticket
  sizes, distinct symbols where a test bought "TEST" twice, the restart row first in three tick tests, gate_b and the
  new book keys. Review adds (+3): a price >= 100 USD lead through the real facts() is never in the twin's pool (99.99 in, 100 and 2,120.92 out); the restart fires once (state flag report per_trade_since) across a reloaded state.json, a day roll and a Monday; each sale refreshes the cooldown (ladder, then a final exit 30 h later: free exactly 24 h after the last sale). 348 pass on 3.13 (313 before).
- Smoke 2026-11-06 (scratch copy of data/, real requests, nothing under data/ changed): tick 1 (17:21 UTC): 21 requests
  in 14.8 s, scan answered 0 leads that run, 10 re-checks; one row "baseline,BOOK,,,REFILL,restart,amount=46.81
  cash_before=3.19 cash_after=50.00 week=2026-W45"; twin due, eligible 0 (every re-check had s1=0), SKIP no_eligible;
  per_trade_since 2026-11-06T17:21:50Z. Tick 2 (17:22): 20 leads, 27 requests in 10.1 s, 16 dex_only, 4 WATCH, 0 PASS;
  twin eligible 3, BUY AODF 0x2128ac... usd=5.00 (seed 202611061722, pick 0, qliq 731, pair_age 17). By-hand
  report on the copy: gate_b {since 2026-11-06T17:21:50Z, desk_closes 0, baseline_closes 0, target 100, kill 200};
  desk value 47.61, net -2.39, refills 0, per_trade n 0; baseline value 49.71 (1 open), cash 45.00, net -47.10, refills
  46.81, multiple 0.5135, per_trade n 0; desk_lead_usd +44.71.
  The AODF the twin bought (0x2128ac..., price 0.01931, asset_kind "") is not the AODF in the would-have-cut list
  (0x75f514..., first price 2,120.92): the symbol has at least 11 Base addresses; the rule ran on the twin's path.

## BL-32
- R4 real-depth floor and impact friction (the owner's Nov 6 decisions a-f plus the go-message changes 1-4,
  DECISIONS). Code: core/config.py (SKIP_IF min_qliq_usd 10,000; min_liquidity_usd kept as the logging floor; FRICTION
  thin_pct read only by the no-qliq fallback, thin_liq_usd not read), core/verify.py (qliq last in DEX_FIELDS and in
  REQUIRED, depth_skip(), DEPTH_SKIPS, logged(), trade_detail() on logged()), adapters/base.py (facts() top-level qliq,
  quote() qliq), core/friction.py (rewritten), core/desk.py (size() on qliq, buy/sell notes, entry_qliq from
  facts["qliq"]), core/exits.py and core/book.py (qliq to the sell and the mark), core/report.py (sell_value(),
  gate_restart(), GATE_KILLS + real_depth), core/live.py (report.sell_value()), core/tick.py (sights on logged(),
  gate_restart() after the twin restart), core/baseline.py (a fresh restart sets gate_restart), tools/replay.py.
  No site, Telegram or workflow change. No new request. Exit rules unchanged.
- qliq in the facts: facts() sets facts["qliq"] = base.quote_liq() of the chosen pair (the same number as the logged
  trade qliq, BL-28; "na" there = the key is absent here, 0 is real). It goes into the twin's dex dict too, so
  universe_skip() sees the same qliq for both books.
- The floor, in dex_skip() (so hard_skip() via goplus_skipped, and universe_skip()): fetch_error, missing:<symbol |
  price | liq_usd | age_min | sells_1h | qliq>, real_depth (qliq < min_qliq_usd; 10,000 passes, 9,999 and 0 skip), then
  asset:<kind>. real_depth replaces "liquidity" at the same place, so a thin stable logs real_depth, not asset. Final
  24 h skip, goplus=skipped, no GoPlus call (facts() returns before GoPlus). hard_skip()'s two old liquidity checks (the
  GoPlus-pending branch and the post-sells check) now read the same floor. The total liquidity (liq_usd) is still a
  required number (missing:liq_usd), still logged, still read by EXIT liq_pull, never by an entry rule.
- Logging set unchanged (the owner, go-message 1, option A): core/verify.py logged(f) = True for a check GoPlus was asked
  about (as before), or a goplus_skipped check whose dex_skip() reason is real_depth or missing:qliq AND liq_usd >=
  min_liquidity_usd (50k, the logging floor) AND no asset kind: exactly the checks the old gate let through, now
  stopped only by real depth. Those VERIFY SKIP rows keep the full BL-25/28/30 keys (b1 s1 b24 s24 liq dex qliq
  pair_age token_age) after goplus=skipped until=..., and the tick hands them to the BL-26 labels (sight_qliq included).
  Checks new to the universe (qliq >= 10k, liq < 50k) pass dex_skip() and are logged like any check past the gate.
  logged() is never read by a trading rule and never raises. Test: a fixed tick of 8 leads (pass, real_depth at 80k,
  missing:qliq at 80k, new universe at 20k/15k, thin at 20k/4k, thin stable at 80k, safety skip, real 0 at 80k): the
  label set = the old set {pass, real_depth, missing:qliq, safety, real 0} + the new-universe token only; the trade keys
  sit on exactly those rows; only qliq >= 10k reaches GoPlus or a buy.
- Ticket (b): desk.size(cash, qliq): TICKET_USD capped by qliq x max_ticket_share (0.01); "size" when the cap is under
  MIN_TICKET, qliq is unknown or cash is short. 5 USD needs qliq >= 500, so above the floor it never binds.
- Friction (c, go-message 2 and 3), core/friction.py, the one function both books, report.liq_value() and live.json use:
  side_pct(usd, qliq, launchpad) = base_pct 3 + pool_fee_pct (Clanker 1) + impact_pct(usd, qliq), impact = 100 x usd /
  qliq, capped at 100 (qliq 0 = the cap). qliq unknown = thin_pct 5 in place of the impact (never the total
  liquidity: it is mostly the token's own side and flatters paper, the owner). buy_fill(price, usd, qliq, lp): the
  ticket into the entry qliq. sell_fill(price, qty, qliq, lp): side USD = qty x quote (gross), whole sell side capped
  at 100% (fill 0, proceeds 0, never negative). Exit depth (exit_depth()): this tick's quote qliq when base.quote()
  could compute it from the pair it just read ("exit"; no new request), else the position's entry_qliq ("entry"),
  else none ("none", thin_pct). The report's liquidation value and live.json use the last mark's qliq (Book.mark stores
  last_qliq from this tick's quote and drops it when the quote had none), else entry_qliq, else thin_pct, via
  report.sell_value(). entry_qliq (BL-27, display only until BL-32) is now the qliq that priced the buy.
- trades.csv note (every BUY and SELL, both books): impact_pct=<4 decimals|na> qliq=<whole USD|na>
  qliq_src=<entry|exit|none>, after quote=fallback when that is there. friction_pct stays the whole side (base + fee +
  impact). No new column, so trades.csv keeps its header.
- Worked numbers: at the floor (qliq 10,000) a 5 USD buy pays 3.05% (impact 0.05%); at qliq 40,000, 3.0125%; the old
  model charged 5% under 100k total liquidity whatever the real depth. A 5 USD round trip at 10,000 costs about 5.9%,
  at 10,000,000 about 5.8%.
- Gate B restart (d, go-message 3): core/report.py gate_restart(root, rs, desk_book, now), called right after the BL-31
  twin restart on every tick, runs once: when report state gate_restart != "BL-32" it writes one desk tasks.csv row
  stage=BOOK action=RESTART reason=gate_b detail "card=BL-32 since=<ts> prev_since=<old per_trade_since> books=desk,
  baseline refill=none", then sets per_trade_since = the tick ts and gate_restart = "BL-32" and saves the report state.
  gate_b.since and both books' per_trade.since follow per_trade_since. No refill: cash stays; Monday's weekly refill
  runs as built. The row is written before the state, so a crash between them repeats both (the last row matches the
  state) rather than moving the count without a row. On an empty state the BL-31 restart already starts the count
  now and also sets gate_restart, so there is no second row. Old closes stay in "closes" and the old report days.
- Exits (e): unchanged. OPEN ITEM for chat: EXIT liq_pull reads total liquidity (quote liq_usd <= entry_liq_usd x
  (1 - liq_drop_pct)), not qliq, so a pool whose real side drains while the token side holds the total up does not
  fire it (BL-28: total liq is mostly the token's own side). Changing it would be a new exit rule (non-negotiable 7:
  new positions only, one tested hypothesis).
- Display (f): no site change. report.GATE_KILLS gains real_depth (missing:qliq comes with DEX_FIELDS), so the machine
  cards count them as gate kills (they also carry goplus=skipped). The site's token table has its own copy of the list
  and classifies by reason alone, so until BL-43 real_depth and missing:qliq rows show under its Safety chip;
  tests/test_site.py pins exactly that gap. live.json values use the new friction; its keys are unchanged.
- Replay (tools/replay.py): Table 3 re-prices with core/friction.py (imported): entry at the BUY note's qliq (from BL-32)
  else qliq@entry from the logged rows; each exit at its own note qliq when qliq_src=exit, else the entry qliq. A
  sighting is now a desk VERIFY row in the logging set (past the gate as before, or a real_depth / missing:qliq row
  carrying qliq=), so labels and sightings stay aligned after the deploy. New Table 4 (read-only): sightings and
  distinct tokens by qliq at first sight, with sells_1h (s1) > 0 on that row, and how many the floor would cut.
- Replay 2026-10-31 .. 2026-11-06 (run 2026-11-06 ~19:00 UTC, data through the 18:48 tick): Tables 0-2 are identical
  to the pre-BL-32 code (1,453 sightings, 4,188 on-time labels). Table 4:

  | qliq at first sight | tokens | with sells_1h > 0 | chat | chat with sells | sightings | with sells |
  |---|---|---|---|---|---|---|
  | < 100 | 662 | 97 | 662 | 97 | 663 | 97 |
  | 100 - 1k | 94 | 73 | 94 | 73 | 94 | 73 |
  | 1k - 10k | 244 | 182 | 244 | 182 | 248 | 186 |
  | >= 10k | 436 | 114 | 436 | 114 | 448 | 126 |
  | na | 0 | 0 | - | - | 0 | 0 |
  | all | 1,436 | 466 | 1,436 | 466 | 1,453 | 482 |

  Chat's count is the distinct-token row, reproduced exactly. The 10k floor would cut 1,000 of 1,436 tokens (352 of
  the 466 with sells); 436 stay, 114 of them with sells in the hour.
  Table 3: 65 random-book trades (desk 0), logged -42.41 USD, re-priced -74.53 USD (the BL-31 step re-pricing gave
  -45.87). The gap is the 15 trades that entered at qliq < 500 (5 USD > 1% of the real side): -5.35 -> -39.32 USD
  (ATOF qliq 1, LOIRD 0, UnityCoded 3 x2, FFOT 5 and 9: near-total losses once impact is priced); the floor now blocks
  such entries. The 43 trades that entered at qliq >= 10k: -25.23 -> -23.71 USD (impact under 0.05%, and no 5% step).
- Smoke 2026-11-06 (scratch copy of data/, real requests, nothing under data/ changed): 16 ticks 18:54 - 19:28 UTC
  (9 back to back, then 7 at 5 min). Tick 1: 18 re-checks, 26 requests in 10.2 s; one row "desk,BOOK,,,RESTART,gate_b,
  card=BL-32 since=2026-11-06T18:54:32Z prev_since=2026-11-06T17:33:13Z books=desk,baseline refill=none", report state
  gate_restart "BL-32"; the twin's MONERO closed time_stop with note "impact_pct=0.0109 qliq=44596 qliq_src=exit".
  VERIFY rows of the first 9 ticks: 41 SKIP real_depth (12 with the full keys, 29 without), 5 WATCH with keys; of the
  18 re-checked tokens the real desk had logged earlier, the 4 without keys now were drained pools (liq 0-3 USD, read
  back from DexScreener), which the old gate would also have skipped as liquidity without keys. Example:
  "VERIFY,NOTF,...,SKIP,real_depth,first_seen=2026-11-06T15:18:14Z checks=11 goplus=skipped until=2026-11-07T18:54:32Z
  b1=12 s1=0 b24=43 s24=0 liq=67370 dex=uniswap:v4 qliq=2352 pair_age=219 token_age=219". 19:12: the twin picked
  MONERO again, SKIP reentry_cooldown (BL-31). 19:28: twin BUY XTruth Oil 0x30b045... usd=5.00, qliq 69,202, liq
  29,001,478, trades.csv friction_pct 3.0072 note "impact_pct=0.0072 qliq=69202 qliq_src=entry" (the old model: 3%).
  0 desk PASS. labels_calls per tick: real ticks 2026-11-05 max 1 (205 of 285 ticks 1, 80 ticks 0), 2026-11-06 to
  18:48 max 1 (161 / 55); smoke max 1 (7 of 16 ticks 1); budget max 2 kept. Requests per tick 3-26, max 11.8 s.
- Tests: tests/test_depth.py (25): config and DEX_FIELDS order; floor edges 9,999 / 10,000 / 0 / na (and 9,999.99,
  10,000.0) in dex_skip, universe_skip and hard_skip; total liq no longer a rule; order (missing fields first, real_depth
  before asset, sells and age); the real facts() at the edges with no GoPlus call below the floor, "na" = missing:qliq,
  no request added; the desk and the twin get the same universe_skip() from the real facts and its dex dict, one config
  number moves both; real_depth final for 24 h (due again after), a gate kill, a watched lead ends without GoPlus;
  ticket cap on qliq not liq; friction parts, entry, exit at this tick's qliq, the entry fallback, the no-qliq
  thin_pct fallback, the 100% cap (proceeds 0), report/live priced the same way, both books one function; the logging
  set on a fixed tick (old set + new token only, keys on exactly those rows, labels_calls <= 2), labels follow a
  real_depth skip, logged() never raises; Gate B restart once with its row and no refill, once across a reload, a day
  roll and a Monday (weekly refills as built, report since moved), old closes history only, fresh state no second row.
  Old tests updated: GOOD gains qliq 40,000; the 3/5% step and the 50k total floor replaced by real_depth and the new
  friction (round trips by hand, exits' E, liquidation value, live.json values, alerts at qliq 43,210, replay
  re-pricing, fresh_pairs() with half the liquidity on the ETH side); BL-27's "entry_qliq changes no decision" becomes
  "entry qliq moves fills, never a decision". 375 pass on 3.13 (348 before).

## BL-33
- R6 backtest tool, part 1: the tool only, no strategy verdict (the owner's Nov 6 decisions a-c and go-message 1-7,
  DECISIONS). Code: tools/candles.py (fetch), tools/backtest.py (simulator, split, lock, specs, twin, validation),
  .github/workflows/backtest.yml (by hand), tests/test_backtest.py. core/ never imports tools/ and never reads backtest/
  (test); tick.yml is unchanged. No change under core/ or data/.
- GeckoTerminal, measured from this sandbox on 2026-11-06 (the docs hosts apiguide.geckoterminal.com,
  www.geckoterminal.com and docs.coingecko.com are blocked by the sandbox proxy, so nothing here is from the docs):
  no rate-limit headers; a 429 says Retry-After 0. 1 call/s: 2 of 14 refused; 20/min: 12 of 30; 10/min: 3 of 20. So
  the card's ~30/min does not hold from a shared IP: PACE_S 7.5 s (8/min), up to 5 tries per call with 15 s between.
  The real fetches: 59 calls with 7 refused (retried), and 26 with 3. OHLCV: limit at most 1,000 ("Invalid limit.
  must be positive integer less than or equal to 1000"); only 5-minute windows with a trade come back (sparse); paged
  back with before_timestamp; currency=usd&token=<our token> gives our token's USD price. /tokens/multi takes 30
  tokens per call with include=top_pools; GeckoTerminal still lists pools DexScreener has dropped (6 of 10 sampled
  2026-11-02 tokens had no DexScreener pair any more; all 6 resolved on GeckoTerminal).
- Candles: sight -> +7 days (go-message 1), paging past 1,000 (the busiest sampled pool had 1,763 candles in 7 days: 2
  calls). Prices at 6 and volume at 4 significant digits (compact(): 2.4x smaller gzipped, far finer than any fill).
  Every logged day is fetched, holdout days included (go-message 2); only the simulator refuses holdout days.
  backtest/candles/<sight-day>.jsonl.gz is written once, when every window of the day has closed (last sight + 7 days);
  until then the workflow writes <sight-day>.open.jsonl.gz, replaced by later runs and deleted with the final file.
- Calls and storage per logged day, measured with the 7-day window (20 random 2026-10-31 tokens, windows nearly
  closed): 24 OHLCV calls for 20 tokens (1.2 per token) + 2 pool lookups; median 11 candles, max 1,763; 2.06 KB
  gzipped per token on average (max 30.6 KB). The twin's 49 tokens (deeper, busier pools): 3.4 KB each. At about 200
  logged tokens a day: ~240 OHLCV + ~7 batch + ~10-20 single pool lookups = ~260 calls, ~33 min at 8/min (~37 min
  with retries), ~0.4 MB a day = ~2.9 MB of git a week for the final files; .open files add ~0.4 MB per open day per
  run. The backlog Oct 31 - Nov 5 (1,271 day-sightings) is ~1,650 calls, ~3.5-4 h: the workflow's 340 min timeout
  may stop before the end; one commit per day keeps what finished. (Corrected in BL-33 part 2: a later Run workflow
  with days blank skips days that have a FINAL file but fetches every .open day again from the start, so after a
  timeout list only the missing days in "days".)
- Tokens = the logging set (verify.logged(), read back as tools/replay.py past_gate()): the first row per address on
  each UTC day. A token seen again on a later day gets its own window in that day's file.
- Pool match quality (go-message 4), per token in the candle file: exact (the position's pair in the state.json the
  buying tick committed, read from git history, or outcomes.csv sight_pair from 2026-11-06), single (GeckoTerminal
  lists one pool and its dex matches the logged dex), age (several pools, exactly one with the logged dex and a
  creation time within 10 min of the logged pair_age; pair_age is logged from 2026-11-05), dex (one pool with the dex
  among the top pools), guess (the deepest with the dex), other (no dex match, the deepest), none. QUALIFIED = exact,
  single, age: the only classes the BL-34 run uses (spec "match"). Counts: twin validation 62 of 62 exact (all 64 twin
  BUYs Oct 31 - Nov 5 found in the state history, 0.6 s, no lookup call); the 20-token sample: single 19, guess 1.
- Split and lock (c, go-message 2 and 7): logged days = complete UTC days from 2026-10-31 that have a tasks.csv;
  holdout = the last third rounded up (backtest.split()). On 2026-11-06: development Oct 31 - Nov 3 (4 days), holdout
  Nov 4 - 5 (2 days); the boundary moves forward as days are added until the first holdout run freezes it in
  backtest/holdout.lock.json (spec name, sha256 of the spec file, ran_at, split, result). A second holdout run is
  refused unless docs/DECISIONS.md has a line containing "holdout-rerun <spec name> <first 12 of the sha256>" and
  "(the owner)"; an allowed rerun is appended under "reruns", never replacing the first. Every run prints the split.
  Development runs and the validation load development days only and cut every row and candle at the first holdout
  day's 00:00 UTC; a position open there is pending. --holdout needs --write (the lock and the result go together).
- PROCESS RULE (the owner, go-message 2): the tool cannot stop anyone reading the raw logs of holdout days, so chat
  does not look at holdout-day outcomes (outcomes.csv labels, trades, tokens_24h.json of those days) for BL-34
  candidates before the BL-34 holdout run.
- Point in time (tools/backtest.py Logs / View): tasks.csv (desk VERIFY rows of the logging set, their trade keys) and
  outcomes.csv (labels), each row at its own ts; View(t) returns rows with ts <= t only (asserted). tokens_24h.json is
  not read (it keeps the latest day only). Test: facts at t are identical with and without every later row and label.
- Simulator rules (pessimistic): buy at the OPEN of the first candle starting at or after the decision and less than
  300 s after it, else no_fill (go-message 3: same rule for the strategy and the twin, counted per book). No candle =
  no trade = the pool price stays at the last close. Inside a candle the stops come first (hard stop, then the trail
  after a rung): an open at or below the stop exits at the open; a low at or below it exits at the WORSE of the stop
  and the candle's close (STOP_FILL "worse", Claude Code's choice from the validation, see below; "level" = the stop).
  A rung fills at its price when the high reaches it and no stop hit; one exit per candle. The trail uses the peak of
  earlier candles only. The time stop is checked at its due time on the last close, then at each later open (a time
  exit with no trade fills at the last close, go-message 7). liq_pull reads the logged total liquidity (VERIFY rows and
  labels) as of each candle; candles carry no liquidity, so most pulls are not seen. Optional exits.max_hold_h. No
  candles = no_data, data ending with the position open = pending; neither is ever a win.
- Costs: core/friction.py imported (CoreFees): base + launchpad fee + impact 100 x side USD / qliq; the buy at the qliq
  as of the decision, each sale at the latest logged qliq after entry up to the sale, else the entry qliq, else
  thin_pct. Launchpad is not in the logged rows, so a run uses "other" (no Clanker 1% fee: a known flattering gap, as
  BL-8's pool-shape launchpad). Fixed 5 USD ticket from the spec.
- Specs: a JSON file {name, text, ticket_usd, universe [[field, op, value]], entry [[...]], exits (core EXIT keys +
  optional max_hold_h), match}; sha256 of the file's bytes. A field missing as of t fails its condition. Decision points
  = the token's logged row times on decision days; the first point where universe and entry hold is the entry (one
  trade per token per run; no MAX_OPEN, cooldown or cash in the backtest: judged per trade). The twin: for the i-th
  spec trade at time t, a random token (random.Random(int(sha256[:12], 16) + i)) among the tokens whose facts at t pass
  the same universe and that have a qualified candle line, same exits and ticket, entered at t.
- Twin validation (the honesty check; development days Oct 31 - Nov 3; python -m tools.backtest --validate-twin):
  the twin's 62 logged lots bought on those days, the same exits (core/config.py EXIT, unchanged since BL-4) and ticket.
  Results: backtest/results/2026-11-06T212642Z-twin-validation/ (summary.json, trades.csv).

  | | trades |
  |---|---|
  | closed in both | 40 (same exit rule 29) |
  | no_fill (no trade within 5 min of the twin's tick) | 20 of 61 tried = 32.8% |
  | no_data (LOIRD: no candles at all) | 1 |
  | pending (Alsa: rungs taken, no logged liquidity pull, the pool's trades end Nov 1) | 1 |

  | 40 closed in both | P&L USD |
  |---|---|
  | logged | -28.53 |
  | simulated at the logged friction %, stop fill "worse" (default) | -35.37 |
  | simulated at the logged friction %, stop fill "level" | -16.28 |
  | simulated at core friction (BL-32) | -48.15 |

  Exit time: time stops land -3 / +2 min from the logged ones (the tick runs at :03/:08, candles start on the 5 min).
  The big gaps:
  - Liquidity pulls (I O F, USOE 22:13 and 23:13, NAFF 12:28, ADP 23:58, Paper): logged liq_pull at about -5.00;
    the simulator sees the crash inside one candle 3-8 min before the next tick and exits on the hard stop. At the
    stop LEVEL it books about -1.4 USD instead of -5.00 (USOE 22:20 candle: open 0.7297, low 0.00000018): a pull
    falls through the stop in one swap and nobody can sell at the level. With STOP_FILL "worse" (the close) the
    simulator books -4.9 to -5.0, like the logs. This is why "worse" is the default.
  - ATLAS 05:48 Nov 1: logged ladder_1 + ladder_2 + trail +4.82; simulated -1.36. The first candle after the tick
    opened 22% below the logged DexScreener quote and swung +62% / -31% inside 5 min: stop first -> hard stop in the
    entry candle. The live tick sampled once per 5 min and never saw the low. Pessimistic by design (-6.18 USD).
  - FFOT 04:28 Nov 2 and UnityCoded 18:58 / 20:58 Nov 2: logged time_stop, simulated hard_stop 43-48 min earlier on a
    wick below -25% inside a candle; at core friction -3.8 / -4.6 USD each because their entry qliq was 5 / 3 USD
    (impact on a near-empty real side; BL-32's floor now blocks such entries).
  - Noah: liq_pull both, the simulator's 62 min later (from a logged label's liquidity) and after its rungs:
    +4.14 vs +4.94. msUSD 07:28 Nov 2: a liq_pull from a logged liquidity drop, -0.29 vs -0.28.
  - no_fill: 20 twin buys on sleepy pools (wtCOIN x4, ADOF x2, ...) had no trade in the 5 min after the tick; the live
    twin filled at a DexScreener quote anyway. Their logged P&L was -0.27 to -0.48 each (time stops), plus IOF -5.00.
  Conclusion: on the same trades, the candle simulator with "worse" stops books MORE loss than the live logs (-35.37 vs
  -28.53 at the same friction), mostly from intra-candle wicks the 5-minute tick never saw; with "level" stops it
  would flatter rugs. The no_fill rate (33% here) has to be reported for every run.
- As-of fields for the BL-34 spec (go-message 6), for a token at 24-72 h of pool age (View.facts(), FIELDS):
  - From the sighting's VERIFY rows (pool age 15 min - 48 h at the row; re-checks only while watched, up to 3 h):
    qliq, liq (the chosen pair's total), b1 s1 b24 s24 (all Base pairs, BL-25), dex, symbol, verdict (the desk's last
    reason), sight_qliq, since_sight_h, last_qliq, last_liq. pool_age_h and token_age_h come from pair_age /
    token_age, logged only from 2026-11-05: for sightings before that the logs give NO pool age (a filter on it fails,
    a missing number). Only 11 of 290 tokens logged Nov 5-6 had a VERIFY row at pool age >= 24 h.
  - From the outcome labels of that sighting, each known only from its own ts (+1 h, +6 h, +24 h after first sight):
    l1_/l6_/l24_ price, chg_pct (vs the first price), liq, qliq, pair_b1 pair_s1 pair_b24 pair_s24 (the deepest pair
    only, BL-26), gone, and from the 2026-11-06 files best_qliq and dead; first_price. 1,262 of 1,298 sightings
    Oct 31 - Nov 5 have their +24 h label. A sighting at pool age p has its +24 h label at pool age p + 24 h, so a
    24-72 h entry sees at most the +24 h label.
  - NOT known at 24-72 h from the logs: any qliq, liquidity, price or buy/sell count after the +24 h label (no label at
    +48 h or +72 h; the scan rarely re-finds an old pool); holders, GoPlus fields after the sighting; the launchpad
    (rows do not log it). Known from the candles but NOT wired as spec fields yet: the price and volume path up to t
    (closes, highs, volumes of candles that started at least 5 min before t) and GeckoTerminal's pool_created_at (a
    pool age for sightings before 2026-11-05). Chat can ask for these as fields in part 2; a candle field would read
    only candles that ended before the decision.
- Tests: tests/test_backtest.py (35): each candle rule (entry at the first open at or after the decision, 299 s vs 300 s
  no_fill, no candles no_data, stop before rung in one candle, gap at the open, worse-of stop vs level, trail from
  earlier peaks only, time stop on the last close and later at an open, pending, data end, liq_pull, max_hold); core
  friction imported (the same object, no copy of its numbers, exit qliq sources exit / entry / none); the leak test
  (facts identical with and without later rows and labels, View never returns a later row, cutoff, last qliq after
  entry); split (last third rounded up, 3 of 7), a development run never trades or exits on a holdout day, unqualified
  pools are no_data; twin seeding (same sha same picks, other seeds other picks, same universe); the lock (first run
  allowed, second refused, a non-owner line refused, the owner line allows that exact spec only, the first lock entry
  kept, a frozen split never moves); --holdout without --write refused; spec sha256 of the file and bad fields
  refused; the fetch client paced and retried on 429 with no network; OHLCV paging past 1,000 and sorting; match
  classes; resolve exact first then one batch; day file .open until closed then final; core/ never imports tools/;
  backtest.yml by hand only, stages backtest/ only, never data/; the validation compares one trade and reads
  development days only. 410 pass on 3.13 (375 before).

## BL-33 part 2
- Pre-registration of BL-34 spec v1 plus the fields and rules its one run needs (the owner's Nov 6 decisions a-c and
  go-message 1-6, DECISIONS). No holdout run in this session. Code: tools/backtest.py, tools/candles.py,
  .github/workflows/backtest.yml, tests/test_bl34.py; nothing under core/ or data/.
- The spec (v1, SUPERSEDED the same evening by v2, see "BL-34 spec v2": the lock uses v2.json's sha256):
  backtest/specs/bl34-survivor-momentum-v1.md (the approved text, verbatim) and
  backtest/specs/bl34-survivor-momentum-v1.json (sha256
  a0533763e3c3122d17894db851999d3d9e3f84742bbaa72b8348c3b9101e4e1f, kept as written, never run). Committing both makes the pre-registration provable
  from git's timestamp. The JSON keeps every number of the text; "sell 1/3" is the string "1/3", read as an exact
  fraction (fractions.Fraction), not 0.33. Its notes hold the definitions below.
- Split (b): the spec carries its own split: development = complete logged days up to 2026-11-07, holdout = decision
  days 2026-11-08 .. 2026-11-14 (all after the approval). backtest.split(root, now, spec) uses it (a lock still wins;
  the last third stays the default for a spec without one). The holdout run is refused until all 7 holdout days are
  complete logged days, and runs ONCE (the lock), around 2026-11-17.
- PROCESS RULE (c, the owner): chat does not look at BL-34 candidates' outcomes on 2026-11-08 .. 2026-11-14 before the
  run (outcomes.csv labels, trades, tokens_24h.json of those days).
- Decision points (spec "decision": "l24"): the ts of each sighting's +24 h outcome label; one trade per token, its first
  +24 h decision decides.
- Universe (as of the decision; v1 had a verdict rule, dropped in v2): verdict (the sighting's last desk VERIFY reason)
  not_in [too_young, too_old] (v1 only); l24_gone == 0; l24_depth >= 10,000 with l24_depth = the +24 h label's best_qliq, else its qliq (best_qliq exists from
  the 2026-11-06 files); l24_pair_s24 >= 30; pool match exact, single or age (from the candle file's line for the
  token: pool identity only). New operators in / not_in; a value {"field": x} compares two fields.
- Entry: ret_6h_pct > 0; ret_6h_pct > eth_ret_6h_pct; eth_ret_24h_pct >= -3. Returns (go-message 2): price now =
  close of the last candle that ENDED at or before the decision; price then = close of the last candle that ended at
  or before decision - 6 h (- 24 h for eth_ret_24h_pct); 100 x (now / then - 1); either missing = the field is missing
  = not in. No trade in the last 6 h gives 0, which fails "> 0". A candle ending exactly at the decision counts; one
  ending after it is never read (backtest.candle_return(), tested at both edges and with a gap longer than 6 h).
- ETH series (3): tools/candles.py --eth writes backtest/candles/eth-<day>.jsonl.gz for every complete day from
  2026-10-30 without one (written once): the day's 5-minute candles of the deepest WETH/USDC pool on Base (GeckoTerminal
  /tokens/WETH/pools, the WETH + USDC pool with the highest reserve; 0x6c561b44... on 2026-11-06), priced in USD
  (token=WETH). One call per day plus one pool lookup: 9 calls for Oct 30 - Nov 5 (1 refused, retried) and 2 for Nov 6,
  3.8-4.8 KB per day, 251-284 of 288 candles per day (some 5-minute windows have no trade even there). backtest.yml runs --eth first
  (one "backtest: candles eth" commit), then the token days. EthSeries never reads a candle ending after the cutoff.
- Exits: time_stop_min null = no time stop (and liq_drop_pct null = no liquidity-pull exit, not in the spec);
  max_hold_h 72 exits at the last close at entry + 72 h (or at a candle open at or after it); ladder 1/3 at +40% and
  1/3 at +100% of the original quantity; trail 25% from the peak of earlier candles after the first rung; hard stop
  -25%, a stop inside a candle at the worse of stop and close (stop_fill "worse").
- Friction (go-message 3): SpecFees = core/friction.py side_pct() with launchpad "other" (base 3% + impact 100 x side
  USD / depth, no launchpad fee) + fee_pct 1 on every side; depth = the token's l24_depth on both sides; the sell side is
  capped at 100%. SpecFees refuses (ValueError) a launchpad with a pool fee together with fee_pct, so the Clanker 1%
  can never stack with it (test).
- Twin (go-message 4): at each strategy entry time t, the pool = tokens whose +24 h decision fell in (t - 6 h, t],
  whose universe facts as of t hold (universe rules only) and whose candle line has a qualified pool. Membership reads
  no candle at all (tested: the pool is identical with every candle after t removed, and with no candles), which fixes
  the part 1 filter that kept only tokens with candles anywhere in the 7-day window (future data). Pick =
  random.Random(int(sha256[:12], 16) + i) over the sorted pool, entered at t through the same simulate() (same no_fill
  rule) with the same exits, ticket and SpecFees at its own l24_depth; an empty pool = twin_skip (counted).
- Hold-ETH (go-message 1): per closed trade, ETH's price at the entry fill time and at the exit fill time (the close of
  the last ETH candle ended at or before each), return = exit / entry - 1, no friction. Pass test 4 compares the mean of
  trade net P&L / 5 USD with the mean hold-ETH return over the same trades.
- Pass tests (holdout only, backtest.pass_tests()): 1 closed >= 20; 2 mean net per trade > 0; 3 > the twin's mean net per
  closed twin trade; 4 mean return > mean hold-ETH; 5 max drawdown of 50 USD + cumulative net P&L ordered by exit time
  < 30% (drawdown = (peak - equity) / peak). Verdict: inconclusive when 1 fails; retired when 2, 3 or 4 fails; pass when
  all five hold; "not passed" when only 5 fails (the spec does not name that case). Always reported on the holdout run,
  never the verdict: no_fill per book, pending, twin_skip, the mean net per trade with stop fill "level", and with
  tick-like close-only exits (every candle after the entry candle reduced to its close).
- Development run (go-message 5): python -m tools.backtest --spec backtest/specs/bl34-survivor-momentum-v1.json prints
  counts only and writes nothing, even with --write: decisions, the log-rule universe, why a universe token has no pool
  (sight day without a candle file yet / token not in the fetched file / match not qualified), the universe, missing
  token or ETH returns, entries, per book n / closed / pending / no_fill / no_data / twin_skip, ETH candles per day and
  the pool-age check. No P&L, no win rate, no pass tests (tested: the output matches none of net, pnl, win, verdict,
  PASS, FAIL or USD, and no file appears).
- Pool age check (5, a check, never a rule): GeckoTerminal pool_created_at is now recorded for exact pools too (the
  exact tokens ride in the same /tokens/multi batches, no extra call); the run counts entries of both books inside /
  outside 24-72 h of pool age at the decision, and unknown (no created time on the line).
- Development plumbing run, 2026-11-07 ~00:40 UTC (counts only; development days so far Oct 31 - Nov 6; token candle
  days fetched yet: 2026-10-31.open and 2026-11-01.open from backtest.yml run 1; ETH Oct 30 - Nov 6):

  | | count |
  |---|---|
  | decisions (+24 h labels on development days) | 1,371 |
  | pass the log rules (verdict, alive, depth >= 10k, pair_s24 >= 30) | 81 |
  | of those, no pool yet because the sight day has no candle file | 61 |
  | of those, pool match not qualified | 1 |
  | in the universe | 19 |
  | without a token 6 h return / without ETH returns | 0 / 0 |
  | entries (BL-34) | 6: closed 2, pending 0, no_fill 4, no_data 0 |
  | twin | 6: closed 3, pending 0, no_fill 3, twin_skip 0 |
  | ETH candles per day (of 288) | Oct 30 273, Oct 31 251, Nov 1 272, Nov 2 284, Nov 3 278, Nov 4 284, Nov 5 283, Nov 6 274 |
  | pool age at the decision (both books' entries) | inside 24-72 h 1, outside 6, unknown 5 |

  81 log-rule survivors in 7 days (~12 a day) matches chat's ~10 a day. The pool-age check: the 5 unknown are exact pools
  from the state history fetched by the part 1 code (no created time; the part 2 batch records it from the next fetch);
  the 6 outside are LSTEAK (GeckoTerminal pool created 2026-04-06, ~5,014 h at the decision, both books), RSGP (created
  2026-10-28, ~100 h, both books, the twin twice) and XDP (~843 h, twin); the 1 inside is ADIN (24.6 h). On the whole
  2026-10-31 file the "single" pools were created 0-15 min before the sighting for 152 tokens, 15 min - 48 h for 24 and
  > 48 h for 4 (of 180): the identities look right, the logging set simply includes very fresh pools and a few old ones.
  The frozen spec has no pool-age rule ("pool age 24.25-72 h" in its decision line is descriptive), so old pools can
  enter BL-34: an item for chat, not changed here. backtest.yml run 1: Oct 31 = 198 tokens (exact 17, single 180,
  guess 1; 191 with candles), 277 calls with 66 refused (24% from the Actions IP), ~44 min, 195 KB (.open); at that pace
  6 days take ~4.4 h, inside the 340 min timeout. eth-2026-11-06 was fetched in the session (2 calls) once Nov 6 was
  complete.
- Tests: tests/test_bl34.py (17): the JSON encodes the frozen text (each number, "1/3" exact, split, conditions, sha256
  of the file); the spec's split vs the default and the incomplete-holdout refusal; candle_return edges (a candle ending
  exactly at t, one after t never read, a gap over 6 h gives 0, missing then); in / not_in / field references; SpecFees
  = side_pct("other") + 1 on both sides and refuses Clanker with fee_pct; time stop off with max hold 72 h and exact
  1/3 rungs; pass verdicts (pass, inconclusive, retired x3, not passed on the drawdown) and the drawdown arithmetic;
  a decision run on a small world (decision at the +24 h label, universe / entry, the twin from universe rules only);
  the twin pool identical without candles after t and without any candle, the (t - 6 h, t] edges; twin_skip counted and
  both books through the same simulate() at t; the entry fields identical without later token and ETH candles;
  l24_depth best_qliq first; SpecFees in a real trade; the CLI plumbing prints no P&L, writes nothing, reads no
  holdout day; the holdout refused until its days are complete, then inconclusive with 0 closed; the ETH pool pick
  and one file per complete day; the exact pool's created time from the batch; backtest.yml runs --eth and says open
  days are fetched again. One part 1 test updated (the exact token now rides in the batch). 427 pass on 3.13 (410
  before).

## BL-34 spec v2
- The owner's decision 2026-11-06 ~19:40 Toronto (options A and A, DECISIONS): spec v2 replaces v1. The gap came from
  the development pool-age check above (LSTEAK, RSGP, XDP on pools far older than 72 h), before any holdout day existed
  and with no P&L seen. v1's "pool age 24.25-72 h" line was wrong: the desk verdict is only a sighting's LAST reason, so
  old pools whose last reason was not too_old entered. v2: no pool-age rule (any pool age) and no verdict rule; "only 5
  fails" = "not passed: drawdown" (forward paper vs twin, no money); results by pool age at the decision reported on
  the holdout run only, never the verdict. Every other number is v1's (test: decision, ticket, match, entry, exits, stop
  fill, friction, twin, hold-ETH, pass, split identical; the universe = v1's without the verdict rule).
- Files: backtest/specs/bl34-survivor-momentum-v2.md (the owner's text, verbatim) and
  bl34-survivor-momentum-v2.json, sha256 e9305116d1230e36319c54dd6cc62448e0d605205e39738d1e751d1a7b6f2e30 (the holdout lock's hash). v1's .md and .json stay as written
  (superseded, never run; v1 sha256 a0533763e3c3... checked unchanged by a test).
- Pool-age report (tools/backtest.py age_report(), holdout only; spec "report_pool_age"): per book and bucket by
  GeckoTerminal pool_created_at to the decision: 24-72 h (any age up to 72 h; pools under 24 h, i.e. created after the
  sighting, are counted in it and shown as "under 24 h"), 72 h-30 d (30 d = 720 h), > 30 d, unknown (no created time):
  n, closed, mean net per closed trade. A development run never computes or prints it.
- Tests (+2, 429 pass): v2.json encodes the v2 text (no verdict or pool-age rule, the buckets, "only 5 fails"); v2 keeps
  every other v1 number and v1 is untouched; the "not passed: drawdown" verdict; the age buckets (24-72 h with one under
  24 h, 72 h-30 d, > 30 d, unknown) and no breakdown on a development run; the fixture world runs on v2 (the too_old
  token now enters, the twin pool grows to three).

## BL-43
- Display only (the owner's mock approval 2026-11-07 and the Nov 7 answers 1-6, DECISIONS). No mock file in the repo:
  built from the card text and the hard rules; the before/after screenshots are the proof. Code: site/index.html,
  core/alerts.py (daily summary only), tools/candles.py (write_status(), --status), tools/backtest.py (the lock's "pass"
  key), .github/workflows/backtest.yml (stages backtest/status.json), tests/test_bl43.py, tests/test_site.py,
  tests/test_alerts.py. No rule, threshold, book, exit, report.json, live.json or tick-path change; no new request on the
  tick path. Nothing under data/ changed.
- Hard rules kept (the owner): REMOVE NOTHING (every card, its order, layout, icons, colours, fonts and the logo header
  stay); ZERO changes to the tick.log terminal and the Paper equity card. tests/test_bl43.py pins sha256 of seven
  segments as of 466419d (the terminal and equity markup, terminal()/termFoot(), curve(), the put('equity', ...) block,
  the Terminal and Equity CSS), the section order (new cards last in their column), the chips and the funnel subtitles.
  The rendered terminal and equity outerHTML and their PNG screenshots are byte-identical before and after (phone 390 px
  and desktop 1280 px, light and dark, fixed clock 2026-11-07 15:31 UTC, the same frozen data files).
- Changes in the page:
  1. Desk vs random: a "Refills" row under "Net after fees" (per book refills_usd; "Net after fees" keeps its label and
     its number, net_pnl_usd, already net of refills since BL-31; no duplicate row, the owner). After the existing verdict
     line: "Per trade since <gate_b.since>" per book (per_trade n closes, net_per_trade_usd / net_per_trade_pct, "—" when
     n = 0), "Desk lead, net of refills" = desk_lead_usd, and an amber note per book with refills_usd > 0: "The twin's
     <value_usd> includes a +<refills_usd> top-up. Top-ups are never profit. Next one: Mon 00:03 UTC." ("The desk's" for
     the desk).
  2. Closes toward Gate B: header "both books" (was "desk only"); the big number stays gate_b.desk_closes / target, a
     "random N closes" (gate_b.baseline_closes) on the right of the same row, and a caption "Desk n / 100 · random m ·
     both since <gate_b.since> · Kill at 200 closes". Track and scale unchanged.
  3. Labels: real_depth = "Real depth under 10k", missing:qliq = "No real-depth number", only where the page already used
     its label map (Why they died, Kill log). The token table keeps raw reason codes and its search (the owner).
  4. Tokens: GATE = core/report.py GATE_KILLS exactly (real_depth, missing:qliq added; a test fails if they differ),
     plus the prefix rule asset: (core/report.py kill_stage() counts asset:<kind> as a gate kill via goplus=skipped; no
     asset kind is in GATE_KILLS). New chip "Gate kills" before "Safety kills" (SKIP and a gate reason); Safety kills =
     SKIP and not a gate reason. Real Nov 6 tokens_24h.json: All 1,046, Gate kills 991, Safety kills 53, Skipped 1,044.
     Known gap: the table classifies by reason alone (tokens_24h.json has no detail), so a too_young / too_old /
     nobody_can_sell skip that carried goplus=skipped counts as gate on the machine cards and as safety in the table
     (none in the Nov 6 kills_since: too_old 4 and nobody_can_sell 4 were all safety).
  5. Kill funnel, Gate subtitle: "Real depth ≥ 10k, DexScreener data present". Nothing else in the funnel.
  6. New card, end of the left column: "BL-34 survivor momentum". Tag SEALED while backtest/holdout.lock.json answers
     HTTP 404; a one-line description from the spec's numbers (real depth ≥ $10k, ≥ 30 sells in 24 h); test week =
     split.holdout_days (Nov 8 – Nov 14, "starts Nov 8" / "day k of 7" / "complete"); verdict ~ last holdout day +
     VERDICT_AFTER_D (3) = ~Nov 17; days left to that date (10 on Nov 7). The 5 pass tests (names from the spec's pass
     numbers) show "waiting". Once the lock exists: tag RUN ONCE, verdict = lock pass.verdict, run time = lock ran_at,
     each test pass / fail from lock pass.tests. The card reads the lock only through LK() and only pass, pass.verdict,
     pass.tests.*, ran_at and spec: never trades, P&L, result or by_pool_age (test). A lock fetch that fails (not 404)
     shows "unknown", never "sealed". A lock without "pass" says so.
  7. New card, end of the right column: "Backtest lab" from backtest/status.json: token-candle days (count and range),
     final / still open, ETH-candle days; honesty check = the latest twin-validation run: trades closed in both, live
     logged vs simulated on candles (logged friction, stop fill "worse"), no fill in 5 min of tried; "Harsher than live,
     so it doesn't flatter." when simulated <= logged, else "Kinder than live: read backtest results with care." Today:
     6 token days (Oct 31 – Nov 5, all .open), 8 ETH days (Oct 30 – Nov 6), 40 closed in both, -$28.53 vs -$35.37, 20 of 61.
  Footer line and the static foot now also name backtest/ (spec, lock, status.json).
- Fetches: six raw.githubusercontent.com URLs, the three data files as before plus SPEC_URL
  (backtest/specs/bl34-survivor-momentum-v2.json), LOCK_URL (backtest/holdout.lock.json, 404 = sealed) and STATUS_URL
  (backtest/status.json), each once at page load through the same load() (cache no-cache, no override); only live.json
  is re-fetched. The browser logs its own 404 line for the lock until the run; no script error.
- backtest/status.json (tools/candles.py write_status(), the owner's answer 6): {as_of, card, token_days [{day, state
  final|open, tokens, with_candles, fetched_at}], eth_days, twin_validation {run, path, days, trades, closed_both,
  logged_usd, simulated_usd, simulated_level_usd, simulated_core_usd, no_fill, no_data, pending, tried}}. Written at the
  end of every tools.candles run but --list-days (in a finally, so also after a failed fetch) and by
  python -m tools.candles --status (no request). as_of = the newest fetched_at among the candle files, so the file
  changes only when a candle file does (no empty backtest commits). A final file wins over a leftover .open of the same
  day. The twin-validation numbers come from that run's trades.csv (its summary.json holds only mode, ran_at and split):
  the 40 closed rows reproduce NOTES "BL-33" (-28.53 / -35.37 / -16.28 level / -48.15 core). backtest.yml stages it with
  the candles (git add -A backtest/candles/ backtest/status.json; the outside-backtest/ guard unchanged). Never under
  data/; core/ and the tick never read it. First copy written in this session.
- The lock's "pass" key (Claude Code's choice, needed for item 6): write_lock() stored only res["summary"] (per-book
  counts), not the pass tests or the verdict, so the site could not read them from the lock. write_lock(..., passed)
  now adds "pass" = pass_tests() (tests, verdict and its means) beside "result", also on an allowed rerun; null for a spec
  without pass tests. It changes no number of the run, the spec, its sha256 or the split (test: two locks differ only in
  "pass"). The site shows only the tests and the verdict.
- Telegram daily summary (core/alerts.py; the owner: no BL-34 line, core/ never reads backtest/): every BL-3 line stays;
  new lines: after the values line "random refilled +$46.81 (not P&L)" (per book, joined by " · ", only when that day's
  top-up rounds above 0), after "Gate B n/100 desk closes" the line "Gate B since <since> · random N closes · kill at
  200", then "desk per trade since <since>: n closes · +$x (+y%) each" and the same for random ("0 closes · —" at n 0,
  "na" without per_trade). The refill is the change in refills_usd between the report day's equity entry and the one
  before it in state.json report.equity (entries before BL-31 have none = 0); report.json's equity_daily has no
  refills_usd, so the state is the source (no report change). Unreadable state: no refill line. Plain text, no
  parse_mode. Real Nov 6 report: "random refilled +$46.81 (not P&L)" (the BL-31 restart). Test: no core string literal
  (docstrings aside) names backtest and no core module imports tools.
- Tests: tests/test_bl43.py (27): the seven pins, section order, the six chips (exact markup and order: hiding, reordering, adding or removing one fails), Desk vs random rows (Refills under Net after
  fees, no "Net after refills"), funnel subtitles; the label map and labels only in Why/Kill log, raw codes and search in
  the table; asset:<kind> = gate in core and on the page; Gate/Safety chip filters; status.json shape, final over .open,
  stable as_of, empty root, --status writes only status.json, the repo copy equals the NOTES numbers, every ST/SD key the
  page reads exists, backtest.yml stages it; spec keys and the ~Nov 17 constant; lock keys and the page's test ids =
  pass_tests() keys; the card never reads trades or P&L; the "pass" key changes no number; core never reads backtest; the
  summary with and without refills, BL-3 lines kept, no BL-34 line, refilled_on() from equity entries (BL-31 gap, a
  later weekly refill, a missing day, a broken state), per-trade formats. tests/test_site.py: six URLs, seven load(,
  GATE == GATE_KILLS (the BL-32 lag set removed). tests/test_alerts.py: test_daily gains the new lines, the closes line
  found by its prefix, the tick test passes refilled_on(). Mutations caught: dropping real_depth from GATE, a terminal
  text, the equity dash pattern, a curve CSS width, renaming a card, unhiding the run pill, reading a P&L key, hiding or renaming a chip. 456 pass
  on 3.13 (429 before).
- Screenshots (Playwright Chromium, page.route serving frozen copies of data/report.json, tokens_24h.json, live.json as of
  the 15:28 UTC tick and the repo's backtest files; clock fixed): before and after full pages at 390 and 1280 px, light
  and dark; the BL-34 card sealed, with a pass lock and a retired lock fixture (scratch, never committed); no horizontal
  scroll at either width; requests = the page + the six URLs.

## BL-44
- WATCH replay, evidence only (the owner, Nov 7): docs only, no rule, threshold, config, code, workflow or data change.
  Question, after the twin's Baranos and KDX marks on Nov 7 (both bought while the desk WATCHed them, GoPlus silent): how
  does the whole group of desk WATCH goplus_not_ready sightings that pass the real-depth floor and have sells do, rugs
  included, next to the twin's universe; and where GoPlus did answer, how many were honeypots or high-tax?
- Window (the owner): sightings with first_seen 2026-10-31 .. 2026-11-06 UTC ONLY. Nov 7 sightings decide on Nov 8,
  inside BL-34's sealed holdout (Nov 8-14), so nothing from Nov 7 on is read. Proof: the script (session scratchpad, not
  committed) opens only data/daily/2026-10-31 .. 2026-11-06 tasks.csv, trades.csv and outcomes.csv (20 files, 28,251
  rows) and asserts every row's ts < 2026-11-07T00:00:00Z; the latest ts read is 2026-11-06T23:58:14Z. No live.json, no
  state.json, no backtest/candles (their windows reach into Nov 8-14), no request. A consequence: outcome labels are
  filed by the day they land, so Nov 6 sightings have no +24 h label and late Nov 6 sightings no +6 h label (n per
  horizon below).
- Sets (unit = a sighting, address + first_seen; each set takes the first qualifying desk VERIFY row of the sighting,
  read from its BL-25/28/30 keys):
  - Group: action WATCH, reason goplus_not_ready, qliq >= 10,000 (the BL-32 floor, applied back to Oct 31), s1 > 0.
    Any GoPlus field may be pending (pending at the row: honeypot 212, top10_pct 189, buy_tax 182, lp_locked 179,
    sell_tax 176, dev 63, mint / freeze / risk flags 49 each). 92 of 223 at their first check. No age or asset filter:
    age at the row ok 56 / outside 15 min - 48 h 11 / unknown 156 (pair_age is logged from Nov 5); asset kind wrapped 5,
    stable 4 (BL-31 would cut them today).
  - Twin's universe: any desk check (any verdict) with qliq >= 10,000, s1 > 0, age not outside (unknown = pass, as the
    twin's universe_skip() cannot see it before Nov 5 either) and no asset kind (core/verify.py asset_kind() on the symbol
    and the first-sight price). 210 of its 230 sightings are group sightings, so it is mostly the same population.
  - Before the BL-32 deploy (2026-11-06 ~20:33 UTC) only leads with total liq >= 50k were logged with keys, so earlier
    days hold the liq >= 50k part of today's universe only.
- Outcomes (BL-26 outcomes.csv, chg_pct = label price vs first-sight price, no friction; dead = the dead column from the
  2026-11-06 files, else gone or label qliq < 100 as BL-30's approximation; gone = no pair):

| set | horizon | n | chg median | chg mean | mean without the top 2 | up | best | dead | gone |
|---|---|---|---|---|---|---|---|---|---|
| group (223 sightings, 218 tokens) | +1 h | 221 | +0.2% | +0.5% | -4.5% | 125 | +686% | 15% | 0% |
| | +6 h | 221 | -100% | -30.8% | -42.5% | 60 | +1,336% | 52% | 0% |
| | +24 h | 181 | -100% | +0.0% | -24.7% | 36 | +2,413% | 59% | 1% |
| twin's universe (230, 223 tokens) | +1 h | 228 | +0.2% | +1.8% | -3.1% | 131 | +686% | 14% | 0% |
| | +6 h | 228 | -100% | -28.7% | -40.0% | 62 | +1,336% | 49% | 0% |
| | +24 h | 193 | -100% | +0.6% | -22.5% | 42 | +2,413% | 55% | 1% |
| twin's universe minus the group (20, 16 tokens) | +1 h | 20 | +0.1% | +7.9% | -0.3% | 11 | +153% | 0% | 0% |
| | +6 h | 20 | -0.2% | +2.1% | -6.5% | 7 | +153% | 5% | 0% |
| | +24 h | 20 | +0.4% | +1.1% | -8.1% | 10 | +153% | 5% | 0% |

  Sightings by day (group): Oct 31 22, Nov 1 15, Nov 2 15, Nov 3 28, Nov 4 41, Nov 5 62, Nov 6 40. dead was derived (old
  files without the column) on 177 / 163 / 136 of the group's 221 / 221 / 181 labels.
- How the desk's watch ended (group, last desk row of the sighting): SKIP liquidity 121 (the pool was pulled while the
  desk waited; the pre-BL-32 gate name), SKIP goplus_timeout 102.
- Where GoPlus did answer: nowhere. In the group: 0 later SKIP honeypot / buy_tax_pct / sell_tax_pct (or any other safety
  skip), 0 goplus_ready_at, 0 PASS. In the twin's universe: 0 honeypot / tax skips, 0 goplus_ready_at, 0 PASS. Over every
  desk VERIFY row in the window: 0 SKIP honeypot / buy_tax_pct / sell_tax_pct. So the logs cannot say how many were
  honeypots or high-tax: GoPlus never gave that answer for any of them (as in BL-24).
- The twin (paper, exits as built), rows with ts < Nov 7 only: 42 of its 67 buys in the window went into 30 group
  tokens; realised SELL P&L on them -25.11 USD by 2026-11-06 23:58 UTC. (Baranos and KDX are Nov 7 sightings, outside
  the window.)
- Reading: the group is a lottery with a fat left side. The median sighting is dead (-100%) by +6 h and stays dead; the
  mean is near 0 at +24 h only because of a few very large winners (best +2,413%), and without the top 2 it is -25% (and
  -43% at +6 h). The twin's universe looks the same because it is mostly this group; the 20 twin-universe sightings
  outside the group were calmer (5% dead at +24 h) but are too few to read. No rule proposal.
- Hypothesis lines for chat (not tested, not proposed as rules):
  - H1: within the group, the rugs are concentrated in the copycat "fund" family (total liq ~29M USD almost all on the
    token side, price ~0.0197, ~80 sells an hour: IOF, NAFF, NOTF, DTCC...); a pre-registered filter on that shape would
    cut most dead sightings. Test only as a frozen spec with its own holdout.
  - H2: the twin's recent lead over the desk comes from the right tail of this same group, not from a better universe;
    over enough closes the tail should not cover the dead majority after friction (the twin's -25.11 USD on group tokens
    in the window points that way).
- Caveats: chg_pct is the price move from first sight with no friction and no exit rules (the twin's trades are the
  with-exits view); the group's first qualifying row is often later than first sight; dead is approximated on old files;
  pool age is unknown before Nov 5; ~7 days, one market, many copycat launches from a few deployers.

## BL-39 survey
- BL-39 part 1 (also BL-38: same universe), evidence only (the owner, Nov 9 choices and go-message 1-4, DECISIONS):
  counts and coverage of established, liquid Base tokens, NO returns of any kind (no P&L, no price change, no forward
  return by MAX or reversal bucket, no backtest), so the BL-38/39 spec can still be written blind. Code:
  tools/liquid.py (by hand), tests/test_liquid.py; files backtest/liquid/universe.json (116 KB) and daily.jsonl.gz
  (1.1 MB, committed by the owner's choice A). Nothing under core/ or data/ changed; core/ never reads backtest/.
- CUT = 2026-11-08T00:00:00Z (protects BL-34's sealed holdout, Nov 8-14). Daily candles asked with
  before_timestamp=CUT; GeckoTerminal also returns the candle STARTING at CUT, so every candle whose day does not end
  by CUT (t + 1 d > CUT) is dropped on arrival (keep()); write() refuses any other. Largest stored candle start
  2026-11-07T00:00:00Z (its day ends at CUT). The list answers carry today's prices and volumes: dropped unread; only
  pool identity, pool_created_at and reserve_in_usd (at fetch, the owner's field list) are kept. The one price read is
  the last daily close before CUT, as core/verify.py asset_kind()'s price input (last_close()). From data/ only the
  address column of data/daily/2026-10-23 .. 2026-11-07 tasks.csv was read (the launch-universe drop set); no
  data/daily file from Nov 8 on, no live.json / state.json / report.json, no BL-34 token candle file.
- Run: 2026-11-09 14:18-15:50 UTC (91 min), 613 calls (116 refused by the rate limit, all retried; 0 errors) through
  tools/candles.py Client (7.5 s pace) under a 1,200-call cap: 147 list + token calls, 465 daily-candle calls (one per
  established pool, 370, plus retries), 1 history check.
- Set, endpoints and counts:
  - Pools: /networks/base/pools sorted by h24_volume_usd_desc and by h24_tx_count_desc, then
    /networks/base/dexes/{dex}/pools (h24_volume_usd_desc) for the 8 dexes most frequent in those two lists
    (aero-slipstream-base, uniswap-v4-base, uniswap-v3-base, aerodrome-slipstream-3, pancakeswap-v3-base,
    aerodrome-slipstream, aerodrome-base, uniswap-v2-base); 10 pages x 20 each, at most 200 pools a list.
  - Tokens = both sides of every listed pool; /networks/base/tokens/multi (30 per call, include=top_pools) gives each
    token's deepest pool (highest reserve among its top pools and the listed pools) and its base / quote pair counts.
  - Established = the deepest pool created before 2026-10-09 (>= 30 days at CUT).

  | step | n |
  |---|---|
  | pools listed | 1,630 |
  | tokens on them | 901 |
  | launch-universe tokens dropped (address in tasks.csv Oct 23 - Nov 7) | 129 |
  | tokens resolved (deepest pool found) | 772 |
  | asset excluded (asset_kind, imported): stable / wrapped / quote | 33 / 23 / 19 (15 of the wrapped by the last close >= 100 USD) |
  | young (deepest pool created on or after 2026-10-09): count only, no candles | 342 |
  | established = the survey | 355 |

  The survey's deepest pools by dex: uniswap-v3 117, aerodrome 72, aerodrome-slipstream(-2/-3) 77, uniswap-v2 32,
  uniswap-v4 26, pancakeswap-v3 15, other 16. Every one of the 355 has candles.

- The history cap (the owner's go-message 3, measured): one daily-OHLCV answer holds at most 184 candles (limit=1000 is
  not honoured), and a request ending 200 days before CUT on the oldest daily-trading pool (KRAV 0x65488e..., 1 call)
  came back refused or empty ("api_limit"), so no paging back. Pools that trade every day therefore have candles from
  2026-05-09 only (window_start: the latest first candle among the pools created > 200 days before CUT with no missing
  day); thin pools reach further back in calendar time with gaps (89 tokens have a candle older than 184 days, the
  oldest 2025-06-02). So ">= 365" below is 0 by the API limit, not because the tokens are young: 197 of the 355 pools
  are >= 365 days old at CUT.
- Tokens by size (n = 355):

  | | >= 100k USD | >= 1M USD | >= 10M USD |
  |---|---|---|---|
  | reserve at fetch (Nov 9, total both sides) | 172 | 19 | 1 |
  | median daily volume, the 30 days before CUT (Oct 9 - Nov 7, a no-trade day = 0) | 39 | 4 | 1 |

  Median reserve 83k USD. Median daily volume >= 10k: 138, >= 1k: 215, = 0: 24. Both median volume >= 100k/day and
  reserve >= 1M: 6 (reserve >= 100k: 38).
- Days of history per token (n = 355):

  | | >= 30 | >= 90 | >= 180 | >= 365 |
  |---|---|---|---|---|
  | daily candles held (days with a trade) | 347 | 313 | 282 | 0 (API limit) |
  | calendar span, first candle to CUT | 355 | 317 | 293 | 13 (thin pools, with gaps) |

  Max 184 candles; max span 524 days.
- Data gaps (n = 355): GeckoTerminal returns a daily candle only for a day with a trade, so a missing day = no trade
  (or no data); zero-volume candles: 0. Missing days between a token's first candle and CUT: 7,704 in all; tokens with
  0 missing 217, 1-5 49, 6-30 42, > 30 47. Last candle more than 6 days before Nov 7: 3 tokens.
- Point-in-time universe per week (as of each Monday 00:00 UTC; trailing 7 days [Monday - 7 d, Monday) as mean
  USD/day of the deepest pool's volume; universe = a trade in those 7 days). Only full weeks inside the candle window:
  week -26 (as of May 11, trailing May 4-10) is partial (window from May 9) and dropped, so the table starts at the
  earliest full week, May 18: 25 weeks. BL-39 group (the owner): tokens with all 31 daily closes in the 31 days
  before the Monday (the trailing 30 days, BL-39's draft MAX lookback), then 10% rounded up; MAX itself is never
  computed; "—" where that lookback starts before the window (May 18 - Jun 8). BL-38 bucket: the bottom 10% (rounded
  up) of the universe by trailing 7-day volume, its median (USD/day) and how many clear 10k / 100k USD/day.

  | as of | universe | >= 100k/d | >= 1M/d | >= 10M/d | BL-39 eligible | BL-39 group | eligible, >= 100k/d | group, >= 100k/d | BL-38 bucket n | its median USD/d | >= 10k/d | >= 100k/d |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | May 18 | 290 | 57 | 10 | 0 | — | — | — | — | 29 | 3.81 | 0 | 0 |
  | May 25 | 290 | 57 | 9 | 1 | — | — | — | — | 29 | 2.61 | 0 | 0 |
  | Jun 1 | 295 | 47 | 11 | 1 | — | — | — | — | 30 | 1.63 | 0 | 0 |
  | Jun 8 | 300 | 45 | 10 | 1 | — | — | — | — | 30 | 1.26 | 0 | 0 |
  | Jun 15 | 296 | 43 | 7 | 1 | 227 | 23 | 38 | 4 | 30 | 5.77 | 0 | 0 |
  | Jun 22 | 299 | 47 | 7 | 0 | 228 | 23 | 43 | 5 | 30 | 3.38 | 0 | 0 |
  | Jun 29 | 301 | 45 | 4 | 0 | 231 | 24 | 44 | 5 | 31 | 3.15 | 0 | 0 |
  | Jul 6 | 303 | 40 | 5 | 0 | 238 | 24 | 39 | 4 | 31 | 4.36 | 0 | 0 |
  | Jul 13 | 305 | 45 | 4 | 0 | 235 | 24 | 44 | 5 | 31 | 3.93 | 0 | 0 |
  | Jul 20 | 309 | 48 | 2 | 0 | 234 | 24 | 48 | 5 | 31 | 2.69 | 0 | 0 |
  | Jul 27 | 308 | 53 | 4 | 0 | 231 | 24 | 53 | 6 | 31 | 2.42 | 0 | 0 |
  | Aug 3 | 308 | 54 | 3 | 0 | 230 | 23 | 53 | 6 | 31 | 1.63 | 0 | 0 |
  | Aug 10 | 310 | 47 | 3 | 1 | 226 | 23 | 46 | 5 | 31 | 1.48 | 0 | 0 |
  | Aug 17 | 310 | 48 | 5 | 3 | 224 | 23 | 44 | 5 | 31 | 2.02 | 0 | 0 |
  | Aug 24 | 320 | 62 | 9 | 1 | 220 | 22 | 58 | 6 | 32 | 2.84 | 0 | 0 |
  | Aug 31 | 321 | 71 | 8 | 1 | 225 | 23 | 63 | 7 | 33 | 5.38 | 0 | 0 |
  | Sep 7 | 323 | 67 | 9 | 1 | 227 | 23 | 58 | 6 | 33 | 6.30 | 0 | 0 |
  | Sep 14 | 324 | 54 | 9 | 1 | 234 | 24 | 48 | 5 | 33 | 4.60 | 0 | 0 |
  | Sep 21 | 329 | 62 | 4 | 1 | 247 | 25 | 57 | 6 | 33 | 2.68 | 0 | 0 |
  | Sep 28 | 340 | 64 | 6 | 1 | 251 | 26 | 59 | 6 | 34 | 3.90 | 0 | 0 |
  | Oct 5 | 343 | 59 | 7 | 1 | 253 | 26 | 56 | 6 | 35 | 8.51 | 0 | 0 |
  | Oct 12 | 351 | 58 | 3 | 1 | 248 | 25 | 57 | 6 | 36 | 5.68 | 0 | 0 |
  | Oct 19 | 352 | 50 | 5 | 1 | 252 | 26 | 49 | 5 | 36 | 1.86 | 0 | 0 |
  | Oct 26 | 351 | 49 | 4 | 1 | 252 | 26 | 48 | 5 | 36 | 1.90 | 0 | 0 |
  | Nov 2 | 351 | 42 | 4 | 1 | 254 | 26 | 41 | 5 | 36 | 1.24 | 0 | 0 |

  Reading (counts only): about 40-70 survey tokens a week trade >= 100k USD/day in their deepest pool, 2-11 trade >= 1M,
  0-3 >= 10M. A BL-39 top-10% group holds 22-26 tokens a week over the whole universe, 4-7 over the >= 100k/day part.
  The BL-38 bottom-10% bucket of this universe is dust (median 1-9 USD/day, none clears 10k): a lowest-volume bucket
  needs a volume floor first (an item for the BL-38 spec, not decided here). The universe grows from 290 to 351 because
  pools created after May join (any pool created before Oct 9 is in the set).
- Survivorship (go-message 4): selecting tokens from today's (Nov 9) GeckoTerminal lists is itself post-CUT selection,
  i.e. the survivorship bias: every token here was still busy or deep enough on Nov 9 to rank in a network or dex top-200
  list, so tokens that died, rugged or faded between May and November are not in the set at all, and the lists, ranked
  by today's 24 h activity, also pull in tokens that are hot today. That flatters long and momentum strategies
  (BL-39 most). Free data before CUT holds no list of established Base tokens as of May, so there is NO upper bound on
  the bias. Floor only, among tokens still listed today:

  | in the universe as of May 18 at | n | still there as of Nov 2 | below the line | no trade in the last week |
  |---|---|---|---|---|
  | >= 100k USD/day | 57 | 29 | 28 | 0 |
  | >= 1M USD/day | 10 | 1 | 9 | 0 |
  | >= 10M USD/day | 0 | 0 | 0 | 0 |

  134 tokens cleared 100k/day in at least one of the 25 weeks; 29 of them have a reserve under 100k at fetch. So even
  among survivors, half of the May >= 100k group fell below the line by November; the real attrition is at least that.
  An honest fix is forward only: snapshot the universe (list + reserve) every week from now on, so a later BL-38/39
  test can use point-in-time lists; not built (an item for the spec).
- Constraint on any BL-38/39 development/holdout split (go-message 4): the free API gives ~184 daily candles per pool
  and refuses older requests, so the usable daily history is 2026-05-09 .. 2026-11-07 (25 full weekly as-of points May
  18 - Nov 2; 21 with BL-39's full 31-close lookback, Jun 15 - Nov 2). The window moves forward one day a day and old
  days fall off the free API: backtest/liquid/daily.jsonl.gz is the only copy of May - Nov 2026. Any development and
  holdout split must fit inside it (or be a forward holdout after CUT), and must be fixed in calendar time before
  anyone reads a return from these files. Days at or after CUT are not in the files.
- Caveats:
  - reserve_usd is the deepest pool's total value, both sides, at fetch (Nov 9): not real depth (qliq) and not point
    in time. GeckoTerminal's pool answer has no per-side reserve, so quote_side_usd is null. No reserve history exists
    here: volume is the only point-in-time liquidity proxy.
  - Volume is the deepest pool's only (deepest at fetch, maybe not in May), not the token's volume over all pools; it
    can include wash or bot volume.
  - asset_kind()'s quote rule uses GeckoTerminal's base/quote sides among the token's top pools (19 dropped as quote);
    universe.json lists only the kept tokens (asset_kind ""), the excluded ones are counted by kind in "counts".
  - One pool is the deepest for both of its tokens (pjailbkOHM / pJailbrkPE): its volume counts for both.
  - Established is judged on the deepest pool's creation date; a token with an old shallow pool and a young deep one
    counts as young.
  - The lists are top-200 views (10 lists): smaller dexes and quiet pools outside every top 200 are missing.
- Tests: tests/test_liquid.py (21): every stored candle ends by CUT, the request's before_timestamp <= CUT, keep()
  drops the CUT day and any unfinished day, write() refuses a candle at CUT, list prices never kept (universe fields
  exact); asset_kind is core's own function (not copied), stable / wrapped (by symbol and by the last close) excluded;
  launch addresses from the address column only, a Nov 8 file never opened and a day at CUT refused, launch tokens
  dropped and counted only; young pools counted only (no candle call, not in the files); deepest pool per token;
  the call cap; history_check at most 2 attempts and its three answers; history counts (span vs candles held, thin
  pools, window start from daily-trading old pools); gaps; full weeks only, BL-39 group (31 closes, 10% up, "—"
  before the lookback fits) and BL-38 bucket (n, median, 10k / 100k); the survivorship floor; no return code (candle
  price columns read only in last_close(), no pnl / ret / chg / win / gain names, no such key in the output); core
  never imports tools.liquid. 477 pass on 3.13 (456 before).

## Open items (detail)
- Open items:
  - BL-7 done only after 24h of green runs, no overlap, one TICK row per run. The TICK rows show the real run rate for Gate A (90%).
  - With the cap, a busy tick checks only 9 leads, so "deferred" will rise; new leads deferred come back only if the next
    scan finds them again. Watch whether watched leads still get their 15-min re-checks on time.
    BL-20: the tick now goes on after the 9 GoPlus calls, so only GoPlus leads are deferred; compare after ~24h.
  - LP lock: GoPlus can't tie lp_holders to our pool unless it lists exactly one pool, so many leads die on
    missing:lp_locked_pct (Uniswap v4 may never have LP holders). The owner decides after 24-48h of BL-7 skip-reason data.
    -> Decided 2026-10-25 (option A) and built as BL-21: unknown LP lock allowed, logged lp_unknown.
  - Clanker/Zora factory addresses unconfirmed; launchpad is pool-shape only until BL-10 (CLANKER_CREATORS/ZORA_CREATORS empty).
  - top10 is the top 8-10 after exclusions (GoPlus returns only 10 holders); slightly lenient.
  - GoPlus rate limit is detected as any non-OK "code" in the body (fetch_error); the exact rate-limit code is unverified live
    (code 3 is NOT a rate limit, see GoPlus diagnosis above). BL-24: code 4029 came back 4 times in 137 calls at 2 s pacing
    (calls 82, 86, 90, 91), very likely the rate limit; not confirmed from GoPlus docs. The adapter already treats it as fetch_error.
  - Week-1 review of the watchlist: if more than about 1/3 of watched leads time out, the owner may raise WATCH max_h to 6.
    -> Decided 2026-10-25: WATCH max_h stays 3 h (timeouts stuck on honeypot + taxes); see BL-20 review above.

## Next step (full text before the 2026-10-23 cleanup)
- Next step: confirm the GoPlus fixes live from tasks.csv after the push: TICK rows show goplus<=9, no fetch_error with
  hosts=goplus at check 10+, 0xb2000000... leads get one SKIP goplus_partial, and any goplus_code=<n> seen is the real
  rate-limit code (write it in section 11). The owner decides on the "GoPlus wasted on DexScreener-only skips" card.
  After 2026-10-24 15:28 UTC count TICK rows since 15:28 (expect 288) and runs in the Actions tab; if green with one
  TICK row per run, mark BL-7 done, then BL-8 (report.py + report.yml).
