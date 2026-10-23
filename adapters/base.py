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
                       one pool and it is ours; else missing
  dev_pct              GoPlus max(creator_percent, owner_percent) x 100
  top10_pct            GoPlus holders, without the traded pool (v4: PoolManager) and burn addresses
  launchpad            GoPlus creator_address vs known factories, else pool shape, else "other"
  risk_flags           GoPlus flags set to "1" (see core/verify.py HARD_FLAGS for the skip list)
Extra keys: "pair" (traded pair address), "goplus_pending" (GoPlus fields not filled yet) and
"fetch_errors" (buckets whose request failed: 429, 5xx, timeout, bad answer -> hard_skip "fetch_error").

CLI: python -m adapters.base check <address>   |   python -m adapters.base scan
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.verify import HARD_FLAGS, REQUIRED, hard_skip

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


def facts(address, now=None, client=None):
    """The standard facts dict for one token. 2 requests, 3 when the age needs GeckoTerminal."""
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

    gp = _fetch(c, "goplus", f"{GOPLUS}?contract_addresses={address}", errors)
    if isinstance(gp, dict) and gp.get("code") != 1:  # GoPlus reports its rate limit in the body
        errors.append("goplus")
    g = _goplus_entry(gp, address)
    gf, pending = goplus_facts(g, pair_address, bool(pair) and is_v4(pair))
    out.update(gf)
    if pair:
        out["launchpad"] = launchpad(g, pair, side, pool_fee(g, pair_address, gt_pool))
    out["pair"] = pair_address
    out["goplus_pending"] = pending
    out["fetch_errors"] = errors
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- CLI
def _show(f, verdict, client):
    print(f"{'field':<20} value")
    for k in REQUIRED + ["pair", "goplus_pending", "fetch_errors"]:
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
    if argv == ["scan"]:
        c = Client()
        for lead in scan(c):
            print(f"{lead['address']}  {lead['symbol'] or '-':<12} {lead['source']}")
        print(f"requests: {len(c.calls)} " + ", ".join(str(s) for _, s in c.calls))
        return 0
    print("usage: python -m adapters.base check <address> | scan")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
