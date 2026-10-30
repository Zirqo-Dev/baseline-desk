"""Base adapter (EVM, chain 8453): scan() finds leads, facts() builds the standard facts dict.

Sources: DexScreener (pairs, profiles, boosts), GeckoTerminal (new pools, pool age),
GoPlus token_security (safety). Python stdlib only. A 429, timeout or bad answer is
missing data, never a crash. Every field we can't source is left out so hard_skip()
reports it.

Field mapping (DECISIONS 2026-10-23, BL-6):
  symbol               DexScreener pair, the side (baseToken/quoteToken) that is our address
  price                DexScreener priceUsd (quote side: priceUsd / priceNative)
  liq_usd              DexScreener liquidity.usd
  age_min              DexScreener pairCreatedAt, else GeckoTerminal pool_created_at, else missing
  sells_1h             DexScreener txns.h1.sells summed over ALL Base pairs with our token (quote side: buys)
  mint_open            GoPlus is_mintable
  freeze_or_blacklist  GoPlus is_blacklisted or transfer_pausable
  honeypot             GoPlus is_honeypot or cannot_sell_all
  buy_tax_pct          GoPlus buy_tax x 100
  sell_tax_pct         GoPlus sell_tax x 100
  lp_locked_pct        GoPlus lp_holders (locked + burned), only when GoPlus lists exactly
                       one pool and it is ours; else missing (unknown: allowed, logged lp_unknown, BL-21)
  dev_pct              GoPlus max(creator_percent, owner_percent) x 100
  top10_pct            GoPlus holders, without the traded pool (v4: PoolManager) and burn addresses
  launchpad            GoPlus creator_address vs known factories, else pool shape, else "other"
  risk_flags           GoPlus flags set to "1" (see core/verify.py HARD_FLAGS for the skip list)
Extra keys: "pair" (traded pair address), "goplus_pending" (GoPlus fields not filled yet),
"fetch_errors" (buckets whose request failed: 429, 5xx, timeout, bad answer -> hard_skip "fetch_error") and
"trade" (BL-25, logging only: b1 s1 b24 s24 liq dex from trade_facts(); no rule reads it).
GoPlus "code" (DECISIONS 2026-10-23, BL-7 GoPlus fixes): 1 = full answer; 3 = partial answer (no honeypot,
taxes or holders) -> "goplus_partial": True, a final skip; any other code -> fetch error, kept in "goplus_code".
DexScreener first (DECISIONS 2026-10-26, BL-20): when core/verify.py dex_skip() already decides a skip from the
DexScreener side (and GeckoTerminal age), GoPlus is not called and the facts carry "goplus_skipped": True.

quote(address, pair) prices a held token for EXIT (DECISIONS 2026-10-23, BL-7 B): the pool we
bought in, else the deepest Base pool ("fallback") only when that pool is gone. 1 request, 2 on fallback.

token_batch(addresses) (BL-26 outcome labels, logging only): one DexScreener /tokens/v1/base request for up to
30 addresses. It answers ONE pair per token (the deepest; probe 2026-10-30), not every Base pair.

CLI: python -m adapters.base check <address>   |   python -m adapters.base scan   |   ... quote <address> [pair]
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.verify import HARD_FLAGS, REQUIRED, TRADE_KEYS, dex_skip, hard_skip

CHAIN_ID = "8453"
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/base"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/" + CHAIN_ID

ETH = "0x0000000000000000000000000000000000000000"   # native ETH side of v4 pools
WETH = "0x4200000000000000000000000000000000000006"
ZORA = "0x1111111111166b7fe7bd91427724b487980afc69"  # quote token of "NODF / ZORA" in the GeckoTerminal fixture
# Well-known Base stablecoins (USDC, USDbC, DAI). A wrong entry only lets a stablecoin reach VERIFY.
STABLES = {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
           "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
           "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"}
STABLE_SYMBOLS = {"USDC", "USDBC", "USDT", "DAI", "EURC", "USDS", "USDE"}
NOT_LEADS = {ETH, WETH} | STABLES

BURN = {ETH, "0x000000000000000000000000000000000000dead"}
V4_POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"  # pool_manager in the GoPlus fixture

# Launchpad factory / creator addresses. Only addresses confirmed from on-chain data or
# fixtures go here, never from memory. None confirmed yet (BL-6), so the pool-shape fallback decides.
CLANKER_CREATORS = set()
ZORA_CREATORS = set()

# Seconds between calls per bucket (CLAUDE.md section 8). GoPlus has no published free
# limit there; 2 s keeps it gentle.
PACE = {"ds_slow": 1.0, "ds_pairs": 0.2, "gt": 2.0, "goplus": 2.0}
UA = {"User-Agent": "baseline-desk (educational paper desk)", "Accept": "application/json"}


FETCH_ERROR = object()  # returned by Client.get on 429, 5xx, timeout or a bad answer: retried, never final


class Client:
    """Paced HTTP GET returning parsed JSON; FETCH_ERROR on 429, 5xx, timeout or bad JSON; None on other 4xx."""

    def __init__(self, timeout=15, sleep=time.sleep, clock=time.monotonic):
        self.timeout, self.sleep, self.clock = timeout, sleep, clock
        self.last = {}
        self.calls = []  # (url, status) for the CLI and the request budget

    def get(self, bucket, url, headers=None):
        wait = self.last.get(bucket, -1e9) + PACE[bucket] - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.last[bucket] = self.clock()
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
            self.calls.append((url, 200))
            return json.loads(body)
        except urllib.error.HTTPError as e:
            self.calls.append((url, e.code))
            return FETCH_ERROR if e.code == 429 or e.code >= 500 else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            self.calls.append((url, type(e).__name__))
        return FETCH_ERROR


def _fetch(c, bucket, url, errors):
    """c.get(), with a failed request noted in `errors` and turned into None."""
    body = c.get(bucket, url)
    if body is FETCH_ERROR:
        errors.append(bucket)
        return None
    return body


# ---------------------------------------------------------------- small helpers
def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN is missing


def _flag(v):
    """GoPlus "1"/"0" -> True/False; anything else (absent, "") -> None."""
    return {"1": True, "0": False}.get(str(v)) if v is not None else None


def _lower(a):
    return (a or "").lower()


def _iso_min(iso, now):
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - t).total_seconds() / 60


# ---------------------------------------------------------------- SCAN
def _gt_symbols(name):
    """"CHUBBY / WETH" or "TIA / ETH 1%" -> ("CHUBBY", "ETH")."""
    parts = [p.strip() for p in (name or "").split(" / ")]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1].split(" ")[0]


def leads_from_gt(page):
    out = []
    for p in (page or {}).get("data") or []:
        a, r = p.get("attributes") or {}, p.get("relationships") or {}
        base = _lower(((r.get("base_token") or {}).get("data") or {}).get("id", "")).removeprefix("base_")
        quote = _lower(((r.get("quote_token") or {}).get("data") or {}).get("id", "")).removeprefix("base_")
        bsym, qsym = _gt_symbols(a.get("name"))
        addr, sym = (base, bsym) if base not in NOT_LEADS else (quote, qsym)
        out.append({"address": addr, "symbol": sym, "source": "gt_new_pools",
                    "pool": _lower(a.get("address"))})
    return out


def leads_from_ds(items, source):
    return [{"address": _lower(i.get("tokenAddress")), "symbol": "", "source": source, "pool": ""}
            for i in (items or []) if isinstance(i, dict) and i.get("chainId") == "base"]


def scan(client=None):
    """GeckoTerminal new_pools page 1 + DexScreener latest profiles and boosts on Base.

    Deduped by token address (first source wins); WETH, native ETH and stablecoins dropped.
    3 requests. A failed source just adds no leads.
    """
    c, errors = client or Client(), []
    raw = (leads_from_gt(_fetch(c, "gt", f"{GT}/new_pools?page=1", errors))
           + leads_from_ds(_fetch(c, "ds_slow", f"{DS}/token-profiles/latest/v1", errors), "ds_profile")
           + leads_from_ds(_fetch(c, "ds_slow", f"{DS}/token-boosts/latest/v1", errors), "ds_boost"))
    seen, out = set(), []
    for lead in raw:
        a = lead["address"]
        if (not re.fullmatch(r"0x[0-9a-f]{40}", a) or a in NOT_LEADS or a in seen
                or lead["symbol"].upper() in STABLE_SYMBOLS):
            continue
        seen.add(a)
        out.append(lead)
    return out


# ---------------------------------------------------------------- VERIFY facts
def pick_pair(pairs, address):
    """Highest-liquidity Base pair with our token on either side. Returns (pair, side) or (None, "")."""
    best, side, best_liq = None, "", -1.0
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("chainId") != "base":
            continue
        for s in ("baseToken", "quoteToken"):
            if _lower((p.get(s) or {}).get("address")) == address:
                liq = _num((p.get("liquidity") or {}).get("usd")) or 0.0
                if liq > best_liq:
                    best, side, best_liq = p, s, liq
    return best, side


def total_sells_1h(pairs, address):
    """Sells of our token in the last hour, summed over every Base pair that holds it. None if no pair has the number."""
    total = None
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("chainId") != "base":
            continue
        h1 = (p.get("txns") or {}).get("h1") or {}
        for side, key in (("baseToken", "sells"), ("quoteToken", "buys")):  # quote side: base buys = our sells
            if _lower((p.get(side) or {}).get("address")) == address and _num(h1.get(key)) is not None:
                total = (total or 0.0) + _num(h1.get(key))
    return total


def trade_facts(pairs, pair, address):
    """BL-25, logging only: b1 s1 b24 s24 liq dex for the VERIFY and baseline BUY rows.

    Buys/sells per window (h1, h24) summed over every Base pair with our token: as baseToken b = buys and
    s = sells, as quoteToken the mirror (b = sells, s = buys), the side logic of total_sells_1h, so s1 equals
    sells_1h. liq = the chosen pair's liquidity.usd rounded; dex = its dexId plus its version label (uniswap:v4;
    not "pool": the baseline BUY row already has pool=, BL-8). qliq (BL-28) = the USD value of the chosen pair's
    other side (ETH, WETH, USDC...), see quote_liq().
    A value DexScreener didn't give, or an odd shape, is "na", never 0. Never raises.
    """
    out = dict.fromkeys(TRADE_KEYS, "na")
    try:
        for p in pairs or []:
            if not isinstance(p, dict) or p.get("chainId") != "base":
                continue
            txns = _dict(p.get("txns"))
            for side, b, s in (("baseToken", "buys", "sells"), ("quoteToken", "sells", "buys")):
                if _lower(_dict(p.get(side)).get("address")) != address:
                    continue
                for w, kb, ks in (("h1", "b1", "s1"), ("h24", "b24", "s24")):
                    for key, field in ((kb, b), (ks, s)):
                        x = _num(_dict(txns.get(w)).get(field))
                        if x is not None:
                            out[key] = (0 if out[key] == "na" else out[key]) + int(x)
        pair = _dict(pair)
        liq = _num(_dict(pair.get("liquidity")).get("usd"))
        if liq is not None:
            out["liq"] = round(liq)
        if isinstance(pair.get("dexId"), str) and pair["dexId"]:
            labels = pair.get("labels") if isinstance(pair.get("labels"), list) else []
            version = next((x for x in labels if isinstance(x, str) and re.fullmatch(r"v\d+", x)), "")
            out["dex"] = re.sub(r"[^\w.-]", "_", pair["dexId"]) + (f":{version}" if version else "")  # one detail token
        out["qliq"] = quote_liq(pair, address)
    except Exception:  # logging must never break a check
        return dict.fromkeys(TRADE_KEYS, "na")
    return out


def _dict(x):
    return x if isinstance(x, dict) else {}


def quote_liq(pair, address):
    """BL-28, logging only: the USD value of the side of `pair` that is NOT our token, rounded; "na" if unknown.

    Our token as baseToken: liquidity.quote x (priceUsd / priceNative), the quote token's USD price. Our token as
    quoteToken: liquidity.base x priceUsd (priceUsd is the base token's price). Any input missing or 0 -> "na", never 0;
    a real side worth under 0.50 USD rounds to 0 and stays 0 (e.g. 21.66 ZORA = 0.13 USD), like BL-25's real 0.
    """
    try:
        pair = _dict(pair)
        liq, usd, native = _dict(pair.get("liquidity")), _num(pair.get("priceUsd")), _num(pair.get("priceNative"))
        if _lower(_dict(pair.get("baseToken")).get("address")) == address:
            amount, price = _num(liq.get("quote")), usd / native if usd and native else None
        elif _lower(_dict(pair.get("quoteToken")).get("address")) == address:
            amount, price = _num(liq.get("base")), usd
        else:
            return "na"
        if not amount or not price or amount < 0 or price < 0:
            return "na"
        value = amount * price
        return round(value) if value == value and value != float("inf") else "na"
    except Exception:  # logging must never break a check
        return "na"


def dex_facts(pair, side, now):
    """symbol, price, liq_usd, age_min (None if no pairCreatedAt) from the traded DexScreener pair."""
    f = {"symbol": (pair.get(side) or {}).get("symbol") or None,
         "liq_usd": _num((pair.get("liquidity") or {}).get("usd"))}
    usd, native = _num(pair.get("priceUsd")), _num(pair.get("priceNative"))
    if side == "baseToken":
        f["price"] = usd
    else:  # pair prices the base token in our token; invert through the native price
        f["price"] = usd / native if usd and native else None
    created = _num(pair.get("pairCreatedAt"))
    f["age_min"] = (now.timestamp() * 1000 - created) / 60000 if created else None
    return f


def _goplus_entry(gp, address):
    if not isinstance(gp, dict) or gp.get("code") != 1:
        return {}
    return ((gp.get("result") or {}).get(address)) or {}


def goplus_facts(g, pair_address, is_v4):
    """GoPlus fields. Returns (facts, pending): pending = fields GoPlus has not filled yet.

    A field left out but not pending is a structural gap (final skip), e.g. the LP lock
    when GoPlus lists several pools and can't be tied to ours.
    """
    f, pending = {}, []

    def need(field, *keys):
        vals = [g.get(k) for k in keys]
        if any(v in (None, "", []) for v in vals):
            pending.append(field)
            return None
        return vals

    v = need("mint_open", "is_mintable")
    if v:
        f["mint_open"] = _flag(v[0])
    v = need("freeze_or_blacklist", "is_blacklisted", "transfer_pausable")
    if v:
        a, b = _flag(v[0]), _flag(v[1])
        f["freeze_or_blacklist"] = None if a is None or b is None else (a or b)
    v = need("honeypot", "is_honeypot", "cannot_sell_all")
    if v:
        a, b = _flag(v[0]), _flag(v[1])
        f["honeypot"] = None if a is None or b is None else (a or b)
    for field, key in (("buy_tax_pct", "buy_tax"), ("sell_tax_pct", "sell_tax")):
        v = need(field, key)
        if v and _num(v[0]) is not None:
            f[field] = _num(v[0]) * 100
    v = need("dev_pct", "creator_percent")
    if v and _num(v[0]) is not None:
        f["dev_pct"] = max(_num(v[0]), _num(g.get("owner_percent")) or 0.0) * 100

    # top10: drop the traded pool (v4 tokens sit in the PoolManager) and burn addresses
    v = need("top10_pct", "holders")
    if v:
        skip = BURN | {pair_address} | ({V4_POOL_MANAGER} if is_v4 else set())
        held = [_num(h.get("percent")) for h in v[0] if _lower(h.get("address")) not in skip]
        if all(x is not None for x in held):
            f["top10_pct"] = sum(sorted(held, reverse=True)[:10]) * 100

    # LP lock on the pool we trade: provable only when GoPlus lists exactly one pool and it is ours
    v = need("lp_locked_pct", "lp_holders", "dex")
    if v:
        lp, dex = v
        if len(dex) == 1 and _lower(dex[0].get("pair")) == pair_address:
            pcts = [(_num(h.get("percent")), h) for h in lp]
            if all(x is not None for x, _ in pcts):
                f["lp_locked_pct"] = 100 * sum(x for x, h in pcts
                                               if str(h.get("is_locked")) == "1" or _lower(h.get("address")) in BURN)

    v = need("risk_flags", *HARD_FLAGS)
    if v:
        flags = [k for k, x in zip(HARD_FLAGS, v) if _flag(x)]
        if _flag(g.get("external_call")):
            flags.append("external_call")
        if _flag(g.get("is_open_source")) is False:
            flags.append("is_open_source=0")
        if (_num(g.get("honeypot_with_same_creator")) or 0) > 0:
            flags.append("honeypot_with_same_creator")
        f["risk_flags"] = flags
    return f, pending


def launchpad(g, pair, side, pool_fee):
    """Known creator -> clanker/zora; else ZORA quote -> zora; else v4 pool at 1% fee -> clanker; else other."""
    creator = _lower(g.get("creator_address"))
    if creator and creator in CLANKER_CREATORS:
        return "clanker"
    if creator and creator in ZORA_CREATORS:
        return "zora"
    other = "quoteToken" if side == "baseToken" else "baseToken"
    if _lower((pair.get(other) or {}).get("address")) == ZORA:
        return "zora"
    if is_v4(pair) and pool_fee is not None and abs(pool_fee - 0.01) < 1e-9:
        return "clanker"
    return "other"


def is_v4(pair):
    return "v4" in (pair.get("labels") or []) or len(_lower(pair.get("pairAddress"))) == 66


def pool_fee(g, pair_address, gt_pool=None):
    """Pool fee as a fraction (0.01 = 1%): GoPlus dex entry for our pair, else the GeckoTerminal pool name."""
    for d in g.get("dex") or []:
        if _lower(d.get("pair")) == pair_address and _num(d.get("pool_fee")) is not None:
            return _num(d.get("pool_fee"))
    name = ((gt_pool or {}).get("data") or {}).get("attributes", {}).get("name", "")
    m = re.search(r"(\d+(?:\.\d+)?)%$", name or "")
    return float(m.group(1)) / 100 if m else None


def facts(address, now=None, client=None, goplus_ok=None, dex=None):
    """The standard facts dict for one token. 2 requests, 3 when the age needs GeckoTerminal.

    DexScreener first (BL-20): if dex_skip() decides on what is already in hand, no GoPlus call
    (1-2 requests) and "goplus_skipped": True. Otherwise, when goplus_ok() says the GoPlus budget
    is used, returns None: the caller defers the lead.

    `dex` (BL-8): a dict filled with the DexScreener side before any GoPlus step, also when the
    lead is deferred: symbol, price, liq_usd, age_min, sells_1h, pair, fetch_errors and launchpad
    from pool shape only (no GoPlus creator). The return value is the same with or without it.
    """
    c = client or Client()
    now = now or datetime.now(timezone.utc)
    address = _lower(address)
    out, errors = {}, []

    pairs = _fetch(c, "ds_pairs", f"{DS}/token-pairs/v1/base/{address}", errors)
    pair, side = pick_pair(pairs, address)
    pair_address, gt_pool = "", None
    if pair:
        pair_address = _lower(pair.get("pairAddress"))
        out.update(dex_facts(pair, side, now))
        out["sells_1h"] = total_sells_1h(pairs, address)
        if out.get("age_min") is None:
            gt_pool = _fetch(c, "gt", f"{GT}/pools/{pair_address}", errors)
            created = ((gt_pool or {}).get("data") or {}).get("attributes", {}).get("pool_created_at")
            out["age_min"] = _iso_min(created, now)

    trade = trade_facts(pairs, pair, address)  # BL-25: from the pairs already fetched, no request
    if dex is not None:
        dex.update({k: v for k, v in out.items() if v is not None},
                   pair=pair_address, fetch_errors=list(errors), trade=trade)
        if pair:
            dex["launchpad"] = launchpad({}, pair, side, pool_fee({}, pair_address, gt_pool))
    if dex_skip(dict(out, fetch_errors=errors)):
        out.update(pair=pair_address, goplus_skipped=True, fetch_errors=errors, trade=trade)
        return {k: v for k, v in out.items() if v is not None}
    if goplus_ok is not None and not goplus_ok():
        return None

    gp = _fetch(c, "goplus", f"{GOPLUS}?contract_addresses={address}", errors)
    code = gp.get("code") if isinstance(gp, dict) else None
    if code == 3:  # partial answer: GoPlus can't read this token, waiting doesn't help
        out["goplus_partial"] = True
    elif isinstance(gp, dict) and code != 1:  # GoPlus reports its rate limit in the body
        errors.append("goplus")
        out["goplus_code"] = code
    g = _goplus_entry(gp, address)
    gf, pending = goplus_facts(g, pair_address, bool(pair) and is_v4(pair))
    out.update(gf)
    if pair:
        out["launchpad"] = launchpad(g, pair, side, pool_fee(g, pair_address, gt_pool))
    out["pair"] = pair_address
    out["goplus_pending"] = pending
    out["fetch_errors"] = errors
    out["trade"] = trade
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- EXIT quote
def quote(address, pair="", client=None):
    """Price and liquidity of a held token: {"price", "liq_usd", "source": "pool" | "fallback"}.

    Reads the pool we bought in. Only when that pool is gone (no pair in the answer, or a 4xx)
    falls back to the highest-liquidity Base pair. A failed request (429, 5xx, timeout) or a
    missing number is left out, so exits.check() logs HOLD no_quote.
    """
    c = client or Client()
    address, pair = _lower(address), _lower(pair)
    found, side, source = None, "", "pool"
    if pair:
        body = c.get("ds_pairs", f"{DS}/latest/dex/pairs/base/{pair}")
        if body is FETCH_ERROR:
            return None
        body = body if isinstance(body, dict) else {}
        pairs = body.get("pairs") if isinstance(body.get("pairs"), list) else []
        found, side = pick_pair(pairs + [body.get("pair")], address)
        if found and _lower(found.get("pairAddress")) != pair:
            found, side = None, ""
    if not found:  # no saved pool counts as a fallback too
        source = "fallback"
        body = c.get("ds_pairs", f"{DS}/token-pairs/v1/base/{address}")
        if body is FETCH_ERROR:
            return None
        found, side = pick_pair(body, address)
    if not found:
        return None
    f = dex_facts(found, side, datetime.now(timezone.utc))
    out = {"price": f["price"], "liq_usd": f["liq_usd"], "source": source}
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- LABELS (BL-26)
def token_batch(addresses, client=None):
    """Pairs for up to 30 tokens in one request (one pair per token). A list, or FETCH_ERROR / None on failure."""
    c = client or Client()
    return c.get("ds_pairs", f"{DS}/tokens/v1/base/{','.join(addresses)}")


# ---------------------------------------------------------------- CLI
def _show(f, verdict, client):
    print(f"{'field':<20} value")
    for k in REQUIRED + ["pair", "goplus_pending", "fetch_errors", "goplus_partial", "goplus_code", "goplus_skipped"]:
        v = f.get(k, "MISSING")
        print(f"{k:<20} {round(v, 4) if isinstance(v, float) else v}")
    print(f"\nverdict: {verdict or 'PASS'}")
    print(f"requests: {len(client.calls)} " + ", ".join(str(s) for _, s in client.calls))


def main(argv):
    if len(argv) == 2 and argv[0] == "check":
        c = Client()
        f = facts(argv[1], client=c)
        _show(f, hard_skip(f), c)
        return 0
    if len(argv) in (2, 3) and argv[0] == "quote":
        c = Client()
        print(quote(argv[1], argv[2] if len(argv) == 3 else "", c))
        print(f"requests: {len(c.calls)} " + ", ".join(str(s) for _, s in c.calls))
        return 0
    if argv == ["scan"]:
        c = Client()
        for lead in scan(c):
            print(f"{lead['address']}  {lead['symbol'] or '-':<12} {lead['source']}")
        print(f"requests: {len(c.calls)} " + ", ".join(str(s) for _, s in c.calls))
        return 0
    print("usage: python -m adapters.base check <address> | scan | quote <address> [pair]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
