"""BL-45 cost meter, read-only (the owner's D5, docs/DECISIONS.md 2026-11-11 / 2026-11-12; docs/NOTES.md "BL-45").

What it measures: the real cost of a 5 and a 25 USD round trip (buy, then sell everything back) in the deepest pool of
each established liquid Base token, plus the hop from that pool's quote token to USDC, and whether the sale goes
through. Every number comes from ONE source, Base's public RPC (https://mainnet.base.org, keyless), by eth_call only:
a probe contract (tools/costprobe.sol, its runtime bytecode in tools/costprobe.hex) is placed at a made-up address with
an eth_call state override, together with a quote-token balance (the token's balance slot, found and confirmed by a
balanceOf check at the freeze). Nothing is ever broadcast: there is no key, wallet, signature or allowance anywhere.

Stored: percentages and pass/fail only (no price, no token amount, no USD value of a token), under backtest/costs/,
never under data/. core/ never imports this module or reads its files (test); no rule reads them.

  python -m tools.costs --freeze             build backtest/costs/tokens.json (done once, BL-45; committed)
  python -m tools.costs --run [--out DIR] [--tokens a,b] [--now ISO]
                                             one run: rows appended to <out>/<UTC day>.csv (default backtest/costs/)
  python -m tools.costs --summary [--all]    per token and size: n, median / p90 round trip %, sale-sim fail rate
  python -m tools.costs --probe-input        the solc standard-JSON input (rebuild: docs/NOTES.md "BL-45")
  python -m tools.costs --probe-output       read solc's standard-JSON output on stdin, write tools/costprobe.hex
"""
import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RPC = "https://mainnet.base.org"
SOURCE = "base-rpc eth_call"
CHAIN_ID = 8453
DIR = "backtest/costs"
TOKENS = f"{DIR}/tokens.json"
WINDOW = f"{DIR}/window.json"
PROBE_SOL = "tools/costprobe.sol"
PROBE_HEX = "tools/costprobe.hex"
SOLC = "0.8.26"  # solcjs, pinned (npm solc@0.8.26)

SIZES = (5, 25)  # USD per round trip
MAX_CALLS = 100  # hard cap on RPC requests per run, retries included
PACE_S = 0.5  # between requests (measured, docs/NOTES.md "BL-45")
RETRIES = 3
BACKOFF_S = 5
TIMEOUT_S = 20
GAS = hex(30_000_000)
TAX_LIKE_PCT = 1.0  # a received amount this far under the pool's own math = tax_like
WINDOW_DAYS = 7

RULE_MIN_MEDIAN_VOL_USD = 100_000  # survey measure: median daily volume of the deepest pool, the 30 days before CUT
RULE_DAYS = 30

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
PROBE = "0x0000000000000000000000000000000000c0517e"  # made-up: code placed only inside eth_call
FROM = "0x00000000000000000000000000000000000c0517"  # made-up caller, no code, no key
FUND = 10 ** 30  # planted start-token balance (raw units), inside eth_call only

# GeckoTerminal dex label (backtest/liquid/universe.json) -> pool kind. v3-style pools are paid in the swap callback,
# the solidly v2 pool by a transfer before swap(). Anything else is excluded at the freeze and counted.
V3, V2 = 1, 2
DEX_KIND = {
    "uniswap-v3-base": V3,
    "pancakeswap-v3-base": V3,
    "aerodrome-slipstream": V3,
    "aerodrome-slipstream-2": V3,
    "aerodrome-slipstream-3": V3,
    "aerodrome-base": V2,
}
V3_FEES = {"uniswap-v3-base": (100, 500, 3000, 10000), "pancakeswap-v3-base": (100, 500, 2500, 10000)}
CL_SPACINGS = (1, 10, 50, 100, 200, 2000)  # Aerodrome Slipstream getPool(a, b, tickSpacing)

STAGES = {0: "pass", 1: "no_route", 2: "revert_buy", 3: "revert_sell", 4: "no_route"}
COLUMNS = ["ts", "block", "leg", "token", "symbol", "start", "pool", "dex", "fee_pct", "size_usd", "buy_pct",
           "sell_pct", "rt_pct", "usdc_rt_pct", "short_buy_pct", "short_sell_pct", "sim", "reason", "source",
           "latency_ms"]


class RpcError(Exception):
    pass


# ---- keccak-256 (Ethereum's, not NIST SHA3), pure Python: selectors and storage keys ----
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                r = _ROT[x][y]
                b[y][(2 * x + 3 * y) % 5] = ((a[x][y] << r) | (a[x][y] >> (64 - r))) & _M if r else a[x][y]
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak(data):
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


def selector(sig):
    return keccak(sig.encode()).hex()[:8]


# ---- ABI, just what the probe and a few views need ----
def _word(v):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, str):
        v = int(v, 16)
    if v < 0:
        v += 1 << 256
    return f"{v:064x}"


def calldata(sig, *args):
    return "0x" + selector(sig) + "".join(_word(a) for a in args)


def words(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - len(h) % 64, 64)]


def addr_of(word):
    return "0x" + f"{word:064x}"[-40:]


def probe_code(root="."):
    return "0x" + (Path(root) / PROBE_HEX).read_text().strip()


# ---- RPC client: paced, capped, retries on 429 / 5xx / timeouts; every attempt counts toward the cap ----
class Rpc:
    def __init__(self, url=RPC, urlopen=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
                 pace=PACE_S, max_calls=MAX_CALLS):
        self.url, self.urlopen, self.sleep, self.clock = url, urlopen, sleep, clock
        self.pace, self.max_calls = pace, max_calls
        self.calls = self.refused = 0
        self.last_ms = 0
        self._last = None

    def capped(self):
        return self.calls >= self.max_calls

    def call(self, method, params):
        for attempt in range(RETRIES):
            if self.capped():
                raise RpcError("cap")
            if self._last is not None:
                gap = self.pace - (self.clock() - self._last)
                if gap > 0:
                    self.sleep(gap)
            self._last = self.clock()
            self.calls += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json",
                                                                       "user-agent": "baseline-costs/1"})
            t0 = self.clock()
            try:
                with self.urlopen(req, timeout=TIMEOUT_S) as f:
                    out = json.loads(f.read())
                self.last_ms = int((self.clock() - t0) * 1000)
                if "error" in out:
                    err = out["error"] or {}
                    if err.get("code") in (-32005, -32016, 429):  # rate limited
                        self.refused += 1
                    else:
                        raise RpcError(f"rpc {err.get('code')}")
                else:
                    return out["result"]
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500:
                    raise RpcError(f"http {e.code}")
                self.refused += 1
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                self.refused += 1
            if attempt < RETRIES - 1:
                self.sleep(BACKOFF_S * (attempt + 1))
        raise RpcError("retries")

    def eth_call(self, to, data, block="latest", overrides=None):
        params = [{"from": FROM, "to": to, "data": data, "gas": GAS}, block]
        if overrides:
            params.append(overrides)
        return self.call("eth_call", params)


# ---- mid price and cost math (raw units; decimals cancel) ----
def mid_price(kind, a, b, start_is_token0):
    """Start-token raw units per other-token raw unit at the pool's mid. v3-style: a = sqrtPriceX96; v2: reserves."""
    if not a or (kind == V2 and not b):
        return None
    p01 = (a * a) / float(1 << 192) if kind == V3 else b / a  # token1 per token0
    return 1 / p01 if start_is_token0 else p01


def pct(x):
    return round(100.0 * x, 4)


def trip_costs(kind, start_is_token0, r):
    """r = the probe's 11 words. Costs against the pool's own mid: buy vs the mid before it, sell vs the mid just
    before the sell (after our buy), round trip = both sides chained. Shortfalls compare what was received with the
    pool's own math (a token tax shows there)."""
    spent, pout_t, recv_t, sent_t, arr_t, pout_s, recv_s = r[2], r[3], r[4], r[7], r[8], r[9], r[10]
    p0 = mid_price(kind, r[0], r[1], start_is_token0)
    p1 = mid_price(kind, r[5], r[6], start_is_token0)
    out = {"buy_pct": None, "sell_pct": None, "rt_pct": None, "short_buy_pct": None, "short_sell_pct": None}
    if spent and p0:
        out["buy_pct"] = pct(1 - recv_t * p0 / spent)
    if recv_t and p1:
        out["sell_pct"] = pct(1 - recv_s / (recv_t * p1))
    if out["buy_pct"] is not None and out["sell_pct"] is not None:
        # held round trip: buy now, sell later at an unchanged mid. NOT 1 - recv_s / spent: an instant flip in the
        # same call wins its own price impact back and shows only the fees (docs/NOTES.md "BL-45").
        out["rt_pct"] = pct(1 - (1 - out["buy_pct"] / 100) * (1 - out["sell_pct"] / 100))
    if pout_t:
        out["short_buy_pct"] = pct(1 - recv_t / pout_t)
    shorts = [1 - arr_t / sent_t if sent_t else 0.0, 1 - recv_s / pout_s if pout_s else 0.0]
    out["short_sell_pct"] = pct(max(shorts))
    return out


def verdict(stage, costs):
    reason = STAGES.get(stage, "no_route")
    if reason == "pass":
        sb, ss = costs["short_buy_pct"] or 0.0, costs["short_sell_pct"] or 0.0
        if sb > TAX_LIKE_PCT or ss > TAX_LIKE_PCT:
            reason = "tax_like"
    return ("pass" if reason == "pass" else "fail"), reason


def combine(rts):
    """Round trip % chained over legs: 1 - prod(1 - rt). None when a leg is missing."""
    if not rts or any(x is None for x in rts):
        return None
    keep = 1.0
    for x in rts:
        keep *= 1 - x / 100.0
    return pct(1 - keep)


# ---- the freeze (once, BL-45): rule, exclusions, pools, balance slots, hop pools ----
def slot_keys(holder):
    """Candidate storage keys of holder's balance: Solidity mapping at slot 0..40, Vyper (key order swapped),
    Solady ERC20 (seed 0x87a211a2), OpenZeppelin v5 namespaced ERC20."""
    h = bytes.fromhex(holder[2:].rjust(64, "0"))
    out = []
    for i in range(41):
        s = i.to_bytes(32, "big")
        out.append((f"solidity:{i}", "0x" + keccak(h + s).hex()))
        out.append((f"vyper:{i}", "0x" + keccak(s + h).hex()))
    out.append(("solady", "0x" + keccak(bytes.fromhex(holder[2:]) + bytes(8) + bytes.fromhex("87a211a2")).hex()))
    oz = bytes.fromhex("52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00")
    out.append(("oz5", "0x" + keccak(h + oz).hex()))
    return out


def find_slot(rpc, token, holder=PROBE, block="latest"):
    """The first candidate key whose planted value balanceOf(holder) returns exactly; None if none confirms."""
    data = calldata("balanceOf(address)", holder)
    for layout, key in slot_keys(holder):
        try:
            res = rpc.eth_call(token, data, block, {token: {"stateDiff": {key: "0x" + _word(FUND)}}})
        except RpcError:
            continue
        w = words(res)
        if w and w[0] == FUND:
            return {"layout": layout, "key": key}
    return None


def view_addr(rpc, to, sig, block):
    return addr_of(words(rpc.eth_call(to, calldata(sig), block))[0])


def view_uint(rpc, to, sig, block, *args):
    return words(rpc.eth_call(to, calldata(sig, *args), block))[0]


def view_symbol(rpc, to, block):
    """symbol() as text, for display only; "" when unreadable."""
    try:
        h = rpc.eth_call(to, calldata("symbol()"), block)[2:]
        w = words(h)
        if len(w) >= 2 and w[0] == 32:
            return bytes.fromhex(h[128:128 + 2 * w[1]]).decode("utf-8", "replace")
        return bytes.fromhex(h[:64]).rstrip(b"\x00").decode("utf-8", "replace")
    except (RpcError, ValueError, IndexError):
        return ""


def pool_meta(rpc, pool, dex, block):
    kind = DEX_KIND[dex]
    t0 = view_addr(rpc, pool, "token0()", block)
    t1 = view_addr(rpc, pool, "token1()", block)
    factory = view_addr(rpc, pool, "factory()", block)
    if kind == V3:
        fee_pct = view_uint(rpc, pool, "fee()", block) / 10_000
        stable = False
    else:
        stable = bool(view_uint(rpc, pool, "stable()", block))
        fee_pct = view_uint(rpc, factory, "getFee(address,bool)", block, pool, stable) / 100
    return {"pool": pool, "dex": dex, "kind": kind, "token0": t0, "token1": t1, "factory": factory,
            "fee_pct": fee_pct, "stable": stable}


def launch_addresses(root):
    """Every address in the address column of every data/daily/*/tasks.csv (the column only; seal A)."""
    out = set()
    for p in sorted(Path(root).glob("data/daily/*/tasks.csv")):
        with open(p, newline="") as f:
            rd = csv.reader(f)
            head = next(rd, None)
            if not head or "address" not in head:
                continue
            i = head.index("address")
            for row in rd:
                if len(row) > i and row[i].startswith("0x"):
                    out.add(row[i].lower())
    return out


def rule_candidates(root):
    """The rule: established survey tokens (backtest/liquid/universe.json) whose deepest pool's median daily volume
    over the 30 days before CUT is >= 100k USD (a no-trade day = 0). Volume column only (BL-39 seal)."""
    from tools import candles, liquid
    u, lines = liquid.load(root)
    cut = candles._ts(u["cut"])
    vols = liquid._vols(lines)
    days = [cut - liquid.DAY_S * k for k in range(RULE_DAYS, 0, -1)]
    out = []
    for x in u["tokens"]:
        med = statistics.median([vols.get(x["token"], {}).get(t, 0.0) for t in days])
        if med >= RULE_MIN_MEDIAN_VOL_USD:
            out.append(x)
    return sorted(out, key=lambda x: x["token"]), u


def _depth_usd(rpc, pool, side, usd_per_raw, block):
    return view_uint(rpc, side, "balanceOf(address)", block, pool) * usd_per_raw


def hop_search(rpc, q, factories, usd_per_raw, block):
    """The deepest pool between q and USDC or WETH over the factories the candidates' pools came from, by the USD value
    of the USDC / WETH it holds. Returns (pool meta, side) or (None, None)."""
    best, best_usd = None, 0.0
    for side in (USDC, WETH):
        if q == side:
            continue
        for dex, fac in factories.items():
            kind = DEX_KIND[dex]
            if kind == V2:
                tries = [("getPool(address,address,bool)", False)]
            elif dex.startswith("aerodrome-slipstream"):
                tries = [("getPool(address,address,int24)", s) for s in CL_SPACINGS]
            else:
                tries = [("getPool(address,address,uint24)", f) for f in V3_FEES.get(dex, ())]
            for sig, arg in tries:
                try:
                    p = addr_of(words(rpc.eth_call(fac, calldata(sig, q, side, arg), block))[0])
                except RpcError:
                    continue
                if int(p, 16) == 0:
                    continue
                usd = _depth_usd(rpc, p, side, usd_per_raw[side], block)
                if usd > best_usd:
                    best, best_usd = (p, dex, side), usd
    if not best:
        return None, None
    meta = pool_meta(rpc, best[0], best[1], block)
    meta["depth_usd_at_freeze"] = round(best_usd)
    return meta, best[2]


def freeze(rpc, root, now=None):
    now = now or datetime.now(timezone.utc)
    block = hex(int(rpc.call("eth_blockNumber", []), 16))
    cands, u = rule_candidates(root)
    launch = launch_addresses(root)
    excluded, kept = [], []
    for x in cands:
        t, dex = x["token"].lower(), x["dex"]
        base = {"token": t, "symbol": x["symbol"], "pool": x["pool"], "dex": dex}
        if t in launch:
            excluded.append({**base, "reason": "launch_universe"})
        elif dex == "uniswap-v4-base":
            excluded.append({**base, "reason": "uniswap_v4"})
        elif dex not in DEX_KIND:
            excluded.append({**base, "reason": "unsupported_pool"})
        else:
            kept.append((x, base))
    tokens, factories = [], {}
    for x, base in kept:
        m = pool_meta(rpc, x["pool"], x["dex"], block)
        if base["token"] not in (m["token0"], m["token1"]):
            excluded.append({**base, "reason": "token_not_in_pool"})
            continue
        if m["stable"]:
            excluded.append({**base, "reason": "stable_curve"})
            continue
        factories.setdefault(m["dex"], m["factory"])
        quote = m["token1"] if base["token"] == m["token0"] else m["token0"]
        tokens.append({**base, "kind": m["kind"], "token0": m["token0"], "fee_pct": m["fee_pct"],
                       "factory": m["factory"], "quote": quote})
    # balance slots for every start token (quotes, plus USDC and WETH for the hops)
    slots, quotes = {}, sorted({x["quote"] for x in tokens} | {USDC, WETH})
    for q in quotes:
        slots[q] = find_slot(rpc, q, PROBE, block)
    # WETH/USDC first: its mid prices WETH for the depth comparison of other hops
    usd_per_raw = {USDC: 1e-6, WETH: None}
    hops = {}
    weth_meta, _ = hop_search(rpc, WETH, factories, {USDC: 1e-6, WETH: 0.0}, block)
    if weth_meta:
        hops[WETH] = {**weth_meta, "start": USDC}
        a, b = words(rpc.eth_call(PROBE, calldata("mid(address,uint8)", weth_meta["pool"], weth_meta["kind"]), block,
                                  {PROBE: {"code": probe_code(root)}}))[:2]
        p = mid_price(weth_meta["kind"], a, b, weth_meta["token0"] == USDC)  # USDC raw per WETH raw
        usd_per_raw[WETH] = p * 1e-6 if p else 0.0
    for q in quotes:
        if q in (USDC, WETH) or q in hops:
            continue
        meta, side = hop_search(rpc, q, factories, usd_per_raw, block)
        if meta:
            hops[q] = {**meta, "start": side}
    for q in hops:
        hops[q]["symbol"] = view_symbol(rpc, q, block)
    final = []
    for x in tokens:
        chain, q, ok = [], x["quote"], True
        while q != USDC:
            if q not in hops:
                ok = False
                break
            chain.append(q)
            q = hops[q]["start"]
        need = [x["quote"]] + [hops[c]["start"] for c in chain]
        if not ok:
            excluded.append({k: x[k] for k in ("token", "symbol", "pool", "dex")} | {"reason": "no_hop_to_usdc"})
        elif any(slots.get(s) is None for s in need):
            excluded.append({k: x[k] for k in ("token", "symbol", "pool", "dex")} | {"reason": "balance_slot_unconfirmed"})
        else:
            final.append({**x, "hops": chain})
    used_hops = sorted({c for x in final for c in x["hops"]})
    doc = {
        "card": "BL-45",
        "frozen_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_block": int(block, 16),
        "chain_id": CHAIN_ID,
        "source": f"{RPC} ({SOURCE}, state override; nothing broadcast)",
        "rule": (f"backtest/liquid/universe.json tokens whose deepest pool's median daily volume over the "
                 f"{RULE_DAYS} days before CUT {u['cut']} is >= {RULE_MIN_MEDIAN_VOL_USD} USD (no-trade day = 0); "
                 "minus any address in a data/daily/*/tasks.csv address column (seal A), Uniswap v4 and other "
                 "unsupported pools, stable curves, tokens without a hop to USDC or a confirmed balance slot"),
        "window": {"days": WINDOW_DAYS, "start": "the first run (backtest/costs/window.json, written once)",
                   "after": "the meter keeps running with this same list until a later story changes it"},
        "sizes_usd": list(SIZES),
        "probe": {"address": PROBE, "from": FROM, "solc": SOLC, "hex_sha256": probe_sha256(root)},
        "counts": {"rule": len(cands), "excluded": len(excluded), "metered": len(final),
                   "excluded_by_reason": _count(excluded)},
        "tokens": final,
        "excluded": sorted(excluded, key=lambda x: (x["reason"], x["token"])),
        "hops": {q: hops[q] for q in used_hops},
        "slots": {s: slots[s] for s in sorted({x["quote"] for x in final} | {hops[c]["start"] for c in used_hops})},
    }
    return doc


def _count(rows):
    out = {}
    for r in rows:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return dict(sorted(out.items()))


def probe_sha256(root="."):
    import hashlib
    return hashlib.sha256((Path(root) / PROBE_HEX).read_bytes()).hexdigest()


def load_tokens(root="."):
    return json.loads((Path(root) / TOKENS).read_text())


# ---- one run ----
def overrides(code, start, slot):
    return {PROBE: {"code": code}, start: {"stateDiff": {slot["key"]: "0x" + _word(FUND)}}}


def start_usd(doc, mids):
    """USD per raw unit of every start token, from this run's hop mids (never stored)."""
    out = {USDC: 1e-6}
    pending = dict(doc["hops"])
    for _ in range(len(pending) + 1):
        for q, h in list(pending.items()):
            if h["start"] in out:
                a, b = mids.get(h["pool"], (0, 0))
                p = mid_price(h["kind"], a, b, h["token0"] == h["start"])  # start raw per q raw
                out[q] = p * out[h["start"]] if p else None
                del pending[q]
    return out


def amount_in(size_usd, usd_per_raw):
    if not usd_per_raw:
        return None
    return int(size_usd / usd_per_raw)


def _row(ts, block, leg, token, symbol, start, pool, dex, fee_pct, size, costs, sim, reason, ms):
    c = costs or {}
    return {"ts": ts, "block": block, "leg": leg, "token": token, "symbol": symbol, "start": start, "pool": pool,
            "dex": dex, "fee_pct": fee_pct, "size_usd": size, "buy_pct": c.get("buy_pct"),
            "sell_pct": c.get("sell_pct"), "rt_pct": c.get("rt_pct"), "usdc_rt_pct": c.get("usdc_rt_pct"),
            "short_buy_pct": c.get("short_buy_pct"), "short_sell_pct": c.get("short_sell_pct"), "sim": sim,
            "reason": reason, "source": SOURCE, "latency_ms": ms}


def trip(rpc, code, block, pool, kind, start, other, token0, amount, slot):
    """One probe round trip. Returns (costs, sim, reason, latency_ms)."""
    data = calldata("trip(address,uint8,address,address,uint256)", pool, kind, start, other, amount)
    try:
        res = rpc.eth_call(PROBE, data, block, overrides(code, start, slot))
    except RpcError as e:
        return None, "error", "cap" if str(e) == "cap" else "rpc_error", rpc.last_ms
    w = words(res)
    if len(w) < 12:
        return None, "error", "rpc_error", rpc.last_ms
    stage, r = w[0], w[1:12]
    costs = trip_costs(kind, token0 == start, r)
    sim, reason = verdict(stage, costs)
    if stage != 0:  # a failed trip keeps only what it measured: the buy side when the sale reverted
        keep = ("buy_pct", "short_buy_pct") if stage == 3 else ()
        costs = {k: (v if k in keep else None) for k, v in costs.items()}
    return costs, sim, reason, rpc.last_ms


def run(rpc, root, out_dir, now=None, only=None):
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = load_tokens(root)
    code = probe_code(root)
    t0 = time.monotonic()
    rows = []
    block = rpc.call("eth_blockNumber", [])
    toks = [x for x in doc["tokens"] if not only or x["token"] in only or x["symbol"] in only]
    need = sorted({c for x in toks for c in x["hops"]})
    hop_pools = [doc["hops"][q] for q in need]
    mids = {}
    if hop_pools:
        res = rpc.eth_call(PROBE, "0x" + selector("mids(address[],uint8[])") + _encode_mids(hop_pools), block,
                           {PROBE: {"code": code}})
        w = words(res)
        n = len(hop_pools)
        vals = w[2:2 + 2 * n]  # offset, length, then (a, b) per pool
        for i, h in enumerate(hop_pools):
            mids[h["pool"]] = (vals[2 * i], vals[2 * i + 1])
    usd = start_usd({"hops": {q: doc["hops"][q] for q in need}}, mids)
    blk = int(block, 16)
    hop_rt = {}
    for q in need:
        h = doc["hops"][q]
        for size in SIZES:
            amt = amount_in(size, usd.get(h["start"]))
            if amt is None:
                costs, sim, reason, ms = None, "error", "no_price", 0
            else:
                costs, sim, reason, ms = trip(rpc, code, block, h["pool"], h["kind"], h["start"], q, h["token0"],
                                              amt, doc["slots"][h["start"]])
            hop_rt[(q, size)] = (costs or {}).get("rt_pct") if sim == "pass" else None
            rows.append(_row(ts, blk, "hop", q, h.get("symbol", ""), h["start"], h["pool"], h["dex"], h["fee_pct"], size, costs,
                             sim, reason, ms))
    for x in toks:
        for size in SIZES:
            amt = amount_in(size, usd.get(x["quote"]))
            if amt is None:
                costs, sim, reason, ms = None, "error", "no_price", 0
            else:
                costs, sim, reason, ms = trip(rpc, code, block, x["pool"], x["kind"], x["quote"], x["token"],
                                              x["token0"], amt, doc["slots"][x["quote"]])
            if costs is not None:
                chain = [costs.get("rt_pct") if sim == "pass" else None] + [hop_rt.get((c, size)) for c in x["hops"]]
                costs["usdc_rt_pct"] = combine(chain)
            rows.append(_row(ts, blk, "token", x["token"], x["symbol"], x["quote"], x["pool"], x["dex"],
                             x["fee_pct"], size, costs, sim, reason, ms))
    write_rows(out_dir, now, rows)
    window_once(out_dir, now)
    return {"ts": ts, "block": blk, "rows": len(rows), "calls": rpc.calls, "refused": rpc.refused,
            "seconds": round(time.monotonic() - t0, 1), "capped": rpc.capped()}


def _encode_mids(pools):
    n = len(pools)
    head = _word(64) + _word(64 + 32 * (n + 1))
    a = _word(n) + "".join(_word(h["pool"]) for h in pools)
    k = _word(n) + "".join(_word(h["kind"]) for h in pools)
    return head + a + k


def write_rows(out_dir, now, rows):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now.strftime('%Y-%m-%d')}.csv"
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in COLUMNS})
    return path


def window_once(out_dir, now):
    """The 7-day window starts at the first run: written once, never rewritten."""
    p = Path(out_dir) / "window.json"
    if p.exists():
        return False
    start = now.replace(microsecond=0)
    p.write_text(json.dumps({"card": "BL-45", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "end": (start + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "days": WINDOW_DAYS}, indent=1) + "\n")
    return True


# ---- summary (evidence only) ----
def p90(xs):
    s = sorted(xs)
    return s[max(0, -(-9 * len(s) // 10) - 1)]


def read_rows(out_dir):
    rows = []
    for p in sorted(Path(out_dir).glob("*.csv")):
        with open(p, newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def summary(out_dir, all_rows=False):
    out_dir = Path(out_dir)
    rows = read_rows(out_dir)
    win = None
    if (out_dir / "window.json").exists():
        win = json.loads((out_dir / "window.json").read_text())
        if not all_rows:
            rows = [r for r in rows if win["start"] <= r["ts"] < win["end"]]
    groups = {}
    for r in rows:
        groups.setdefault((r["leg"], r["token"], r["symbol"], int(r["size_usd"])), []).append(r)
    table = []
    for (leg, token, symbol, size), rs in sorted(groups.items(), key=lambda k: (k[0][0] != "token", k[0][2], k[0][1], k[0][3])):
        tested = [r for r in rs if r["sim"] in ("pass", "fail")]
        key = "usdc_rt_pct" if leg == "token" else "rt_pct"
        vals = [float(r[key]) for r in tested if r["sim"] == "pass" and r[key] != ""]
        fails = [r for r in tested if r["sim"] == "fail"]
        table.append({"leg": leg, "token": token, "symbol": symbol or token[:10], "size_usd": size,
                      "n": len(tested), "errors": len(rs) - len(tested),
                      "median_rt_pct": round(statistics.median(vals), 3) if vals else None,
                      "p90_rt_pct": round(p90(vals), 3) if vals else None,
                      "fail_rate": round(len(fails) / len(tested), 3) if tested else None,
                      "fail_reasons": _count(fails)})
    return {"window": win, "all": all_rows, "rows": len(rows), "runs": len({r["ts"] for r in rows}),
            "table": table}


def render_summary(s):
    lines = []
    w = s["window"]
    scope = "all rows" if s["all"] or not w else f"window {w['start']} .. {w['end']}"
    lines.append(f"BL-45 cost meter summary ({scope}): {s['runs']} runs, {s['rows']} rows. Evidence only.")
    lines.append("Round trip % = token legs: from USDC through every hop (usdc_rt_pct); hop legs: the hop alone.")
    lines.append(f"{'leg':<6}{'symbol':<16}{'size':>5}{'n':>5}{'err':>5}{'median %':>10}{'p90 %':>9}{'fail':>7}  reasons")
    for r in s["table"]:
        f = lambda v: "na" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)
        reasons = " ".join(f"{k}={v}" for k, v in r["fail_reasons"].items())
        lines.append(f"{r['leg']:<6}{r['symbol'][:15]:<16}{r['size_usd']:>5}{r['n']:>5}{r['errors']:>5}"
                     f"{f(r['median_rt_pct']):>10}{f(r['p90_rt_pct']):>9}{f(r['fail_rate']):>7}  {reasons}")
    return "\n".join(lines)


# ---- probe build (reproducible; docs/NOTES.md "BL-45") ----
def probe_input(root="."):
    src = (Path(root) / PROBE_SOL).read_text()
    return {"language": "Solidity", "sources": {"costprobe.sol": {"content": src}},
            "settings": {"optimizer": {"enabled": True, "runs": 200}, "evmVersion": "cancun",
                         "outputSelection": {"*": {"CostProbe": ["evm.deployedBytecode.object"]}}}}


def probe_output(text):
    """solcjs prints a notice line before the JSON; read from the first '{'."""
    o = json.loads(text[text.index("{"):])
    errs = [e for e in o.get("errors", []) if e.get("severity") == "error"]
    if errs:
        raise SystemExit("solc: " + errs[0].get("formattedMessage", "error"))
    return o["contracts"]["costprobe.sol"]["CostProbe"]["evm"]["deployedBytecode"]["object"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.costs")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--freeze", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--summary", action="store_true")
    g.add_argument("--probe-input", action="store_true")
    g.add_argument("--probe-output", action="store_true")
    ap.add_argument("--out", default=DIR)
    ap.add_argument("--tokens", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.probe_input:
        print(json.dumps(probe_input(root)))
        return 0
    if a.probe_output:
        (root / PROBE_HEX).write_text(probe_output(sys.stdin.read()) + "\n")
        print(f"{PROBE_HEX} sha256 {probe_sha256(root)}")
        return 0
    if a.summary:
        print(render_summary(summary(root / a.out if not Path(a.out).is_absolute() else Path(a.out), a.all)))
        return 0
    if a.freeze:
        rpc = Rpc(max_calls=2000)
        doc = freeze(rpc, root)
        (root / DIR).mkdir(parents=True, exist_ok=True)
        (root / TOKENS).write_text(json.dumps(doc, indent=1) + "\n")
        print(f"frozen: rule {doc['counts']['rule']}, metered {doc['counts']['metered']}, excluded "
              f"{doc['counts']['excluded_by_reason']}; calls={rpc.calls} refused={rpc.refused}")
        return 0
    rpc = Rpc()
    only = {t.strip().lower() if t.strip().startswith("0x") else t.strip() for t in a.tokens.split(",") if t.strip()}
    out = Path(a.out) if Path(a.out).is_absolute() else root / a.out
    try:
        res = run(rpc, root, out, only=only or None)
    except RpcError as e:
        print(f"costs=error:{e} calls={rpc.calls} refused={rpc.refused}")
        return 1
    print(f"costs=ok rows={res['rows']} calls={res['calls']} refused={res['refused']} seconds={res['seconds']}"
          f" block={res['block']} capped={res['capped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
