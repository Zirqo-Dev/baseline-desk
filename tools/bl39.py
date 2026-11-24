"""BL-39 run tool: spec v2.1 "MAX momentum" (backtest/specs/bl39-max-momentum-v2.1.md), by hand only, offline.

    python -m tools.bl39 --check [--stage 1|2]   hashes, meta headers and timestamps only; no price, return, MAX or pick
    python -m tools.bl39 --run --stage 1         THE run of a stage (once): writes backtest/results/<stage>/ first

Never on the tick path: core/ never imports tools/ and never reads backtest/. Stdlib only, no network module is
imported (test). It encodes the frozen text rule for rule; every block below quotes the spec line it implements.
Where the text is unclear the tool does not choose: each such point is a CHOICES key, every option is built and
tested, and the real run is refused while any key is None (unresolved). A later story fills CHOICES from the owner's
answers and records them in docs/DECISIONS.md.

Refusals, in this order, the first failing step is the reason (refusal()), all before any candle, ETH or universe
file is opened beyond hashing: 1 the sha256 of the spec, its two data files, the survey files, the ETH file and the
Q9s cost file must equal PINS, and the family rule must give exactly the family list on the two spec data files;
2 a docs/DECISIONS.md row (card BL-39) starting "stage N run approved" and marked "(the owner)"; 3 that row names
"tools/bl39.py sha256 <the first 12 hex of this file's sha256>" (tool pin); 4 for stage 2 a row starting "stage 1 pass";
5 for stage 2 the stage 1 result's summary.json exists and records a pass (read only at this step); 6 no result
folder for the stage (no override; Q12); 7 no attempt record for the stage (an aborted attempt: a retry is a rerun,
its own story); 8 no unresolved CHOICES. run() then writes the attempt record before the first data read.

Peek guard (the owner, 2026-11-24, A): load(), evaluate(), simulate() and the readers refuse the real committed files
(a pinned path, anything under the repo's backtest/liquid/, or any file whose bytes hash to a pin) unless run() has
opened its private in-process gate for that root and stage, which it does only after refusal() passed and the attempt
record is written, and closes in a finally on every exit path. Synthetic fixtures in temp dirs are not guarded.

Seal (the owner, A): stage 1 keeps the full daily candles that start before 2026-09-07 and, of the 2026-09-07 candle,
the open only (each token and ETH); its high, low, close and volume and every later candle are dropped as each file
line is parsed and never reach the run (read_daily()).
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400

SPEC = "backtest/specs/bl39-max-momentum-v2.1.md"
FAMILY = "backtest/specs/bl39-max-momentum-v2.1-family.json"
FEES = "backtest/specs/bl39-max-momentum-v2.1-fees.json"
UNIVERSE = "backtest/liquid/universe.json"
DAILY = "backtest/liquid/daily.jsonl.gz"
ETH = "backtest/liquid/eth-daily.jsonl.gz"
DECISIONS = "docs/DECISIONS.md"
RESULTS = "backtest/results"
COST = "backtest/specs/bl39-max-momentum-v2.1-bl45-cost.json"
TOOL = "tools/bl39.py"
NAME = "bl39-max-momentum-v2.1"

# The spec, its two data files (DECISIONS 2026-11-20 (f), the spec's DATA line) and the ETH file (NOTES "BL-39 ETH
# daily"). The survey files: NOTES "BL-46" records that their sha256 is pinned in tests/test_snapshot.py; these are
# those pins (NOTES itself does not write the hash). The Q9s cost file: frozen 2026-11-24 from NOTES "BL-39 pre-freeze"
# (the owner, Q9s), read only for the Q9 report line.
PINS = {
    SPEC: "5202982dbe901b5fc7b77956ef322ef5770cc05f863acd1d878d749c9a2b53ba",
    FAMILY: "374b70eaf450e03d6a2bc208708fa1d3afcdaad77d080c2639aa39a00becc9ab",
    FEES: "4a7318c95a9650f5fa533ba0f9ac552db0b0449d7a9e8e22bca6797b16991180",
    ETH: "82bc487a78a2f0306e56b1b1780b4bdab91b3d1588328c4edd3ece0f79828ac8",
    DAILY: "5d14fcee0fca60ac5f71f616acb6869a0cb5489cf99fc9d24d688aa6f6f71267",
    UNIVERSE: "09c301d0ed8b5fbe5d4365659b950749d085fbec192be44dd8226e760e9b695a",
    COST: "71d71de47e1d39b893c469f4ffa7f81be3ae595e4f627238e68301fe75d98566",
}
_REAL_PINS = dict(PINS)  # the peek guard's copy: patching PINS (tests) never makes a committed file look synthetic

# "GoPlus creator_address = 0xd95a366a2c887033ba71743c6342e2df470e9db9, or the deepest pool's other token is ADS
#  0xb20a4bd059f5914a2f8b9c18881c637f79efb7df"
CREATOR = "0xd95a366a2c887033ba71743c6342e2df470e9db9"
ADS = "0xb20a4bd059f5914a2f8b9c18881c637f79efb7df"
ZERO = "0x0000000000000000000000000000000000000000"
FAMILY_COUNT = 39  # "the 39 token addresses in FAMILY LIST"


def _ts(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _iso_ts(s):
    return int(datetime.strptime(s.replace(".000", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mondays(first, count):
    return [_ts(first) + 7 * DAY * i for i in range(count)]


# "STAGE 1 = Mondays 2026-06-15 .. 2026-08-31 (12)"; "STAGE 2 = Mondays 2026-09-07 .. 2026-10-26 (8)"
# "(1) closed legs >= 40 in stage 1, >= 30 in stage 2"; "(3) ... in at least 8 of 12 weeks (stage 1) / 6 of 8 weeks"
# full_before / open_until = the load window (read_daily): full candles start < full_before; from full_before to
# open_until only the open. Stage 1: "The stage 1 run reads nothing after the open of the 2026-09-07 candle" and "For
# stage 1's last Monday no later open is searched". Stage 2's last legs may search 3 days past 2026-11-02 (the LEG
# rule), so it keeps opens to 2026-11-05; nothing at or after CUT exists in the files.
STAGES = {
    1: {"mondays": _mondays("2026-06-15", 12), "min_legs": 40, "weeks_needed": 8,
        "full_before": _ts("2026-09-07"), "open_until": _ts("2026-09-07"), "last_search_d": 0},
    2: {"mondays": _mondays("2026-09-07", 8), "min_legs": 30, "weeks_needed": 6,
        "full_before": _ts("2026-11-02"), "open_until": _ts("2026-11-05"), "last_search_d": 3},
}
assert len(STAGES[1]["mondays"]) == 12 and _day(STAGES[1]["mondays"][-1]) == "2026-08-31"
assert len(STAGES[2]["mondays"]) == 8 and _day(STAGES[2]["mondays"][-1]) == "2026-10-26"

# ---- open questions: the tool does not choose; the owner answered them all (chat, 2026-11-24, DECISIONS) ----
OPTIONS = {
    "Q1": ("pick_leg", "any_leg"),
    "Q2": ("search", "close"),
    "Q3": ("picked_weeks", "zero_diff", "cash_zero"),
    "Q4": ("inconclusive", "drawdown"),
    "Q5": ("open", "fill_picks", "fill_both"),
    "Q6": ("open_to_open", "close_to_close", "open_to_close"),
    "Q7": ("simple_ceil", "simple_floor", "log_ceil", "log_floor"),
    "Q8": ("pool_only", "pool_or_hop"),
    "Q9": ("replace_metered", "metered_legs_only", "side_by_side"),
    "Q9s": ("pinned_file",),  # the frozen COST file (PINS): NOTES "BL-39 pre-freeze", 5 USD round trip / 2
    "Q10": ("compute", "na_first"),
    "Q11": ("fail", "not_evaluable"),
    "Q12": ("no_rerun",),  # the one built option = today's behaviour; the DECISIONS-line rerun path is not built
}
# Options named in the open questions but not built in this story (a later story builds the chosen one).
NOT_BUILT = {
    "Q12": ("decisions_rerun: a docs/DECISIONS.md row 'stage N rerun approved ... (the owner)' lets a second run write "
            "its own rerun folder, logged as a rerun, the first result never replaced",),
}
QUESTIONS = {
    "Q1": "STOP/PICKS 'A token stopped in the week before m ... is not eligible at m': stopped = its PICK leg hit the "
          "stop (pick_leg), or ANY leg of it hit the stop, i.e. its benchmark leg, so also tokens not picked (any_leg)?",
    "Q2": "ROLL 'If that candle is missing, the old leg ends by the exit_gap rule (a real sell)': the whole LEG fallback "
          "(first open in the next 3 days, else the last close) (search), or straight to the close of the leg's last "
          "candle (close)?",
    "Q3": "PASS (3) 'the mean of (P_w - B_w)': over the weeks with a closed pick leg only (picked_weeks), or over every "
          "week with P_w - B_w = 0 for a week without one (zero_diff), or with P_w = 0, cash (cash_zero)?",
    "Q4": "OUTCOME when tests 1 AND 5 fail and 2-4 pass: 'inconclusive = not passed' (inconclusive) or 'not passed: "
          "drawdown', forward paper (drawdown)? (stage 2 is not run either way)",
    "Q5": "STOP 'the entry fill for a new position': the open of the m candle (open), or the open plus the buy friction "
          "(core's fill convention) for pick legs with benchmark legs at the open as they are gross (fill_picks), or for "
          "both books (fill_both)? It moves the -30% level.",
    "Q6": "REPORT 'by ETH 7-day return before m': ETH open(m - 7 d) -> open(m) (open_to_open), close(m - 8 d) -> "
          "close(m - 1 d) (close_to_close), or open(m - 7 d) -> close(m - 1 d) (open_to_close)?",
    "Q7": "REPORT 'the top third of the universe by 30-day volatility': the stdev of the 30 daily returns behind MAX "
          "as simple returns or as log returns, and the third rounded up or down (ties by lower address, as PICKS)? "
          "Sample or population stdev rank the same over 30 returns, so that is not a question.",
    "Q8": "REPORT 'legs on unknown-fee, hook and changeable-fee pools': the deepest pool's flags only (unknown, a "
          "non-zero hook, may_differ) (pool_only), or also a route hop of unknown fee (pool_or_hop)?",
    "Q9": "REPORT 'the BL-45 measured-cost line': net results with the measured per-side cost in place of the spec "
          "friction for metered tokens, other legs at spec friction (replace_metered); the same over metered-token legs "
          "only (metered_legs_only); or no recompute, the spec friction and the measured cost side by side per picked "
          "metered token (side_by_side)?",
    "Q9s": "REPORT the BL-45 measured cost's source for the real run: which BL-45 cost-meter file and which "
           "statistic (window, size, median or other)? pinned_file = the frozen COST file under backtest/specs/ (the "
           "14-token table of NOTES 'BL-39 pre-freeze', 5 USD round trip / 2 per side, window Nov 13-20); this tool "
           "never opens the BL-45 cost-meter files.",
    "Q10": "REPORT 'picks that joined the universe in the last 7 days' on stage 1's first Monday (Jun 15): its m - 7 d "
           "universe (Jun 8) needs candles from May 8, before most pools' first candle (May 9): compute it by the same "
           "rule anyway (compute) or report Jun 15 as not available (na_first)?",
    "Q11": "PASS (2) and (4) with too few legs (no closed leg; or 2 or fewer, so the mean 'without the top 2 legs' has "
           "no leg): the test fails (fail) or is not evaluable, which makes the stage inconclusive when 1 also fails "
           "(not_evaluable)?",
    "Q12": "RUN 'A second run of a stage needs an owner rerun line in DECISIONS, is logged as a rerun and never replaces "
           "the first result': no rerun path in this tool, any rerun is its own story (no_rerun, built: the run-once "
           "guard has no override), or a DECISIONS-line rerun path (decisions_rerun, not built)?",
}
# The owner's answers (chat, 2026-11-24, all choice A; docs/DECISIONS.md). Q12 no_rerun: the first result is the
# verdict; any rerun is its own reviewed story, logged as a rerun.
CHOICES = {"Q1": "pick_leg", "Q2": "close", "Q3": "zero_diff", "Q4": "inconclusive", "Q5": "fill_picks",
           "Q6": "open_to_open", "Q7": "simple_ceil", "Q8": "pool_or_hop", "Q9": "replace_metered",
           "Q9s": "pinned_file", "Q10": "na_first", "Q11": "fail", "Q12": "no_rerun"}


class Refused(Exception):
    pass


class SealError(Exception):
    pass


def unresolved(choices):
    return [q for q in OPTIONS if choices.get(q) not in OPTIONS[q]]


# ---- hashes, family, decisions, run-once ----
def sha256(path):
    """The only place a data file is opened before the refusal checks pass: raw bytes into the hash, nothing parsed."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_refusals(root, pins=None):
    pins = PINS if pins is None else pins
    out = []
    for rel, want in pins.items():
        p = Path(root) / rel
        if not p.exists():
            out.append(f"{rel} missing")
        else:
            got = sha256(p)
            if got != want:
                out.append(f"{rel} sha256 {got[:12]} is not the pinned {want[:12]}")
    return out


def family_set(family, fees):
    """The ADS family to exclude, checked offline on the committed files (the owner's amendment 3).

    Spec UNIVERSE: "EXCLUDED: the ADS family = the 39 token addresses in FAMILY LIST ... GoPlus creator_address =
    0xd95a..., or the deepest pool's other token is ADS 0xb20a...; a token with no GoPlus creator answer is a member
    only if its deepest pool is ADS-quoted; plus ADS itself (not in universe.json, removes nothing). The run uses the
    list, never a live GoPlus call; the code asserts that the rule gives exactly the list."
    Re-derived here: the ADS-quoted half of the rule, from the fee file's other_token (token0/token1 at the block).
    Pinned by hash only, not re-derived: the GoPlus creator half (the family file's own "checks" record it)."""
    members = {m["token"].lower() for m in family["members"]}
    why = []
    if family.get("ads", "").lower() != ADS:
        why.append("family file ads is not the spec's ADS")
    if family.get("creator", "").lower() != CREATOR:
        why.append("family file creator is not the spec's creator")
    if len(members) != FAMILY_COUNT or family.get("count") != FAMILY_COUNT:
        why.append(f"family list has {len(members)} tokens, the spec says {FAMILY_COUNT}")
    tok = {t["token"].lower(): t for t in fees["tokens"]}
    ads_quoted = {a for a, t in tok.items() if (t.get("other_token") or "").lower() == ADS}
    flagged = {a for a, t in tok.items() if t.get("family")}
    if ads_quoted != members:
        why.append(f"ADS-quoted tokens in the fee file ({len(ads_quoted)}) are not the family list "
                   f"({len(ads_quoted ^ members)} differ)")
    if flagged != members:
        why.append("fee file family flags are not the family list")
    for m in family["members"]:
        t = tok.get(m["token"].lower())
        if t is None or t["pool"].lower() != m["pool"].lower():
            why.append(f"family member {m['token'][:10]} pool is not the fee file's")
    if ADS in tok:
        why.append("ADS is in the fee file (the spec: not in universe.json, removes nothing)")
    if why:
        raise Refused("family check: " + "; ".join(why))
    return members | {ADS}


def decision_lines(root, start):
    """Every docs/DECISIONS.md table row whose card cell is BL-39, whose decision cell starts with `start` (case
    ignored) and which is marked "(the owner)"."""
    p = Path(root) / DECISIONS
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 5 and cells[2] == "BL-39" and cells[3].lower().startswith(start.lower()) \
                and "(the owner)" in line:
            out.append(line)
    return out


def decision_line(root, start):
    """The first such row; "" when there is none."""
    rows = decision_lines(root, start)
    return rows[0] if rows else ""


def stage1_pass_line(root):
    """Spec RUN: "Stage 2 runs only after a DECISIONS line records 'stage 1 pass'"."""
    return decision_line(root, "stage 1 pass")


def run_approved_line(root, stage):
    """The owner's run gate (2026-11-23, A): --run --stage N needs a row starting "stage N run approved"."""
    return decision_line(root, f"stage {stage} run approved")


def result_dir(root, stage):
    return Path(root) / RESULTS / f"{NAME}-stage{stage}"


def attempt_path(root, stage):
    """The attempt record, written by run() after the gate and before the first data read (beside the folder)."""
    return Path(root) / RESULTS / f"{NAME}-stage{stage}.attempt.json"


def tool_sha():
    """sha256 of this file (tools/bl39.py) as it runs now."""
    return sha256(Path(__file__).resolve())


TOOL_PIN = re.compile(r"tools/bl39\.py sha256 ([0-9a-f]{12,64})\b")


def tool_pin_refusal(row):
    """Tool pin (the owner, 2026-11-24, A): the run-approval row names "tools/bl39.py sha256 <hex>", at least the first
    12 hex of the tool's current sha256; a row naming none, or another hash, refuses."""
    cells = [c.strip() for c in row.split("|")]
    named = TOOL_PIN.findall(cells[3] if len(cells) > 3 else row)
    have = tool_sha()
    if any(have.startswith(n) for n in named):
        return ""
    if named:
        return (f"refused: tool pin: the approval row names tools/bl39.py sha256 {', '.join(n[:12] for n in named)}, "
                f"the tool is {have[:12]}")
    return f"refused: tool pin: the approval row names no tools/bl39.py sha256 (the tool is {have[:12]})"


def stage1_summary_refusal(root):
    """Stage 2 only, after the "stage 1 pass" row: the stage 1 result's summary.json exists and records a pass."""
    p = result_dir(root, 1) / "summary.json"
    rel = p.relative_to(root)
    if not p.exists():
        return f"refused: stage 2 needs {rel} (the stage 1 result), which does not exist"
    try:
        s = json.loads(p.read_text())
    except ValueError:
        return f"refused: stage 2 needs {rel} to be readable JSON"
    if not isinstance(s, dict) or s.get("stage") != 1 or s.get("verdict") != "pass":
        v = s.get("verdict") if isinstance(s, dict) else None
        return f"refused: stage 2 needs {rel} to record a stage 1 pass; it records verdict {v!r}"
    return ""


def refusal(root, stage, pins=None, choices=None):
    """"" when the run of `stage` may start, else the reason of the FIRST failing step (the owner's order, 2026-11-23,
    with the 2026-11-24 steps slotted in): 1 spec and file hashes (raw bytes only), then the family check on the two
    spec data files; 2 the run-approval line; 3 that line's tool pin; 4 (stage 2) the "stage 1 pass" line; 5 (stage 2)
    the stage 1 summary.json records a pass; 6 no result folder; 7 no attempt record; 8 no unresolved CHOICES. No
    candle, ETH, universe or cost file is opened beyond hashing and nothing is created (test, with an open() spy)."""
    choices = CHOICES if choices is None else choices
    if stage not in STAGES:
        return f"refused: no stage {stage}"
    why = hash_refusals(root, pins)
    if why:
        return "refused: hashes: " + "; ".join(why)
    try:
        family_set(json.loads((Path(root) / FAMILY).read_text()), json.loads((Path(root) / FEES).read_text()))
    except Refused as e:
        return f"refused: {e}"
    rows = decision_lines(root, f"stage {stage} run approved")
    if not rows:
        return f"refused: no docs/DECISIONS.md BL-39 row starting 'stage {stage} run approved' (the owner)"
    pins_why = [tool_pin_refusal(r) for r in rows]
    if all(pins_why):
        return pins_why[0]
    if stage == 2 and not stage1_pass_line(root):
        return "refused: stage 2 needs a docs/DECISIONS.md BL-39 row starting 'stage 1 pass' (the owner)"
    if stage == 2:
        why = stage1_summary_refusal(root)
        if why:
            return why
    if result_dir(root, stage).exists():
        return f"refused: {result_dir(root, stage).relative_to(root)} exists: a stage runs once (no override)"
    if attempt_path(root, stage).exists():
        return (f"refused: an aborted attempt exists ({attempt_path(root, stage).relative_to(root)}, no result "
                "folder): a retry is a rerun, its own story")
    open_q = unresolved(choices)
    if open_q:
        return "refused: open spec questions unresolved (CHOICES): " + " ".join(open_q)
    return ""


# ---- peek guard: the committed files are read only inside run(), after its gate (the owner, 2026-11-24, A) ----
_GATE = None  # (resolved root, stage) while run() is between its attempt record and its exit; private, no CLI flag


class _Loaded(dict):
    """A dict that load() returns; .real_root is set when it came from the real committed files."""
    real_root = None


def _is_real(path):
    """A pinned committed path, anything under the repo's backtest/liquid/, or any file whose bytes hash to a pin."""
    p = Path(path).resolve()
    if any(p == (ROOT / rel).resolve() for rel in _REAL_PINS):
        return True
    if (ROOT / "backtest/liquid").resolve() in p.parents:
        return True
    return p.is_file() and sha256(p) in _REAL_PINS.values()


def _gate_open(root, stage):
    return _GATE is not None and _GATE == (Path(root).resolve(), stage)


def _peek_refused(what, stage):
    return Refused(f"refused: peek guard: {what} reads the committed files only inside run(), after the stage {stage} "
                   "run gate passed (python -m tools.bl39 --check --stage N says why a run is refused now)")


def _guard_path(path, what, window=None):
    """Readers: a real file only while run()'s gate is open, for a file under the gate's root and, for read_daily, the
    gate stage's own load window."""
    if not _is_real(path):
        return
    ok = _GATE is not None and _GATE[0] in Path(path).resolve().parents
    if ok and window is not None:
        cfg = STAGES[_GATE[1]]
        ok = window == (cfg["full_before"], cfg["open_until"])
    if not ok:
        raise _peek_refused(what, _GATE[1] if _GATE else "N")


def _guard_data(stage, *xs):
    """evaluate() and simulate(): data that load() took from the real files only while the gate is open for it."""
    for x in xs:
        r = getattr(x, "real_root", None)
        if r is not None and not _gate_open(r, stage):
            raise _peek_refused("evaluate()/simulate()", stage)


# ---- readers ----
def read_meta(path):
    """Line 1 (the meta header) of a candle file only."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        x = json.loads(fh.readline())
    return x if x.get("kind") == "meta" else {}


def read_daily(path, full_before, open_until):
    """{token: {"pool", "symbol", "c": {t: (o, h, l, c, v)}}}: candles that start before full_before whole; from
    full_before to open_until (inclusive) the open only, as (o, None, None, None, None); every later candle and the
    other fields of an open-only candle are dropped as each line is parsed (the seal, the owner's amendment 1).
    tools/candles.py read() would hand over every candle of the file, so it is not reused here."""
    _guard_path(path, "read_daily()", (full_before, open_until))
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            x = json.loads(line)
            if x.get("kind") == "meta":
                continue
            cs = {}
            for c in x.get("candles") or []:
                t = int(c[0])
                if t < full_before:
                    cs[t] = (float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
                elif t <= open_until:
                    cs[t] = (float(c[1]), None, None, None, None)
            x["candles"] = None
            out[x["token"].lower()] = {"pool": (x.get("pool") or "").lower(), "symbol": x.get("symbol", ""), "c": cs}
    return out


def read_universe(path):
    """universe.json: token, pool and pool_created_at only (no reserve)."""
    _guard_path(path, "read_universe()")
    u = json.loads(Path(path).read_text())
    return {t["token"].lower(): {"pool": t["pool"].lower(), "created": _iso_ts(t["pool_created_at"])
                                 if t.get("pool_created_at") else None, "symbol": t.get("symbol", "")}
            for t in u["tokens"]}


def read_cost(path):
    """The Q9s cost file: {token: per-side %}, for the Q9 report line only, never the verdict."""
    _guard_path(path, "read_cost()")
    return {t["token"].lower(): float(t["per_side_pct"]) for t in json.loads(Path(path).read_text())["tokens"]}


# ---- friction ----
def _pct(s):
    return None if s is None else float(s)


def route(fee):
    """FRICTION: "per real buy or sell: the fee tiers of the route from USDC (the deepest pool plus each hop, by BL-45's
    hop rule) + 0.5%. Unknown fee (...: hook-set per swap or unreadable) = 1%. No impact term; gas is inside the 0.5%."
    Returns (percent per side, flags)."""
    parts = [1.0 if fee.get("unknown") or fee.get("fee_pct") is None else _pct(fee["fee_pct"])]
    for h in fee.get("hops") or []:
        parts.append(1.0 if h.get("unknown") or h.get("fee_pct") is None else _pct(h["fee_pct"]))
    hooks = fee.get("hooks")
    flags = {"pool_unknown": bool(fee.get("unknown")), "hook": bool(hooks) and hooks.lower() != ZERO,
             "may_differ": bool(fee.get("may_differ")),
             "hop_unknown": any(h.get("unknown") for h in fee.get("hops") or []),
             "pool_fee": None if fee.get("unknown") else _pct(fee.get("fee_pct"))}
    return sum(parts) + 0.5, flags


def fee_table(fees):
    return {t["token"].lower(): dict(zip(("pct", "flags"), route(t)), pool=t["pool"].lower()) for t in fees["tokens"]}


# ---- universe and signal ----
def universe_at(m, data, uni, family):
    """UNIVERSE at m (a missing number = not in): "universe.json tokens only ...; deepest pool created at or before
    m - 31 d ...; all 31 daily candles in [m - 31 d, m); median daily USD volume of the deepest pool over [m - 30 d, m)
    >= 50,000; that pool's USD volume >= 10,000 on every day in [m - 7 d, m); EXCLUDED: the ADS family".
    SIGNAL: "MAX = the largest of the 30 close-to-close daily returns in [m - 31 d, m)."
    Returns ({token: 30 returns} after the family exclusion, the set before it). A close <= 0 makes a return
    undefined: a missing number, so not in."""
    kept, before = {}, set()
    days = [m - (31 - i) * DAY for i in range(31)]
    for a, info in uni.items():
        cr = info["created"]
        if cr is None or cr > m - 31 * DAY:
            continue
        cs = (data.get(a) or {}).get("c") or {}
        rows = [cs.get(t) for t in days]
        if any(r is None or r[3] is None for r in rows):
            continue
        vols = [r[4] for r in rows[1:]]  # [m - 30 d, m)
        if statistics.median(vols) < 50_000 or any(v < 10_000 for v in vols[-7:]):
            continue
        closes = [r[3] for r in rows]
        if any(c <= 0 for c in closes):
            continue
        before.add(a)
        if a in family:
            continue
        kept[a] = [closes[i] / closes[i - 1] - 1 for i in range(1, 31)]
    return kept, before


def rank(rets):
    """PICKS: "the highest MAX; ties by lower address (lowercase hex)"."""
    return sorted(rets, key=lambda a: (-max(rets[a]), a))


# ---- legs ----
def week_stop(cs, m, ref):
    """STOP: "-30% from the leg's starting price ..., checked on every daily candle of the leg, the first day included.
    A low at or below it exits at the lowest of the stop level, that day's open and that day's close."
    The leg's candles are those in [m, m + 7 d): a later exit (LEG's 3-day search) only happens when the m + 7 d candle
    and the days before the found open have no candle. Returns (t, price) or None."""
    level = 0.7 * ref
    for d in range(7):
        r = cs.get(m + d * DAY)
        if r is None:
            continue
        if r[2] is None:
            raise SealError(f"open-only candle {_day(m + d * DAY)} inside a leg week")
        if r[2] <= level:
            return m + d * DAY, min(level, r[0], r[3])
    return None


def leg_end(cs, m, search_d):
    """LEG: "ends at the open of the candle that starts at m + 7 d (if missing, the open of the first candle in the next
    3 days; else the close of the leg's last candle = exit_gap). For stage 1's last Monday no later open is searched: a
    missing 2026-09-07 candle = exit_gap." Returns (how, t, price, at_close)."""
    t7 = m + 7 * DAY
    for d in range(0, search_d + 1):
        if t7 + d * DAY in cs:
            return "exit", t7 + d * DAY, cs[t7 + d * DAY][0], False
    last = max(t for t in cs if m <= t < t7)
    return "exit_gap", last, cs[last][3], True


def book(mondays, stage, data, select, side_pct, choices, kind, rebuy=False):
    """One book (picks or benchmark) over a stage's Mondays. select(i, m, stopped_last_week) -> (ordered tokens, info).

    LEG: "one pick for one week. It starts at the open of the daily candle that starts at m (no candle that day =
    no_fill, counted) ... A leg that ends by exit, stop, roll or exit_gap is a closed leg; a no_fill is not a leg."
    ROLL: "a token picked again at m + 7 d in the same stage is not sold: its leg ends and the next begins at the open
    of the candle that starts at m + 7 d, with no friction on either side. If that candle is missing, the old leg ends
    by the exit_gap rule (a real sell) and the new pick is a no_fill. No roll across a stage end: a stage's last-Monday
    legs end with a real sell". rebuy=True is v1's line ("re-buy every week"): a roll is a real sell and a real buy."""
    cfg = STAGES[stage]
    legs, weeks, open_, stopped = [], [], {}, set()

    def close(L, how, t, px, at_close, sell):
        L.update(end=how, end_t=t, end_px=px, end_at_close=at_close, sell=sell)
        legs.append(L)

    for i, m in enumerate(mondays):
        chosen, info = select(i, m, stopped)
        chosen_set, rolled_in, nofill = set(chosen), set(), []
        for a in sorted(open_):
            L, cs = open_.pop(a), data[a]["c"]
            if a in chosen_set:
                if m in cs:
                    close(L, "roll", m, cs[m][0], False, rebuy)
                    rolled_in.add(a)
                elif choices["Q2"] == "search":
                    close(L, *leg_end(cs, L["start_t"], 3), True)
                else:
                    t = max(x for x in cs if L["start_t"] <= x < m)
                    close(L, "exit_gap", t, cs[t][3], True, True)
            else:
                close(L, *leg_end(cs, L["start_t"], 3), True)
        stopped_now = set()
        for a in chosen:
            cs = data[a]["c"]
            if m not in cs:
                nofill.append(a)
                continue
            o = cs[m][0]
            if o is None or cs[m][2] is None:
                raise SealError(f"open-only candle {_day(m)} at a leg start")
            buy = rebuy or a not in rolled_in
            fill_stop = choices["Q5"] == "fill_both" or (choices["Q5"] == "fill_picks" and kind == "pick")
            ref = o * (1 + side_pct(a) / 100) if (buy and fill_stop) else o
            L = {"book": kind, "token": a, "monday": m, "start_t": m, "start_px": o, "stop_ref": ref,
                 "rolled": a in rolled_in, "buy": buy}
            hit = week_stop(cs, m, ref)
            if hit:
                close(L, "stop", hit[0], hit[1], True, True)
                stopped_now.add(a)
            else:
                open_[a] = L
        weeks.append(dict(info, monday=m, chosen=list(chosen), no_fill=nofill, stopped=sorted(stopped_now)))
        stopped = stopped_now
    for a in sorted(open_):  # the stage's last Monday: a real sell, no roll across the stage end
        close(open_[a], *leg_end(data[a]["c"], open_[a]["start_t"], cfg["last_search_d"]), True)
    return legs, weeks


def price_legs(legs, side_pct, eth):
    """TICKET: "5 USD per leg. A leg's net P&L = 5 USD x its net return, rolled legs included (as if re-sized to 5 USD at
    no cost), booked on the day the leg ends." Fills by core/friction.py's convention: a buy fills at price x (1 + f),
    a sell at price x (1 - f), f = the side's friction (capped at 100%); gross = from the m open, no friction.
    HOLD-ETH: "per leg, ETH's daily open on the leg's start day to ETH's daily open on its end day (that day's close for
    a stop, or for an exit_gap that ends at a last close), no friction." A missing ETH number refuses the run."""
    for L in legs:
        f = min(100.0, side_pct(L["token"])) / 100
        buy_px = L["start_px"] * (1 + f) if L["buy"] else L["start_px"]
        sell_px = L["end_px"] * (1 - f) if L["sell"] else L["end_px"]
        L["gross"] = L["end_px"] / L["start_px"] - 1
        L["net"] = sell_px / buy_px - 1
        L["pnl"] = 5.0 * L["net"]
        L["fric_pct"] = f * 100
        e0, e1 = eth.get(L["start_t"]), eth.get(L["end_t"])
        if e0 is None or e1 is None or (L["end_at_close"] and e1[3] is None):
            raise Refused(f"ETH candle missing for a leg ({_day(L['start_t'])} -> {_day(L['end_t'])})")
        L["eth"] = (e1[3] if L["end_at_close"] else e1[0]) / e0[0] - 1
    return legs


# ---- the simulation ----
def simulate(stage, data, uni, family, eth, fees, choices, side_pct=None, rebuy=False, mondays=None):
    """Both books for one stage at one friction. side_pct(token) -> percent per side (default: the spec's route)."""
    _guard_data(stage, data, uni, eth)
    mondays = mondays or STAGES[stage]["mondays"]
    ft = fee_table(fees)
    side_pct = side_pct or (lambda a: ft[a]["pct"])
    U = {}
    for m in mondays:
        U[m] = universe_at(m, data, uni, family)
        missing = [a for a in U[m][1] if a not in ft or ft[a]["pool"] != uni[a]["pool"]]
        if missing:
            raise Refused(f"{len(missing)} universe tokens at {_day(m)} have no fee-file entry for their pool")

    # BENCHMARK: "at each m, one leg for every universe token (tokens stopped the week before included),
    #   same LEG, ROLL and STOP rules, gross, equal weight."
    bench, bweeks = book(mondays, stage, data, lambda i, m, s: (sorted(U[m][0]), {}), side_pct, choices, "bench")
    bench_stops = {w["monday"]: set(w["stopped"]) for w in bweeks}

    # PICKS: "k = ceil(n / 10), n = the universe size at m. The k eligible universe tokens with the highest MAX; ties
    # by lower address (lowercase hex). A token stopped in the week before m stays in the universe, in n and in the
    # benchmark, but is not eligible at m; its slot goes to the next eligible token by MAX."
    def select(i, m, own_stopped):
        rets = U[m][0]
        stopped = set(own_stopped)
        if choices["Q1"] == "any_leg":
            stopped |= bench_stops.get(m - 7 * DAY, set())
        n = len(rets)
        k = math.ceil(n / 10)
        ranked = rank(rets)
        eligible = [a for a in ranked if a not in stopped]
        picks = eligible[:k]
        return picks, {"n": n, "k": k, "refilled": [a for a in ranked[:k] if a in stopped],
                       "max_last_day": [a for a in picks if rets[a][-1] == max(rets[a])]}

    picks, pweeks = book(mondays, stage, data, select, side_pct, choices, "pick", rebuy)
    price_legs(picks, side_pct, eth)
    price_legs(bench, lambda a: 0.0, eth)
    return {"U": U, "picks": picks, "bench": bench, "weeks": pweeks, "bweeks": bweeks, "mondays": mondays}


# ---- tests and outcome ----
def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def without_top2(legs):
    """"(2) ... and still > 0 without the top 2 legs": the two legs with the highest net P&L removed."""
    return sorted(legs, key=lambda L: L["pnl"], reverse=True)[2:]


def week_table(sim, bench_filter=None):
    """PER WEEK w: "P_w = mean gross return of that week's pick legs; B_w = mean gross return of that week's benchmark
    legs." A week's legs are those that start at its Monday."""
    rows = []
    for m in sim["mondays"]:
        p = [L["gross"] for L in sim["picks"] if L["start_t"] == m]
        b = [L["gross"] for L in sim["bench"] if L["start_t"] == m and (bench_filter is None or bench_filter(L, m))]
        rows.append({"monday": _day(m), "pick_legs": len(p), "bench_legs": len(b), "P_w": _mean(p), "B_w": _mean(b)})
    return rows


def test3(rows, choices, need):
    """"(3) the mean of (P_w - B_w) > 0, AND P_w > B_w in at least 8 of 12 weeks (stage 1) / 6 of 8 weeks (stage 2)";
    "A week with no closed pick leg counts as P_w <= B_w." The mean over weeks without a pick leg is Q3."""
    diffs = []
    for r in rows:
        if r["P_w"] is not None and r["B_w"] is not None:
            diffs.append(r["P_w"] - r["B_w"])
        elif choices["Q3"] == "zero_diff":
            diffs.append(0.0)
        elif choices["Q3"] == "cash_zero" and r["B_w"] is not None:
            diffs.append(0.0 - r["B_w"])
    wins = sum(1 for r in rows if r["P_w"] is not None and r["B_w"] is not None and r["P_w"] > r["B_w"])
    mean = _mean(diffs)
    return {"mean_diff": mean, "weeks_won": wins, "weeks": len(rows), "need": need,
            "pass": mean is not None and mean > 0 and wins >= need}


def max_drawdown(legs, start=50.0):
    """"(5) max drawdown < 30% on a 50 USD book, equity updated once per exit date with that date's summed P&L.\""""
    by_day = {}
    for L in legs:
        by_day[L["end_t"]] = by_day.get(L["end_t"], 0.0) + L["pnl"]
    eq = peak = start
    dd = 0.0
    for t in sorted(by_day):
        eq += by_day[t]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 1.0)
    return dd


def tests_2_4(legs, choices):
    """(2) "mean net P&L per leg after friction > 0, and still > 0 without the top 2 legs"; (4) "mean net return per leg >
    hold-ETH over the same legs". A mean with no leg is Q11: fail, or not evaluable (None)."""
    m_all, m_rest = _mean(L["pnl"] for L in legs), _mean(L["pnl"] for L in without_top2(legs))
    net, eth = _mean(L["net"] for L in legs), _mean(L["eth"] for L in legs)
    na = None if choices["Q11"] == "not_evaluable" else False
    t2 = na if m_all is None or m_rest is None else (m_all > 0 and m_rest > 0)
    t4 = na if net is None else net > eth
    return {"2": t2, "4": t4, "mean_pnl": m_all, "mean_pnl_without_top2": m_rest, "mean_net": net, "mean_eth": eth}


def outcome(t, stage, choices):
    """"A stage that fails 2, 3 or 4 is retired, whatever its leg count. A stage that would otherwise pass but has fewer
    legs than (1) is inconclusive = not passed; if it is stage 1, stage 2 is not run and stays unspent. ... Only 5 fails
    = not passed: drawdown: stage 2 is not run; forward paper vs the benchmark, never money. Both stages pass = forward
    paper vs the benchmark, never money." "A fail is strong evidence, a pass is weak: a pass means 'not killed', never
    'proven'." Tests 1 and 5 failing together (2-4 pass) is Q4; a test that is not evaluable (Q11) counts as
    "would otherwise pass" only for the inconclusive rule."""
    if t["2"] is False or t["3"] is False or t["4"] is False:
        verdict = "retired"
    elif t["2"] is None or t["4"] is None:
        assert not t["1"], "a mean with no leg cannot meet test 1"
        verdict = "inconclusive"
    elif t["1"] and t["5"]:
        verdict = "pass"
    elif not t["1"] and t["5"]:
        verdict = "inconclusive"
    elif t["1"] and not t["5"]:
        verdict = "not passed: drawdown"
    else:
        verdict = "inconclusive" if choices["Q4"] == "inconclusive" else "not passed: drawdown"
    nxt = {"retired": "retired" + ("; stage 2 is never read and stays unspent" if stage == 1 else ""),
           "inconclusive": "inconclusive = not passed" + ("; stage 2 is not run and stays unspent" if stage == 1 else ""),
           "not passed: drawdown": ("stage 2 is not run; " if stage == 1 else "")
           + "forward paper vs the benchmark, never money",
           "pass": ("stage 2 may run once, only after a docs/DECISIONS.md 'stage 1 pass' line (the owner)" if stage == 1
                    else "both stages pass: forward paper vs the benchmark, never money")
           + "; a pass means 'not killed', never 'proven'"}[verdict]
    return verdict, nxt


def evaluate(stage, data, uni, family, eth, fees, choices, measured=None, mondays=None):
    """Every pass test, the outcome and every "always reported, never the verdict" line. mondays: tests only (the run
    uses the stage's own). measured (the Q9s cost file) feeds the Q9 line only, never a test or the verdict."""
    _guard_data(stage, data, uni, eth)
    cfg = STAGES[stage]
    ft = fee_table(fees)
    sim = simulate(stage, data, uni, family, eth, fees, choices, mondays=mondays)
    mondays = sim["mondays"]
    legs = sim["picks"]
    rows = week_table(sim)
    t = tests_2_4(legs, choices)
    t3 = test3(rows, choices, cfg["weeks_needed"])
    tests = {"1": len(legs) >= cfg["min_legs"], "2": t["2"], "3": t3["pass"], "4": t["4"],
             "5": max_drawdown(legs) < 0.30}
    verdict, nxt = outcome(tests, stage, choices)

    def lines_at(**kw):
        s = simulate(stage, data, uni, family, eth, fees, choices, mondays=mondays, **kw)
        x = tests_2_4(s["picks"], choices)
        return {"legs": len(s["picks"]), "mean_pnl": x["mean_pnl"], "mean_pnl_without_top2": x["mean_pnl_without_top2"],
                "mean_net": x["mean_net"], "test2": x["2"], "test4": x["4"]}

    # "Always reported, never the verdict"
    toks = [L["token"] for L in legs]
    rep = {"legs": len(legs), "distinct_tokens": len(set(toks)),
           "legs_per_token": {"mean": len(toks) / len(set(toks)) if toks else None,
                              "max": max((toks.count(a) for a in set(toks)), default=0)},
           "roll_share": (sum(1 for L in legs if L["rolled"]) / len(legs)) if legs else None,
           "no_fill": sum(len(w["no_fill"]) for w in sim["weeks"]),
           "exit_gap": sum(1 for L in legs if L["end"] == "exit_gap"),
           "stops": sum(1 for L in legs if L["end"] == "stop"),
           "mean_pnl_without_top2": t["mean_pnl_without_top2"]}

    # "results per week and by ETH 7-day return before m (>= 0 / < 0)" (Q6 = the ETH prices)
    def eth7(m):
        e = eth
        q = choices["Q6"]
        if q == "open_to_open":
            a, b = e.get(m - 7 * DAY), e.get(m)
            return None if a is None or b is None else b[0] / a[0] - 1
        if q == "close_to_close":
            a, b = e.get(m - 8 * DAY), e.get(m - DAY)
            return None if a is None or b is None else b[3] / a[3] - 1
        a, b = e.get(m - 7 * DAY), e.get(m - DAY)
        return None if a is None or b is None else b[3] / a[0] - 1

    def eth_week(m):
        a, b = eth.get(m), eth.get(m + 7 * DAY)
        return None if a is None or b is None else b[0] / a[0] - 1

    for r, m, w in zip(rows, sim["mondays"], sim["weeks"]):
        lg = [L for L in legs if L["start_t"] == m]
        r.update(n=w["n"], k=w["k"], picks=len(w["chosen"]), mean_net=_mean(L["net"] for L in lg),
                 eth_7d_before=eth7(m), eth_leg_week=eth_week(m),
                 diff=None if r["P_w"] is None or r["B_w"] is None else r["P_w"] - r["B_w"])

    def split(key):
        out = {}
        for name, ok in ((">= 0", lambda x: x is not None and x >= 0), ("< 0", lambda x: x is not None and x < 0)):
            ws = [r for r in rows if ok(r[key])]
            ms = {_ts(r["monday"]) for r in ws}
            lg = [L for L in legs if L["start_t"] in ms]
            out[name] = {"weeks": len(ws), "legs": len(lg), "mean_net": _mean(L["net"] for L in lg),
                         "mean_diff": _mean(r["diff"] for r in ws if r["diff"] is not None),
                         "weeks_won": sum(1 for r in ws if r["diff"] is not None and r["diff"] > 0)}
        return out

    rep["by_eth_7d_before_m"] = split("eth_7d_before")
    # "P_w - B_w split by ETH's return over the leg week (>= 0 / < 0)" (ETH open m -> open m + 7 d)
    rep["diff_by_eth_leg_week"] = split("eth_leg_week")

    # "friction at half and at double"
    rep["friction_half"] = lines_at(side_pct=lambda a: 0.5 * ft[a]["pct"])
    rep["friction_double"] = lines_at(side_pct=lambda a: 2.0 * ft[a]["pct"])

    # "the BL-45 measured-cost line" (Q9 = what it computes; Q9s = its source for the real run: not built)
    if measured is None:
        rep["bl45_measured"] = "not computed: no Q9s measured cost was passed"
    elif choices["Q9"] == "side_by_side":
        rep["bl45_measured"] = [{"token": a, "spec_pct": ft[a]["pct"], "measured_pct": measured[a]}
                                for a in sorted(set(toks)) if a in measured]
    else:
        s = simulate(stage, data, uni, family, eth, fees, choices, mondays=mondays,
                     side_pct=lambda a: measured[a] if a in measured else ft[a]["pct"])
        sel = s["picks"] if choices["Q9"] == "replace_metered" else [L for L in s["picks"] if L["token"] in measured]
        x = tests_2_4(sel, choices)
        rep["bl45_measured"] = {"legs": len(sel), "mean_pnl": x["mean_pnl"],
                                "mean_pnl_without_top2": x["mean_pnl_without_top2"], "mean_net": x["mean_net"]}

    # "v1's line (4% a side, re-buy every week)"
    rep["v1_line"] = lines_at(side_pct=lambda a: 4.0, rebuy=True)

    # "picks that joined the universe in the last 7 days" (Q10 = the first Monday of stage 1)
    joined = []
    for i, (m, w) in enumerate(zip(sim["mondays"], sim["weeks"])):
        if i == 0 and stage == 1 and choices["Q10"] == "na_first":
            joined.append({"monday": _day(m), "joined": None})
            continue
        prev = sim["U"][m - 7 * DAY][0] if (m - 7 * DAY) in sim["U"] else universe_at(m - 7 * DAY, data, uni, family)[0]
        joined.append({"monday": _day(m), "joined": sum(1 for a in w["chosen"] if a not in prev)})
    rep["picks_joined_last_7d"] = joined

    # "B_w without the 0.01-0.05% pools" (the deepest pool's fee tier in [0.01, 0.05]; an unknown fee is kept)
    def not_low(L, m):
        f = ft[L["token"]]["flags"]["pool_fee"]
        return not (f is not None and 0.01 <= f <= 0.05)

    rep["B_w_without_001_005"] = _bw_line(rows, week_table(sim, not_low))

    # "B_w over the top third of the universe by 30-day volatility" (Q7 = the measure and the rounding)
    meas, rnd = choices["Q7"].split("_")
    top = {}
    for m in sim["mondays"]:
        rets = sim["U"][m][0]
        vol_of = {a: statistics.pstdev([math.log1p(r) for r in rs] if meas == "log" else rs) for a, rs in rets.items()}
        vol = sorted(rets, key=lambda a: (-vol_of[a], a))
        n3 = math.ceil(len(vol) / 3) if rnd == "ceil" else len(vol) // 3
        top[m] = set(vol[:n3])
    rep["B_w_top_third_vol"] = _bw_line(rows, week_table(sim, lambda L, m: L["token"] in top[m]))

    # "picks whose MAX day is the last day before m"
    rep["picks_max_last_day"] = sum(len(w["max_last_day"]) for w in sim["weeks"])

    # "legs on unknown-fee, hook and changeable-fee pools, with tests 2 and 4 re-run with only those legs at double
    # friction" (Q8 = whether an unknown hop counts)
    def flagged(a):
        f = ft[a]["flags"]
        return f["pool_unknown"] or f["hook"] or f["may_differ"] or (choices["Q8"] == "pool_or_hop" and f["hop_unknown"])

    s = simulate(stage, data, uni, family, eth, fees, choices, mondays=mondays,
                 side_pct=lambda a: (2.0 if flagged(a) else 1.0) * ft[a]["pct"])
    sel = [L for L in s["picks"] if flagged(L["token"])]
    x = tests_2_4(sel, choices)
    rep["flagged_pools_double"] = {"legs": len(sel), "mean_pnl": x["mean_pnl"],
                                   "mean_pnl_without_top2": x["mean_pnl_without_top2"], "mean_net": x["mean_net"],
                                   "mean_eth": x["mean_eth"], "test2": x["2"], "test4": x["4"]}

    # "exit_gap legs at -100%"
    gap = [dict(L, net=-1.0, pnl=-5.0) if L["end"] == "exit_gap" else L for L in legs]
    x = tests_2_4(gap, choices)
    rep["exit_gap_at_minus_100"] = {"exit_gap_legs": rep["exit_gap"], "mean_pnl": x["mean_pnl"],
                                    "mean_pnl_without_top2": x["mean_pnl_without_top2"], "test2": x["2"],
                                    "test4": x["4"]}

    # "picks lost to no_fill and slots refilled after a stop"
    rep["picks_lost_no_fill"] = rep["no_fill"]
    rep["slots_refilled_after_stop"] = sum(len(w["refilled"]) for w in sim["weeks"])

    return {"stage": stage, "mondays": [_day(m) for m in sim["mondays"]],
            "tests": {"1": {"pass": tests["1"], "closed_legs": len(legs), "need": cfg["min_legs"]},
                      "2": {"pass": tests["2"], "mean_pnl": t["mean_pnl"],
                            "mean_pnl_without_top2": t["mean_pnl_without_top2"]},
                      "3": t3,
                      "4": {"pass": tests["4"], "mean_net": t["mean_net"], "mean_hold_eth": t["mean_eth"]},
                      "5": {"pass": tests["5"], "max_drawdown": max_drawdown(legs)}},
            "verdict": verdict, "next": nxt, "weeks": rows, "reported": rep, "legs": legs}


def _bw_line(rows, alt):
    out = []
    for r, a in zip(rows, alt):
        d = None if r["P_w"] is None or a["B_w"] is None else r["P_w"] - a["B_w"]
        out.append({"monday": r["monday"], "B_w": a["B_w"], "bench_legs": a["bench_legs"], "diff": d})
    ds = [x["diff"] for x in out if x["diff"] is not None]
    return {"weeks": out, "mean_diff": _mean(ds), "weeks_won": sum(1 for d in ds if d > 0)}


# ---- run, write, check ----
LEG_FIELDS = ("book", "token", "monday", "rolled", "buy", "start_t", "start_px", "stop_ref", "end", "end_t", "end_px",
              "sell", "fric_pct", "gross", "net", "pnl", "eth")


def write_result(root, stage, result, now, choices, meta=None):
    """The result folder is created once (mkdir without exist_ok: a second write fails), before anything is printed.
    meta (from run()): the tool's sha256, git HEAD, the approval row and the attempt record's path."""
    d = result_dir(root, stage)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.mkdir()
    legs = result.pop("legs")
    summary = dict(result, spec=SPEC, sha256=PINS[SPEC], ran_at=_iso(now), choices=dict(choices), **(meta or {}))
    (d / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    with (d / "legs.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(LEG_FIELDS)
        for L in legs:
            w.writerow([_day(L[k]) if k in ("monday", "start_t", "end_t") else L[k] for k in LEG_FIELDS])
    result["legs"] = legs
    return d


def load(root, stage):
    """The stage's data. Peek guard: the real committed files only while run()'s gate is open for this root and
    stage; checked before any file is opened."""
    cfg = STAGES[stage]
    root = Path(root)
    paths = [root / rel for rel in (DAILY, ETH, UNIVERSE, FAMILY, FEES, COST)]
    real = any(_is_real(p) for p in paths)
    if real and not _gate_open(root, stage):
        raise _peek_refused("load()", stage)
    data = read_daily(root / DAILY, cfg["full_before"], cfg["open_until"])
    eth_lines = read_daily(root / ETH, cfg["full_before"], cfg["open_until"])
    if len(eth_lines) != 1:
        raise Refused("the ETH file must hold one token line")
    eth = next(iter(eth_lines.values()))["c"]
    uni = read_universe(root / UNIVERSE)
    if ADS in uni:
        raise Refused("ADS is in universe.json (the spec: it is not)")
    family = family_set(json.loads((root / FAMILY).read_text()), json.loads((root / FEES).read_text()))
    fees = json.loads((root / FEES).read_text())
    data, uni, eth = _Loaded(data), _Loaded(uni), _Loaded(eth)
    for x in (data, uni, eth):
        x.real_root = root.resolve() if real else None
    return data, uni, family, eth, fees


def _git_head(root):
    import subprocess  # lazy: only the run records it
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def write_attempt(root, stage, now, row):
    """The attempt record, created once (mode "x") before the first data read; it stays after the run."""
    p = attempt_path(root, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"stage": stage, "started_at": _iso(now), "tool": TOOL, "tool_sha256": tool_sha(),
           "git_head": _git_head(root), "spec_sha256": PINS[SPEC], "approval_row": row}
    with open(p, "x") as fh:
        fh.write(json.dumps(rec, indent=1) + "\n")
    return rec


def run(root=ROOT, stage=1, now=None, pins=None, choices=None):
    """THE run of a stage. Refused unless every check passes and every CHOICES key is resolved; writes the result
    before returning it."""
    global _GATE
    choices = CHOICES if choices is None else choices
    why = refusal(root, stage, pins, choices)
    if why:
        raise Refused(why)
    now = now or int(datetime.now(timezone.utc).timestamp())
    row = next(r for r in decision_lines(root, f"stage {stage} run approved") if not tool_pin_refusal(r))
    rec = write_attempt(root, stage, now, row)
    _GATE = (Path(root).resolve(), stage)
    try:
        data, uni, family, eth, fees = load(root, stage)
        measured = read_cost(Path(root) / COST) if choices["Q9s"] == "pinned_file" else None
        result = evaluate(stage, data, uni, family, eth, fees, choices, measured=measured)
        meta = {"tool": TOOL, "tool_sha256": rec["tool_sha256"], "git_head": rec["git_head"], "approval_row": row,
                "attempt": str(attempt_path(root, stage).relative_to(root))}
        write_result(root, stage, result, now, choices, meta)
    finally:
        _GATE = None
    return result


def check(root=ROOT, stage=1, pins=None):
    """Hashes, meta headers and timestamps only: no candle row, no price, return, MAX or pick."""
    pins = PINS if pins is None else pins
    root = Path(root)
    cfg = STAGES[stage]
    out = {"stage": stage, "hashes": {}}
    for rel, want in pins.items():
        p = root / rel
        got = sha256(p) if p.exists() else None
        out["hashes"][rel] = {"sha256": got, "pinned": want, "ok": got == want}
    for rel in (DAILY, ETH):
        p = root / rel
        if p.exists():
            m = read_meta(p)
            out[rel] = {k: m.get(k) for k in ("card", "cut", "fetched_at", "first_day", "last_day")}
    if (root / UNIVERSE).exists():
        u = json.loads((root / UNIVERSE).read_text())
        out[UNIVERSE] = {"cut": u.get("cut"), "fetched_at": u.get("fetched_at"), "tokens": len(u.get("tokens") or [])}
    try:
        fam = family_set(json.loads((root / FAMILY).read_text()), json.loads((root / FEES).read_text()))
        out["family_check"] = (f"ok: {len(fam) - 1} listed tokens = the fee file's ADS-quoted tokens, + ADS "
                               "(the GoPlus creator half is pinned by the family file's hash, not re-derived)")
    except (Refused, OSError, KeyError, ValueError) as e:
        out["family_check"] = f"FAILED: {e}"
    out["mondays"] = [_day(m) for m in cfg["mondays"]]
    out["load"] = {"full_candles_before": _iso(cfg["full_before"]),
                   "open_only": f"{_day(cfg['full_before'])} .. {_day(cfg['open_until'])}",
                   "dropped": f"every candle after {_day(cfg['open_until'])}"}
    out["result_dir"] = {"path": str(result_dir(root, stage).relative_to(root)),
                         "exists": result_dir(root, stage).exists()}
    out["tool_sha256"] = tool_sha()
    out["run_approved_line"] = bool(run_approved_line(root, stage))
    out["stage1_pass_line"] = bool(stage1_pass_line(root))
    out["attempt_record"] = {"path": str(attempt_path(root, stage).relative_to(root)),
                             "exists": attempt_path(root, stage).exists()}
    out["open_questions"] = unresolved(CHOICES)
    out["run_now"] = refusal(root, stage, pins) or "allowed"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.bl39", description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--run", action="store_true")
    ap.add_argument("--stage", type=int, default=1, choices=(1, 2))
    ap.add_argument("--root", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    if a.check:
        print(json.dumps(check(a.root, a.stage), indent=1))
        return 0
    try:
        r = run(a.root, a.stage)
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps({k: r[k] for k in ("stage", "tests", "verdict", "next", "reported")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
