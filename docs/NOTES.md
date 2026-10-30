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
    (code 3 is NOT a rate limit, see GoPlus diagnosis above).
  - Week-1 review of the watchlist: if more than about 1/3 of watched leads time out, the owner may raise WATCH max_h to 6.
    -> Decided 2026-10-25: WATCH max_h stays 3 h (timeouts stuck on honeypot + taxes); see BL-20 review above.

## Next step (full text before the 2026-10-23 cleanup)
- Next step: confirm the GoPlus fixes live from tasks.csv after the push: TICK rows show goplus<=9, no fetch_error with
  hosts=goplus at check 10+, 0xb2000000... leads get one SKIP goplus_partial, and any goplus_code=<n> seen is the real
  rate-limit code (write it in section 11). The owner decides on the "GoPlus wasted on DexScreener-only skips" card.
  After 2026-10-24 15:28 UTC count TICK rows since 15:28 (expect 288) and runs in the Actions tab; if green with one
  TICK row per run, mark BL-7 done, then BL-8 (report.py + report.yml).
