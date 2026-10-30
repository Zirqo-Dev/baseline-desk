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
