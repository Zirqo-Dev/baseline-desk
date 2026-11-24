"""BL-39 run tool (tools/bl39.py): synthetic fixtures only. Committed backtest/ files are hashed, never parsed."""
import builtins
import gzip
import hashlib
import io
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import bl39 as B

ROOT = Path(__file__).resolve().parents[1]
D = B.DAY
M0 = B._ts("2026-06-15")
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
START = B._ts("2026-05-01")
SENTINEL = 777.125  # a value no kept field may carry


def tok(i):
    return "0x" + f"{i:040x}"


def pool(a):
    return "0xe" + a[3:]


def flat(start, days, px=1.0, vol=100_000.0):
    return {start + i * D: (px, px, px, px, vol) for i in range(days)}


def fee_entry(a, fee="0.3", other=USDC, family=False, unknown=False, may_differ=False, hops=(), hooks=None):
    e = {"token": a, "pool": pool(a), "other_token": other, "family": family, "fee_pct": None if unknown else fee,
         "unknown": unknown, "may_differ": may_differ, "hops": list(hops)}
    if hooks is not None:
        e["hooks"] = hooks
    return e


FAM = ["0xadf" + f"{i:037x}" for i in range(39)]


def family_file():
    return {"ads": B.ADS, "creator": B.CREATOR, "count": 39,
            "members": [{"token": a, "pool": pool(a), "symbol": f"F{i}", "source": "synthetic"}
                        for i, a in enumerate(FAM)]}


def base_fees(tokens, extra=()):
    return {"tokens": [fee_entry(a) for a in tokens] + list(extra)
            + [fee_entry(a, fee="0.01", other=B.ADS, family=True) for a in FAM]}


BASE = {q: (o[0] if o else None) for q, o in B.OPTIONS.items()}


def ch(**kw):
    return dict(BASE, **kw)


class World:
    """n flat tokens (price 1, volume 100k/day, May 1 .. Sep 30), pools created Jan 1, ETH flat 2000."""

    def __init__(self, n=11, days=153):
        self.tokens = [tok(i + 1) for i in range(n)]
        self.data = {a: {"pool": pool(a), "symbol": a[-4:], "c": flat(START, days)} for a in self.tokens}
        self.uni = {a: {"pool": pool(a), "created": B._ts("2026-01-01"), "symbol": a[-4:]} for a in self.tokens}
        self.family = set(FAM) | {B.ADS}
        self.eth = flat(START, days, 2000.0)
        self.extra_fees = []

    @property
    def fees(self):
        return base_fees([a for a in self.tokens if a not in {e["token"] for e in self.extra_fees}], self.extra_fees)

    def set(self, a, t, o, h=None, l=None, c=None, v=100_000.0):
        h = max(o, c if c is not None else o) if h is None else h
        c = o if c is None else c
        l = min(o, c) if l is None else l
        self.data[a]["c"][t] = (o, h, l, c, v)

    def level(self, a, t0, px):
        """Every candle of `a` from t0 on at price px."""
        for t in list(self.data[a]["c"]):
            if t >= t0:
                self.data[a]["c"][t] = (px, px, px, px, 100_000.0)

    def jump(self, a, t, px=1.2):
        """One day at px, so MAX includes px - 1 while t is in the lookback."""
        self.set(a, t, px)

    def drop(self, a, t):
        self.data[a]["c"].pop(t, None)

    def sim(self, choices=None, mondays=(M0,), stage=1, **kw):
        return B.simulate(stage, self.data, self.uni, self.family, self.eth, self.fees, choices or ch(),
                          mondays=list(mondays), **kw)

    def evaluate(self, choices=None, mondays=(M0,), stage=1, measured=None):
        return B.evaluate(stage, self.data, self.uni, self.family, self.eth, self.fees, choices or ch(),
                          measured=measured, mondays=list(mondays))


TOP = tok(50)  # a high address, so only its MAX can make it a pick


def world_with_top(**kw):
    w = World(**kw)
    w.tokens.append(TOP)
    w.data[TOP] = {"pool": pool(TOP), "symbol": "TOP", "c": flat(START, 153)}
    w.uni[TOP] = {"pool": pool(TOP), "created": B._ts("2026-01-01"), "symbol": "TOP"}
    w.jump(TOP, M0 - 3 * D)
    return w


def legs_of(s, a, book="picks"):
    return [L for L in s[book] if L["token"] == a]


# ---- files ----
def write_gz(path, meta, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(meta, kind="meta")) + "\n")
        for x in lines:
            fh.write(json.dumps(dict(x, kind="token")) + "\n")


def candle_rows(cs):
    return [[t, *row] for t, row in sorted(cs.items())]


def make_root(w, decisions=""):
    """A temp repo root with every file the tool reads, built from World w (synthetic)."""
    r = Path(tempfile.mkdtemp())
    (r / "backtest/specs").mkdir(parents=True)
    (r / B.SPEC).write_text("synthetic spec\n")
    (r / B.FAMILY).write_text(json.dumps(family_file()))
    (r / B.FEES).write_text(json.dumps(w.fees))
    (r / B.COST).write_text(json.dumps({"source": "synthetic", "window": {"from": "x", "to": "y"},
                                        "tokens": [{"token": w.tokens[0], "symbol": "T0", "per_side_pct": "0.1"}]}))
    (r / "backtest/liquid").mkdir(parents=True)
    (r / B.UNIVERSE).write_text(json.dumps({"cut": "2026-11-08T00:00:00Z", "fetched_at": "2026-11-09T14:18:40Z",
                                            "tokens": [{"token": a, "pool": pool(a), "symbol": a[-4:],
                                                        "pool_created_at": "2026-01-01T00:00:00Z",
                                                        "reserve_usd": SENTINEL} for a in w.tokens]}))
    write_gz(r / B.DAILY, {"cut": "2026-11-08T00:00:00Z", "fetched_at": "2026-11-09T14:18:40Z", "card": "BL-39"},
             [{"token": a, "pool": pool(a), "symbol": a[-4:], "candles": candle_rows(w.data[a]["c"])}
              for a in w.tokens])
    write_gz(r / B.ETH, {"cut": "2026-11-08T00:00:00Z", "fetched_at": "2026-11-20T16:51:47Z", "first_day": "2026-05-01",
                         "last_day": "2026-09-30", "card": "BL-39"},
             [{"token": WETH, "pool": "0x6c561b446416e1a00e8e93e221854d6ea4171372", "symbol": "WETH",
               "candles": candle_rows(w.eth)}])
    (r / "docs").mkdir()
    (r / B.DECISIONS).write_text("| date | card | decision | why | rejected |\n|---|---|---|---|---|\n" + decisions)
    pins = {rel: hashlib.sha256((r / rel).read_bytes()).hexdigest() for rel in B.PINS}
    return r, pins


class Pins(unittest.TestCase):
    def test_pins_are_the_frozen_hashes(self):
        self.assertEqual(B.PINS, {
            "backtest/specs/bl39-max-momentum-v2.1.md": "5202982dbe901b5fc7b77956ef322ef5770cc05f863acd1d878d749c9a2b53ba",
            "backtest/specs/bl39-max-momentum-v2.1-family.json":
                "374b70eaf450e03d6a2bc208708fa1d3afcdaad77d080c2639aa39a00becc9ab",
            "backtest/specs/bl39-max-momentum-v2.1-fees.json":
                "4a7318c95a9650f5fa533ba0f9ac552db0b0449d7a9e8e22bca6797b16991180",
            "backtest/liquid/eth-daily.jsonl.gz": "82bc487a78a2f0306e56b1b1780b4bdab91b3d1588328c4edd3ece0f79828ac8",
            "backtest/liquid/daily.jsonl.gz": "5d14fcee0fca60ac5f71f616acb6869a0cb5489cf99fc9d24d688aa6f6f71267",
            "backtest/liquid/universe.json": "09c301d0ed8b5fbe5d4365659b950749d085fbec192be44dd8226e760e9b695a",
            "backtest/specs/bl39-max-momentum-v2.1-bl45-cost.json":
                "71d71de47e1d39b893c469f4ffa7f81be3ae595e4f627238e68301fe75d98566"})
        self.assertEqual(B._REAL_PINS, B.PINS)

    def test_committed_files_match_by_hash_only(self):
        self.assertEqual(B.hash_refusals(ROOT), [])

    def test_survey_pins_are_test_snapshots(self):
        src = (ROOT / "tests/test_snapshot.py").read_text()
        self.assertIn(B.PINS[B.DAILY], src)
        self.assertIn(B.PINS[B.UNIVERSE], src)


def row(text, card="BL-39"):
    return f"| 2026-12-01 | {card} | {text} | why | rejected |\n"


SHA12 = B.tool_sha()[:12]
PIN = f"tools/bl39.py sha256 {SHA12}"
APPROVE1 = row(f"stage 1 run approved, {PIN} (the owner)")
APPROVE2 = row(f"stage 2 run approved, {PIN} (the owner)")
PASS1 = row("stage 1 pass: all five tests (the owner)")


class Refusals(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.root, self.pins = make_root(self.w, APPROVE1)

    def tearDown(self):
        shutil.rmtree(self.root)

    def set_decisions(self, *rows):
        (self.root / B.DECISIONS).write_text("| date | card | decision | why | rejected |\n|---|---|---|---|---|\n"
                                             + "".join(rows))

    def test_clean_approved_root_passes(self):
        self.assertEqual(B.hash_refusals(self.root, self.pins), [])
        self.assertEqual(B.refusal(self.root, 1, self.pins, ch()), "")
        self.assertEqual(B.refusal(self.root, 1, self.pins), "")  # the owner's CHOICES
        why = B.refusal(self.root, 1, self.pins, ch(Q9s="x"))
        self.assertEqual(why, "refused: open spec questions unresolved (CHOICES): Q9s")
        self.assertEqual(B.refusal(self.root, 1, self.pins, ch(Q9s="x", Q12="decisions_rerun")),
                         "refused: open spec questions unresolved (CHOICES): Q9s Q12")  # not built = unresolved

    def test_each_file_hash_refused(self):
        for rel in B.PINS:
            with self.subTest(rel=rel):
                p = self.root / rel
                raw = p.read_bytes()
                p.write_bytes(raw + b" ")
                why = B.refusal(self.root, 1, self.pins, ch())
                self.assertTrue(why.startswith("refused: hashes:"))
                self.assertIn(rel, why)
                self.assertIn("is not the pinned", why)
                p.write_bytes(raw)

    def test_missing_file_refused(self):
        (self.root / B.ETH).unlink()
        self.assertIn(f"{B.ETH} missing", B.refusal(self.root, 1, self.pins, ch()))

    def test_real_pins_refuse_synthetic_files(self):
        why = B.refusal(self.root, 1, choices=ch())
        for rel in B.PINS:
            self.assertIn(rel, why)

    def test_choices_are_the_owners_answers(self):
        self.assertEqual(B.CHOICES, {"Q1": "pick_leg", "Q2": "close", "Q3": "zero_diff", "Q4": "inconclusive",
                                     "Q5": "fill_picks", "Q6": "open_to_open", "Q7": "simple_ceil",
                                     "Q8": "pool_or_hop", "Q9": "replace_metered", "Q9s": "pinned_file",
                                     "Q10": "na_first", "Q11": "fail", "Q12": "no_rerun"})
        self.assertEqual(B.unresolved(B.CHOICES), [])
        self.assertEqual((B.OPTIONS["Q9s"], B.OPTIONS["Q12"]), (("pinned_file",), ("no_rerun",)))
        self.assertEqual(set(B.NOT_BUILT), {"Q12"})
        self.assertEqual(set(B.QUESTIONS), set(B.OPTIONS))

    def test_unresolved_choices_refuse_the_run(self):
        with self.assertRaises(B.Refused) as e:
            B.run(self.root, 1, pins=self.pins, choices={q: None for q in B.OPTIONS})
        for q in B.OPTIONS:
            self.assertIn(q, str(e.exception))
        self.assertFalse(B.result_dir(self.root, 1).exists())
        self.assertFalse(B.attempt_path(self.root, 1).exists())

    def test_bad_choice_value_is_unresolved(self):
        self.assertIn("Q1", B.unresolved(ch(Q1="maybe")))

    def test_run_gate(self):
        for rows, ok in [
            ((APPROVE1,), True),
            ((row(f"Stage 1 run approved after the review, {PIN} (the owner)"),), True),
            ((row(f"stage 1 run approved, {PIN}"),), False),  # not the owner's
            ((row(f"stage 1 run approved, {PIN} (the owner)", card="BL-38"),), False),
            ((row(f"no stage 1 run approved yet, {PIN} (the owner)"),), False),
            ((f"stage 1 run approved BL-39 {PIN} (the owner)\n",), False),
            ((APPROVE2,), False),  # stage 2's approval is not stage 1's
            ((PASS1,), False),  # a pass line is not a run approval
            ((), False),
        ]:
            with self.subTest(rows=rows):
                self.set_decisions(*rows)
                self.assertEqual(bool(B.run_approved_line(self.root, 1)), ok)
                self.assertEqual("stage 1 run approved" in B.refusal(self.root, 1, self.pins, ch()), not ok)

    def test_stage2_needs_its_own_approval_and_the_pass_line(self):
        self.set_decisions(APPROVE1, PASS1)
        self.assertIn("stage 2 run approved", B.refusal(self.root, 2, self.pins, ch()))
        self.set_decisions(APPROVE1, APPROVE2)
        self.assertIn("stage 2 needs", B.refusal(self.root, 2, self.pins, ch()))
        self.set_decisions(APPROVE2, PASS1)
        self.assertIn("summary.json (the stage 1 result)", B.refusal(self.root, 2, self.pins, ch()))  # past both lines

    def test_stage2_pass_line(self):
        for line, ok in [
            (PASS1, True),
            (row("Stage 1 pass recorded (the owner)"), True),
            (row("stage 1 pass: all five tests"), False),  # not the owner's
            (row("stage 1 pass (the owner)", card="BL-38"), False),
            (row("no stage 1 pass yet (the owner)"), False),
            ("stage 1 pass BL-39 (the owner)\n", False),
        ]:
            with self.subTest(line=line):
                self.set_decisions(APPROVE2, line)
                self.assertEqual(bool(B.stage1_pass_line(self.root)), ok)
                self.assertEqual("stage 2 needs a docs/DECISIONS.md" in B.refusal(self.root, 2, self.pins, ch()),
                                 not ok)

    def test_run_once_guard(self):
        B.result_dir(self.root, 1).mkdir(parents=True)
        self.assertIn("runs once (no override)", B.refusal(self.root, 1, self.pins, ch()))
        self.set_decisions(APPROVE2, PASS1)
        self.assertNotIn("runs once", B.refusal(self.root, 2, self.pins, ch()))  # stage 2 has its own folder

    def test_write_result_once(self):
        r = {"stage": 1, "legs": [], "tests": {}}
        d = B.write_result(self.root, 1, r, M0, ch())
        self.assertTrue((d / "summary.json").exists() and (d / "legs.csv").exists())
        with self.assertRaises(FileExistsError):
            B.write_result(self.root, 1, {"stage": 1, "legs": []}, M0, ch())
        self.assertIn("runs once", B.refusal(self.root, 1, self.pins, ch()))

    def test_no_override_flag(self):
        src = (ROOT / "tools/bl39.py").read_text()
        for word in ("--force", "--rerun", "--override", "exist_ok=True)\n    d.mkdir(exist_ok"):
            self.assertNotIn(word, src)
        with self.assertRaises(SystemExit):
            B.main(["--run", "--force"])

    def test_bad_stage(self):
        self.assertIn("no stage 3", B.refusal(self.root, 3, self.pins, ch()))


class OpenSpy:
    """Wraps builtins.open and io.open (pathlib and gzip go through them); B.sha256 is wrapped to mark hashing."""

    def __init__(self, root, boom=True):
        self.root, self.hashing, self.seen, self.boom = Path(root).resolve(), 0, [], boom
        self.events = []  # every open and (when a test logs it) every load() call, in order, with the gate state
        self.real_open, self.real_sha = builtins.open, B.sha256
        self.data = {(self.root / rel).resolve() for rel in (B.DAILY, B.ETH, B.UNIVERSE, B.COST)}

    def open(self, file, mode="r", *a, **k):
        if isinstance(file, (str, Path)):
            self.seen.append((Path(file).resolve(), mode, self.hashing > 0))
            self.events.append({"kind": "open", "path": Path(file).resolve(), "mode": mode,
                                "hashing": self.hashing > 0, "gate": B._GATE})
        return self.real_open(file, mode, *a, **k)

    def sha(self, path):
        self.hashing += 1
        try:
            return self.real_sha(path)
        finally:
            self.hashing -= 1

    def data_reads(self):
        """Opens of a candle, ETH, universe or cost file outside hashing."""
        return [x for x in self.seen if x[0] in self.data and not x[2]]

    def hashed(self):
        return {x[0] for x in self.seen if x[0] in self.data and x[2]}

    def __enter__(self):
        boom = mock.Mock(side_effect=AssertionError("a data reader was reached"))
        self.patches = [mock.patch("builtins.open", self.open), mock.patch("io.open", self.open),
                        mock.patch.object(B, "sha256", self.sha)]
        if self.boom:
            self.patches += [mock.patch.object(B, f, boom) for f in ("load", "read_daily", "read_universe", "read_meta",
                                                                     "read_cost", "evaluate", "simulate",
                                                                     "write_result", "write_attempt")]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def tree(p):
    return sorted(str(x.relative_to(p)) for x in Path(p).rglob("*")) if Path(p).exists() else None


def write_stage1_summary(r, verdict, stage=1):
    d = B.result_dir(r, 1)
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({"stage": stage, "verdict": verdict}))


class RefusalOrder(unittest.TestCase):
    """The owner's order (2026-11-23, A, with the 2026-11-24 steps): hashes -> run-approval line -> its tool pin ->
    (stage 2) stage 1 pass line -> (stage 2) stage 1 summary records a pass -> no result folder -> no attempt record ->
    no unresolved CHOICES; every refusal path reads no candle, ETH, universe or cost file and creates nothing."""

    def setUp(self):
        self.w = World(n=3)

    def root(self, decisions=APPROVE1):
        r, pins = make_root(self.w, decisions)
        self.addCleanup(shutil.rmtree, r)
        return r, pins

    def refused(self, r, pins, stage, start, choices=None):
        before = tree(r / B.RESULTS)
        with OpenSpy(r) as spy:
            with self.assertRaises(B.Refused) as e:
                B.run(r, stage, pins=pins, choices=choices)
        msg = str(e.exception)
        print(f"  stage {stage}: {msg[:110]}")
        self.assertTrue(msg.startswith(start), msg)
        self.assertEqual(spy.data_reads(), [], "a candle, ETH or universe file was opened beyond hashing")
        self.assertEqual(tree(r / B.RESULTS), before, "the result folder changed")
        return spy, msg

    def test_1_hash_each_file(self):
        for rel in B.PINS:
            with self.subTest(rel=rel):
                r, pins = self.root()
                (r / rel).write_bytes((r / rel).read_bytes() + b" ")
                self.refused(r, pins, 1, "refused: hashes:")

    def test_1_missing_data_file(self):
        r, pins = self.root()
        (r / B.DAILY).unlink()
        self.refused(r, pins, 1, "refused: hashes:")

    def test_1_family_check(self):
        r, pins = self.root()
        fees = json.loads((r / B.FEES).read_text())
        fees["tokens"][0]["other_token"] = B.ADS
        (r / B.FEES).write_text(json.dumps(fees))
        pins = dict(pins, **{B.FEES: hashlib.sha256((r / B.FEES).read_bytes()).hexdigest()})
        self.refused(r, pins, 1, "refused: family check:")

    def test_2_no_run_approval(self):
        r, pins = self.root(decisions=PASS1)
        spy, _ = self.refused(r, pins, 1, "refused: no docs/DECISIONS.md BL-39 row starting 'stage 1 run approved'")
        self.assertEqual(spy.hashed(), spy.data)  # hashed (raw bytes), never parsed

    def test_3_stage2_no_pass_line(self):
        r, pins = self.root(decisions=APPROVE2)
        self.refused(r, pins, 2, "refused: stage 2 needs")

    def test_4_result_folder_exists(self):
        r, pins = self.root()
        d = B.result_dir(r, 1)
        d.mkdir(parents=True)
        (d / "summary.json").write_text("{}")
        self.refused(r, pins, 1, "refused: backtest/results/bl39-max-momentum-v2.1-stage1 exists")
        self.assertEqual((d / "summary.json").read_text(), "{}")

    def test_8_unresolved_choices(self):
        r, pins = self.root()
        self.refused(r, pins, 1, "refused: open spec questions unresolved (CHOICES): Q1 Q2",
                     choices={q: None for q in B.OPTIONS})
        self.refused(r, pins, 1, "refused: open spec questions unresolved (CHOICES): Q9s", choices=ch(Q9s=None))

    def test_first_failing_step_wins(self):
        r, pins = self.root(decisions="")
        B.result_dir(r, 2).mkdir(parents=True)
        (r / B.ETH).write_bytes((r / B.ETH).read_bytes() + b" ")
        self.refused(r, pins, 2, "refused: hashes:")
        pins = dict(pins, **{B.ETH: hashlib.sha256((r / B.ETH).read_bytes()).hexdigest()})
        self.refused(r, pins, 2, "refused: no docs/DECISIONS.md BL-39 row starting 'stage 2 run approved'")
        (r / B.DECISIONS).write_text(APPROVE2)
        self.refused(r, pins, 2, "refused: stage 2 needs")
        (r / B.DECISIONS).write_text(row(f"stage 2 run approved, tools/bl39.py sha256 {'0' * 12} (the owner)") + PASS1)
        self.refused(r, pins, 2, "refused: tool pin:")
        (r / B.DECISIONS).write_text(APPROVE2)
        self.refused(r, pins, 2, "refused: stage 2 needs a docs/DECISIONS.md")
        (r / B.DECISIONS).write_text(APPROVE2 + PASS1)
        self.refused(r, pins, 2, "refused: stage 2 needs backtest/results/bl39-max-momentum-v2.1-stage1/summary.json")
        write_stage1_summary(r, "pass")
        self.refused(r, pins, 2, "refused: backtest/results/bl39-max-momentum-v2.1-stage2 exists")
        shutil.rmtree(B.result_dir(r, 2))
        B.attempt_path(r, 2).write_text("{}")
        self.refused(r, pins, 2, "refused: an aborted attempt exists")
        B.attempt_path(r, 2).unlink()
        self.refused(r, pins, 2, "refused: open spec questions unresolved", choices=ch(Q1=None))
        self.assertFalse(B.result_dir(r, 2).exists())

    def test_cli_refusal_reads_nothing(self):
        r, pins = self.root(decisions=row("stage 1 run approved (the owner)"))
        with OpenSpy(r) as spy, mock.patch.object(B, "PINS", pins), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(B.main(["--run", "--stage", "1", "--root", str(r)]), 2)
        self.assertIn("refused: tool pin: the approval row names no tools/bl39.py sha256", err.getvalue())
        self.assertEqual(spy.data_reads(), [])
        self.assertIsNone(tree(r / B.RESULTS))


class ToolPinSummaryAttempt(unittest.TestCase):
    """The owner's 2026-11-24 fixes 2-4: the stage 2 summary gate, the tool pin, the attempt record."""

    def setUp(self):
        self.w = World(n=3)
        self.r, self.pins = make_root(self.w, APPROVE1)
        self.addCleanup(shutil.rmtree, self.r)

    def dec(self, *rows):
        (self.r / B.DECISIONS).write_text("".join(rows))

    def test_tool_pin(self):
        for text, ok in [
            (f"stage 1 run approved, {PIN} (the owner)", True),
            (f"stage 1 run approved, tools/bl39.py sha256 {B.tool_sha()} (the owner)", True),  # the full hash
            ("stage 1 run approved (the owner)", False),
            (f"stage 1 run approved, tools/bl39.py sha256 {SHA12[:11]} (the owner)", False),  # under 12 hex
            (f"stage 1 run approved, tools/bl39.py sha256 {'f' * 12} (the owner)", False),
            (f"stage 1 run approved, {SHA12} (the owner)", False),  # the phrase names the file
        ]:
            with self.subTest(text=text):
                self.dec(row(text))
                why = B.refusal(self.r, 1, self.pins, ch())
                self.assertEqual(why, "" if ok else why)
                self.assertEqual(why.startswith("refused: tool pin:"), not ok, why)
        self.dec(row(f"stage 1 run approved, tools/bl39.py sha256 {'f' * 12} (the owner)"))
        self.assertIn(f"names tools/bl39.py sha256 ffffffffffff, the tool is {SHA12}", B.refusal(self.r, 1, self.pins))
        self.dec(row(f"stage 1 run approved, tools/bl39.py sha256 {'f' * 12} (the owner)"), APPROVE1)  # a later row
        self.assertEqual(B.refusal(self.r, 1, self.pins), "")

    def test_tool_pin_follows_the_file(self):
        with mock.patch.object(B, "tool_sha", lambda: "a" * 64):
            self.assertIn("the tool is aaaaaaaaaaaa", B.refusal(self.r, 1, self.pins))

    # The fresh-context review of the final tool (2026-11-24, GO): gaps b1, b2, b4, b5.
    def assert_pin_refused(self, text):
        self.dec(text)
        why = B.refusal(self.r, 1, self.pins, ch())
        self.assertTrue(why.startswith("refused: tool pin:"), why)
        return why

    def test_tool_pin_first_8_hex_only_is_refused(self):
        """b1: a pin that matches the first 8 hex of the tool sha but differs afterwards."""
        sha = B.tool_sha()
        flip = "".join("1" if c == "0" else "0" for c in sha[8:])
        for pin in (sha[:8] + flip[:4], sha[:8] + flip):  # 12 hex and the full 64
            with self.subTest(pin=pin[:16]):
                self.assertEqual(pin[:8], sha[:8])
                self.assertFalse(sha.startswith(pin))
                self.assert_pin_refused(row(f"stage 1 run approved, tools/bl39.py sha256 {pin} (the owner)"))

    def test_tool_pin_substring_not_prefix_is_refused(self):
        """b2: a pin that is a substring of the tool sha, not its prefix."""
        sha = B.tool_sha()
        for sub in (sha[1:13], sha[30:42], sha[52:], sha[1:]):
            with self.subTest(sub=sub[:12]):
                self.assertIn(sub, sha)
                self.assertFalse(sha.startswith(sub))
                self.assert_pin_refused(row(f"stage 1 run approved, tools/bl39.py sha256 {sub} (the owner)"))

    def test_tool_pin_outside_the_decision_cell_is_refused(self):
        """b4: a correct pin in the row's why or rejected cell, none in its decision cell."""
        for cells in ((PIN, "rejected"), ("why", PIN), (PIN, PIN)):
            with self.subTest(cells=cells):
                why = self.assert_pin_refused(
                    f"| 2026-12-01 | BL-39 | stage 1 run approved (the owner) | {cells[0]} | {cells[1]} |\n")
                self.assertIn("the approval row names no tools/bl39.py sha256", why)
        self.dec(f"| 2026-12-01 | BL-39 | stage 1 run approved, {PIN} (the owner) | why | rejected |\n")
        self.assertEqual(B.refusal(self.r, 1, self.pins, ch()), "")  # the same pin in the decision cell passes

    def test_tool_sha_is_the_file_actually_run(self):
        """b5: tool_sha() = sha256 of the bytes of tools/bl39.py, the module file this process imported."""
        run_file = Path(B.__file__).resolve()
        self.assertEqual(run_file, (ROOT / "tools/bl39.py").resolve())
        self.assertEqual(B.tool_sha(), hashlib.sha256(run_file.read_bytes()).hexdigest())

    def test_stage2_summary_gate(self):
        self.dec(APPROVE2, PASS1)
        for setup, want in [
            (lambda: None, "summary.json (the stage 1 result), which does not exist"),
            (lambda: write_stage1_summary(self.r, "retired"), "it records verdict 'retired'"),
            (lambda: write_stage1_summary(self.r, "inconclusive"), "it records verdict 'inconclusive'"),
            (lambda: write_stage1_summary(self.r, "pass", stage=2), "it records verdict 'pass'"),  # wrong stage
            (lambda: (B.result_dir(self.r, 1) / "summary.json").write_text("{not json"), "readable JSON"),
            (lambda: write_stage1_summary(self.r, "pass"), ""),
        ]:
            with self.subTest(want=want):
                setup()
                why = B.refusal(self.r, 2, self.pins, ch())
                self.assertIn(want, why) if want else self.assertEqual(why, "")

    def test_summary_read_only_after_the_pass_line(self):
        write_stage1_summary(self.r, "pass")
        self.dec(APPROVE1, APPROVE2)
        with mock.patch.object(B, "stage1_summary_refusal", side_effect=AssertionError("read too early")):
            self.assertIn("stage 2 needs a docs/DECISIONS.md", B.refusal(self.r, 2, self.pins, ch()))
            self.assertIn("runs once", B.refusal(self.r, 1, self.pins, ch()))  # stage 1 never reads it

    def test_attempt_record_refuses_a_retry(self):
        B.attempt_path(self.r, 1).parent.mkdir(parents=True)
        B.attempt_path(self.r, 1).write_text("{}")
        why = B.refusal(self.r, 1, self.pins, ch())
        self.assertIn("an aborted attempt exists", why)
        self.assertIn("a retry is a rerun, its own story", why)
        B.result_dir(self.r, 1).mkdir()
        self.assertIn("runs once", B.refusal(self.r, 1, self.pins, ch()))  # the folder step comes first

    def test_run_writes_attempt_then_opens_the_gate_then_reads(self):
        seen = {}
        real_load = B.load

        def spy_load(root, stage):
            seen["attempt"] = B.attempt_path(self.r, 1).exists()
            seen["gate"] = B._GATE
            return real_load(root, stage)

        with mock.patch.object(B, "load", spy_load):
            B.run(self.r, 1, now=M0, pins=self.pins)
        self.assertEqual(seen, {"attempt": True, "gate": (self.r.resolve(), 1)})
        self.assertIsNone(B._GATE)
        rec = json.loads(B.attempt_path(self.r, 1).read_text())
        s = json.loads((B.result_dir(self.r, 1) / "summary.json").read_text())
        self.assertEqual(rec["tool_sha256"], B.tool_sha())
        self.assertEqual(s["tool_sha256"], B.tool_sha())
        self.assertEqual(s["tool"], "tools/bl39.py")
        self.assertIn("git_head", s)
        self.assertEqual(s["approval_row"], APPROVE1.strip())
        self.assertEqual(s["attempt"], "backtest/results/bl39-max-momentum-v2.1-stage1.attempt.json")
        self.assertEqual(s["choices"], B.CHOICES)
        self.assertIn("runs once", B.refusal(self.r, 1, self.pins))

    def test_git_head_recorded_in_a_repo(self):
        self.assertRegex(B._git_head(ROOT) or "", r"^[0-9a-f]{40}$")
        self.assertIsNone(B._git_head(self.r))  # a temp dir is no repo

    def test_aborted_run_leaves_a_record_and_closes_the_gate(self):
        with mock.patch.object(B, "evaluate", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                B.run(self.r, 1, now=M0, pins=self.pins)
        self.assertIsNone(B._GATE)
        self.assertTrue(B.attempt_path(self.r, 1).exists())
        self.assertFalse(B.result_dir(self.r, 1).exists())
        with self.assertRaises(B.Refused) as e:
            B.run(self.r, 1, now=M0, pins=self.pins)
        self.assertIn("an aborted attempt exists", str(e.exception))

    def test_measured_feeds_the_q9_line_only(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.2)
        a = w.evaluate()
        b = w.evaluate(measured={TOP: 0.01, w.tokens[0]: 3.0})
        self.assertEqual((a["tests"], a["verdict"], a["next"]), (b["tests"], b["verdict"], b["next"]))
        self.assertNotEqual(a["reported"]["bl45_measured"], b["reported"]["bl45_measured"])
        self.assertEqual({k: v for k, v in a["reported"].items() if k != "bl45_measured"},
                         {k: v for k, v in b["reported"].items() if k != "bl45_measured"})

    def test_run_reads_the_cost_file_for_q9(self):
        r = B.run(self.r, 1, now=M0, pins=self.pins)
        self.assertIsInstance(r["reported"]["bl45_measured"], dict)  # replace_metered computed
        self.assertEqual(B.read_cost(self.r / B.COST), {self.w.tokens[0]: 0.1})


class RunReadOrder(unittest.TestCase):
    """The fresh-context review of the final tool (2026-11-24, GO), gaps g1, g3, g4: inside one successful run() on a
    synthetic World, the attempt record is written first, then the gate opens, then load() and every data-file open.
    The open() log and the load() wrapper are test-side monkeypatches only (OpenSpy, mock.patch)."""

    def setUp(self):
        self.w = World(n=3)
        self.r, self.pins = make_root(self.w, APPROVE1)
        self.addCleanup(shutil.rmtree, self.r)
        self.attempt = B.attempt_path(self.r, 1).resolve()
        self.gate = (self.r.resolve(), 1)
        real_load = B.load
        with OpenSpy(self.r, boom=False) as spy:
            def logged_load(root, stage):
                spy.events.append({"kind": "load", "gate": B._GATE, "attempt": self.attempt.exists()})
                return real_load(root, stage)

            with mock.patch.object(B, "load", logged_load):
                B.run(self.r, 1, now=M0, pins=self.pins)
        self.ev, self.data = spy.events, spy.data
        self.assertTrue((B.result_dir(self.r, 1) / "summary.json").exists())  # the run succeeded
        self.assertIsNone(B._GATE)

    def first(self, pred, what):
        for i, e in enumerate(self.ev):
            if pred(e):
                return i
        self.fail(f"no {what} in the run")

    def attempt_write(self):
        return self.first(lambda e: e["kind"] == "open" and e["path"] == self.attempt
                          and any(c in e["mode"] for c in "xwa"), "attempt record write")

    def data_opens(self):
        return [i for i, e in enumerate(self.ev)
                if e["kind"] == "open" and e["path"] in self.data and not e["hashing"]]

    def test_g1_load_not_called_before_the_attempt_record(self):
        loads = [i for i, e in enumerate(self.ev) if e["kind"] == "load"]
        self.assertEqual(len(loads), 1, "run() calls load() once")
        self.assertGreater(loads[0], self.attempt_write())
        self.assertTrue(self.ev[loads[0]]["attempt"], "load() ran before the attempt record existed")

    def test_g3_gate_opens_only_after_the_attempt_record(self):
        w = self.attempt_write()
        self.assertEqual([e["gate"] for e in self.ev[:w + 1]], [None] * (w + 1),
                         "the gate was open at or before the attempt record write")
        opened = self.first(lambda e: e["gate"] is not None, "open gate")
        self.assertGreater(opened, w)
        self.assertEqual(self.ev[opened]["gate"], self.gate)

    def test_g4_first_data_open_after_the_attempt_record_and_inside_the_gate(self):
        opens = self.data_opens()
        self.assertTrue(opens, "the run opened no data file")
        self.assertGreater(opens[0], self.attempt_write())
        self.assertEqual({self.ev[i]["path"] for i in opens}, self.data)  # daily, ETH, universe and cost all read
        for i in opens:
            with self.subTest(path=self.ev[i]["path"].name):
                self.assertEqual(self.ev[i]["gate"], self.gate, "a data file was opened outside the gate")


class PeekGuard(unittest.TestCase):
    """The owner's 2026-11-24 peek guard: the real committed files are read only inside run(), after its gate."""

    def real_calls(self):
        s1, s2 = B.STAGES[1], B.STAGES[2]
        return [("load 1", lambda: B.load(ROOT, 1)), ("load 2", lambda: B.load(ROOT, 2)),
                ("daily", lambda: B.read_daily(ROOT / B.DAILY, s1["full_before"], s1["open_until"])),
                ("daily 2", lambda: B.read_daily(ROOT / B.DAILY, s2["full_before"], s2["open_until"])),
                ("eth", lambda: B.read_daily(ROOT / B.ETH, s1["full_before"], s1["open_until"])),
                ("universe", lambda: B.read_universe(ROOT / B.UNIVERSE)), ("cost", lambda: B.read_cost(ROOT / B.COST)),
                ("liquid", lambda: B.read_universe(ROOT / "backtest/liquid/any-other-file.json"))]

    def tagged(self, root=ROOT):
        w = world_with_top()
        data, uni, eth = B._Loaded(w.data), B._Loaded(w.uni), B._Loaded(w.eth)
        for x in (data, uni, eth):
            x.real_root = Path(root).resolve()
        return w, data, uni, eth

    def assert_all_refuse(self):
        self.assertIsNone(B._GATE)
        with OpenSpy(ROOT, boom=False) as spy:
            for name, call in self.real_calls():
                with self.subTest(call=name):
                    with self.assertRaises(B.Refused) as e:
                        call()
                    self.assertTrue(str(e.exception).startswith("refused: peek guard:"), str(e.exception))
            w, data, uni, eth = self.tagged()
            for stage in (1, 2):
                with self.subTest(evaluate=stage):
                    with self.assertRaises(B.Refused) as e:
                        B.evaluate(stage, data, uni, w.family, eth, w.fees, ch(), mondays=[M0])
                    self.assertIn("peek guard: evaluate()", str(e.exception))
                    with self.assertRaises(B.Refused):
                        B.simulate(stage, data, uni, w.family, eth, w.fees, ch(), mondays=[M0])
        self.assertEqual(spy.data_reads(), [], "a committed data file was opened beyond hashing")
        self.assertIsNone(B._GATE)

    def test_load_and_evaluate_refuse_the_real_files_today(self):
        self.assert_all_refuse()

    def test_still_refused_after_every_other_public_call(self):
        w = World(n=3)
        r, pins = make_root(w, APPROVE1)
        self.addCleanup(shutil.rmtree, r)
        calls = [lambda: B.refusal(ROOT, 1), lambda: B.refusal(ROOT, 2), lambda: B.hash_refusals(ROOT),
                 lambda: B.tool_sha(), lambda: B.run_approved_line(ROOT, 1), lambda: B.check(r, 1, pins),
                 lambda: B.refusal(r, 1, pins), lambda: B.load(r, 1), lambda: B.run(r, 1, now=M0, pins=pins),
                 lambda: B.run(r, 1, now=M0, pins=pins),  # refused: runs once
                 lambda: B.main(["--check", "--stage", "1", "--root", str(r)]),
                 lambda: B.main(["--run", "--stage", "2", "--root", str(r)])]
        for i, call in enumerate(calls):
            with self.subTest(call=i), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                try:
                    call()
                except B.Refused:
                    pass
            self.assert_all_refuse()

    def test_gate_closed_after_a_run_raises(self):
        w = World(n=3)
        for boom in ("load", "evaluate", "write_result", "read_cost"):
            with self.subTest(boom=boom):
                r, pins = make_root(w, APPROVE1)
                self.addCleanup(shutil.rmtree, r)
                with mock.patch.object(B, boom, side_effect=KeyError("boom")):
                    with self.assertRaises(KeyError):
                        B.run(r, 1, now=M0, pins=pins)
                self.assertIsNone(B._GATE)
                self.assert_all_refuse()

    def test_open_gate_admits_only_its_root_and_stage(self):
        w, data, uni, eth = self.tagged()
        with mock.patch.object(B, "_GATE", (ROOT.resolve(), 2)):
            with self.assertRaises(B.Refused):
                B.evaluate(1, data, uni, w.family, eth, w.fees, ch(), mondays=[M0])
            with OpenSpy(ROOT, boom=False) as spy:
                with self.assertRaises(B.Refused):  # stage 1's window while stage 2's gate is open
                    B.read_daily(ROOT / B.DAILY, B.STAGES[1]["full_before"], B.STAGES[1]["open_until"])
            self.assertEqual(spy.data_reads(), [])
        with mock.patch.object(B, "_GATE", (Path(tempfile.gettempdir()).resolve(), 1)):
            with self.assertRaises(B.Refused):
                B.evaluate(1, data, uni, w.family, eth, w.fees, ch(), mondays=[M0])

    def test_a_copy_of_a_committed_file_is_real(self):
        w = World(n=3)
        r, pins = make_root(w, APPROVE1)
        self.addCleanup(shutil.rmtree, r)
        with mock.patch.object(B, "_REAL_PINS", dict(B._REAL_PINS, **{B.UNIVERSE: pins[B.UNIVERSE]})):
            with self.assertRaises(B.Refused):
                B.read_universe(r / B.UNIVERSE)
            with self.assertRaises(B.Refused):
                B.load(r, 1)
        self.assertIsNotNone(B.read_universe(r / B.UNIVERSE))  # a synthetic file is not guarded
        data, *_ = B.load(r, 1)
        self.assertIsNone(data.real_root)

    def test_patching_pins_never_unguards(self):
        with mock.patch.object(B, "PINS", {}):
            with self.assertRaises(B.Refused):
                B.load(ROOT, 1)

    def test_gate_is_private(self):
        src = (ROOT / "tools/bl39.py").read_text()
        self.assertNotIn("__all__", src)
        self.assertEqual(src.count("_GATE = ("), 1)  # only run() opens it
        self.assertIn("    finally:\n        _GATE = None\n", src)
        for flag in ("--gate", "--open-gate", "--peek"):
            with self.subTest(flag=flag), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    B.main(["--check", flag])


class Family(unittest.TestCase):
    def setUp(self):
        self.fam, self.fees = family_file(), base_fees([tok(1), tok(2)])

    def test_ok_adds_ads(self):
        self.assertEqual(B.family_set(self.fam, self.fees), set(FAM) | {B.ADS})

    def refused(self, why):
        with self.assertRaises(B.Refused) as e:
            B.family_set(self.fam, self.fees)
        self.assertIn(why, str(e.exception))

    def test_member_not_ads_quoted(self):
        self.fees["tokens"][-1]["other_token"] = WETH
        self.refused("ADS-quoted tokens")

    def test_ads_quoted_non_member(self):
        self.fees["tokens"][0]["other_token"] = B.ADS
        self.refused("ADS-quoted tokens")

    def test_flag_mismatch(self):
        self.fees["tokens"][0]["family"] = True
        self.refused("family flags")

    def test_count(self):
        self.fam["members"].pop()
        self.refused("the spec says 39")

    def test_pool_mismatch(self):
        self.fam["members"][0]["pool"] = pool(tok(99))
        self.refused("pool is not the fee file's")

    def test_creator_and_ads(self):
        self.fam["creator"] = WETH
        self.refused("creator")
        self.fam = family_file()
        self.fam["ads"] = WETH
        self.refused("ads")

    def test_ads_in_fee_file(self):
        self.fees["tokens"].append(fee_entry(B.ADS))
        self.refused("ADS is in the fee file")

    def test_no_live_call(self):
        src = (ROOT / "tools/bl39.py").read_text()
        for word in ("gopluslabs", "token_security", "mainnet.base.org", "eth_call", "urlopen", "https://"):
            self.assertNotIn(word, src.lower())


class Seal(unittest.TestCase):
    """The owner's amendment 1: stage 1 loads only the open of the 2026-09-07 candle and nothing after."""

    def setUp(self):
        self.w = World(n=3)
        sep7 = B._ts("2026-09-07")
        for a in self.w.tokens:
            self.w.data[a]["c"][sep7] = (1.5, SENTINEL, SENTINEL, SENTINEL, SENTINEL)
            for d in range(1, 20):
                self.w.data[a]["c"][sep7 + d * D] = (SENTINEL,) * 5
        self.w.eth[sep7] = (2100.0, SENTINEL, SENTINEL, SENTINEL, SENTINEL)
        for d in range(1, 20):
            self.w.eth[sep7 + d * D] = (SENTINEL,) * 5
        self.root, self.pins = make_root(self.w)
        self.sep7 = sep7

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_stage1_load_keeps_only_the_sep7_open(self):
        data, uni, family, eth, fees = B.load(self.root, 1)
        for name, cs in [("eth", eth)] + [(a, data[a]["c"]) for a in data]:
            with self.subTest(series=name):
                self.assertEqual(max(cs), self.sep7)
                self.assertTrue(all(t < self.sep7 or t == self.sep7 for t in cs))
                self.assertIsNotNone(cs[self.sep7][0])
                self.assertEqual(cs[self.sep7][1:], (None, None, None, None))
                for t, row in cs.items():
                    if t < self.sep7:
                        self.assertTrue(all(x is not None for x in row))
        blob = repr((data, eth, uni))
        self.assertNotIn(str(SENTINEL), blob)
        self.assertNotIn("777.125", blob)

    def test_read_daily_window(self):
        cs = B.read_daily(self.root / B.ETH, self.sep7, self.sep7)
        (only,) = cs.values()
        self.assertEqual(only["c"][self.sep7], (2100.0, None, None, None, None))
        self.assertNotIn(self.sep7 + D, only["c"])
        self.assertNotIn("candles", {k for k, v in only.items() if v})

    def test_stage2_window(self):
        s2 = B.STAGES[2]
        data, *_ = B.load(self.root, 2)
        cs = data[self.w.tokens[0]]["c"]
        self.assertLessEqual(max(cs), s2["open_until"])
        for t, row in cs.items():
            if t >= s2["full_before"]:
                self.assertEqual(row[1:], (None, None, None, None))

    def test_stage1_constants(self):
        s1 = B.STAGES[1]
        self.assertEqual(B._day(s1["full_before"]), "2026-09-07")
        self.assertEqual(B._day(s1["open_until"]), "2026-09-07")
        self.assertEqual(s1["last_search_d"], 0)
        self.assertEqual([B._day(m) for m in s1["mondays"]][0], "2026-06-15")
        self.assertEqual(len(s1["mondays"]), 12)
        self.assertEqual(len(B.STAGES[2]["mondays"]), 8)

    def test_open_only_candle_never_used_for_a_stop(self):
        cs = {M0: (1.0, None, None, None, None)}
        with self.assertRaises(B.SealError):
            B.week_stop(cs, M0, 1.0)

    def test_universe_ignores_universe_json_reserve(self):
        self.assertNotIn(str(SENTINEL), repr(B.read_universe(self.root / B.UNIVERSE)))

    def test_meta_reader_reads_line_one_only(self):
        m = B.read_meta(self.root / B.DAILY)
        self.assertEqual(m["kind"], "meta")
        self.assertNotIn("candles", m)

    def test_check_prints_no_candle_value(self):
        out = json.dumps(B.check(self.root, 1, self.pins))
        self.assertNotIn(str(SENTINEL), out)
        self.assertNotIn("1.5", out)
        c = B.check(self.root, 1, self.pins)
        self.assertTrue(all(h["ok"] for h in c["hashes"].values()))
        self.assertTrue(c["family_check"].startswith("ok"))
        self.assertEqual(len(c["mondays"]), 12)
        self.assertEqual(c["open_questions"], [])
        self.assertEqual(c["tool_sha256"], B.tool_sha())
        self.assertFalse(c["attempt_record"]["exists"])
        self.assertIn("refused", c["run_now"])
        for k in ("picks", "max", "return", "pnl", "price"):
            self.assertNotIn(f'"{k}"', out.lower())


class Universe(unittest.TestCase):
    def setUp(self):
        self.w = World(n=3)
        self.a = self.w.tokens[0]

    def u(self):
        return B.universe_at(M0, self.w.data, self.w.uni, self.w.family)

    def test_flat_tokens_in(self):
        kept, before = self.u()
        self.assertEqual(set(kept), set(self.w.tokens))
        self.assertEqual(len(kept[self.a]), 30)

    def test_pool_age(self):
        self.w.uni[self.a]["created"] = M0 - 31 * D  # "at or before m - 31 d"
        self.assertIn(self.a, self.u()[0])
        self.w.uni[self.a]["created"] = M0 - 31 * D + 1
        self.assertNotIn(self.a, self.u()[0])
        self.w.uni[self.a]["created"] = None
        self.assertNotIn(self.a, self.u()[0])

    def test_all_31_candles(self):
        self.w.drop(self.a, M0 - 31 * D)
        self.assertNotIn(self.a, self.u()[0])
        w = World(n=1)
        w.drop(w.tokens[0], M0 - 32 * D)  # outside [m - 31 d, m)
        w.drop(w.tokens[0], M0)  # m itself is not in the lookback
        self.assertIn(w.tokens[0], B.universe_at(M0, w.data, w.uni, w.family)[0])

    def test_median_volume(self):
        for d in range(1, 16):  # 15 of the 30 days at 40k: median (40k + 100k) / 2 = 70k, in
            o = self.w.data[self.a]["c"][M0 - (31 - d) * D]
            self.w.data[self.a]["c"][M0 - (31 - d) * D] = o[:4] + (40_000.0,)
        self.assertIn(self.a, self.u()[0])
        o = self.w.data[self.a]["c"][M0 - 15 * D]
        self.w.data[self.a]["c"][M0 - 15 * D] = o[:4] + (40_000.0,)  # 16 of 30 at 40k: median 40k, out
        self.assertNotIn(self.a, self.u()[0])

    def test_the_lookback_day_m_minus_31_volume_is_not_in_the_median(self):
        o = self.w.data[self.a]["c"][M0 - 31 * D]
        self.w.data[self.a]["c"][M0 - 31 * D] = o[:4] + (0.0,)
        self.assertIn(self.a, self.u()[0])

    def test_last_7_days_10k(self):
        o = self.w.data[self.a]["c"][M0 - 7 * D]
        self.w.data[self.a]["c"][M0 - 7 * D] = o[:4] + (9_999.0,)
        self.assertNotIn(self.a, self.u()[0])
        w = World(n=1)
        a = w.tokens[0]
        o = w.data[a]["c"][M0 - 8 * D]
        w.data[a]["c"][M0 - 8 * D] = o[:4] + (9_999.0,)  # m - 8 d is outside [m - 7 d, m)
        self.assertIn(a, B.universe_at(M0, w.data, w.uni, w.family)[0])

    def test_family_excluded_but_counted_before(self):
        f = FAM[0]
        self.w.data[f] = {"pool": pool(f), "symbol": "F", "c": flat(START, 153)}
        self.w.uni[f] = {"pool": pool(f), "created": B._ts("2026-01-01"), "symbol": "F"}
        kept, before = self.u()
        self.assertNotIn(f, kept)
        self.assertIn(f, before)

    def test_zero_close_is_a_missing_number(self):
        self.w.set(self.a, M0 - 5 * D, 0.0)
        self.assertNotIn(self.a, self.u()[0])

    def test_universe_token_without_fee_entry_refused(self):
        self.w.extra_fees = []
        w = World(n=2)
        w.tokens.append(tok(77))
        w.data[tok(77)] = {"pool": pool(tok(77)), "symbol": "X", "c": flat(START, 153)}
        w.uni[tok(77)] = {"pool": pool(tok(77)), "created": B._ts("2026-01-01"), "symbol": "X"}
        fees = base_fees(w.tokens[:2])
        with self.assertRaises(B.Refused):
            B.simulate(1, w.data, w.uni, w.family, w.eth, fees, ch(), mondays=[M0])


class MaxAndPicks(unittest.TestCase):
    def test_max_is_the_largest_close_to_close_return(self):
        w = World(n=1)
        a = w.tokens[0]
        w.set(a, M0 - 10 * D, 1.1)
        w.set(a, M0 - 9 * D, 1.32)
        rets = B.universe_at(M0, w.data, w.uni, w.family)[0][a]
        self.assertAlmostEqual(max(rets), 0.2)
        w.set(a, M0 - 31 * D, 0.5)  # the first of the 31 closes: its return is not in the 30
        rets = B.universe_at(M0, w.data, w.uni, w.family)[0][a]
        self.assertAlmostEqual(rets[0], 1.0)
        self.assertEqual(len(rets), 30)

    def test_k_and_ties(self):
        for n, k in ((9, 1), (10, 1), (11, 2), (20, 2), (21, 3)):
            with self.subTest(n=n):
                w = World(n=n)
                s = w.sim()
                self.assertEqual(s["weeks"][0]["k"], k)
                self.assertEqual(s["weeks"][0]["chosen"], w.tokens[:k])  # all MAX 0: ties by lower address

    def test_highest_max_first(self):
        w = world_with_top()
        s = w.sim()
        self.assertEqual(s["weeks"][0]["chosen"], [TOP, w.tokens[0]])
        self.assertEqual(s["weeks"][0]["max_last_day"], [w.tokens[0]])  # flat: max 0 on every day incl. the last

    def test_max_last_day_line(self):
        w = world_with_top()
        w.jump(TOP, M0 - 3 * D, 1.0)
        w.set(TOP, M0 - D, 1.3)
        s = w.sim()
        self.assertIn(TOP, s["weeks"][0]["max_last_day"])


class Legs(unittest.TestCase):
    def test_leg_start_and_exit_at_open(self):
        w = world_with_top()
        w.set(TOP, M0, 1.0, c=1.05)
        w.level(TOP, M0 + D, 1.05)
        w.set(TOP, M0 + 7 * D, 1.1)
        (L,) = legs_of(w.sim(), TOP)
        self.assertEqual((L["start_t"], L["start_px"], L["end"], L["end_t"], L["end_px"]),
                         (M0, 1.0, "exit", M0 + 7 * D, 1.1))
        self.assertTrue(L["buy"] and L["sell"])

    def test_no_fill(self):
        w = world_with_top()
        w.drop(TOP, M0)
        s = w.sim()
        self.assertEqual(legs_of(s, TOP), [])
        self.assertEqual(s["weeks"][0]["no_fill"], [TOP])
        self.assertEqual(legs_of(s, TOP, "bench"), [])
        r = w.evaluate()
        self.assertEqual(r["reported"]["no_fill"], 1)
        self.assertEqual(r["reported"]["picks_lost_no_fill"], 1)
        self.assertEqual(r["tests"]["1"]["closed_legs"], 1)  # the no_fill is not a leg

    def test_exit_gap_search_then_close(self):
        w = world_with_top()
        w.set(TOP, M0 + 6 * D, 1.0, c=0.9)
        for d in (7, 8):
            w.drop(TOP, M0 + d * D)
        w.set(TOP, M0 + 9 * D, 1.4)
        (L,) = legs_of(w.sim(mondays=[M0], stage=2), TOP)  # stage 2's last Monday searches 3 days
        self.assertEqual((L["end"], L["end_t"], L["end_px"]), ("exit", M0 + 9 * D, 1.4))
        w.drop(TOP, M0 + 9 * D)
        w.drop(TOP, M0 + 10 * D)
        (L,) = legs_of(w.sim(mondays=[M0], stage=2), TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"]), ("exit_gap", M0 + 6 * D, 0.9))
        self.assertTrue(L["end_at_close"] and L["sell"])

    def test_search_inside_a_stage_is_3_days(self):
        """A non-last Monday's leg (not picked again) searches the next 3 days for an open, then the last close."""
        w = world_with_top()
        for a in w.tokens[1:3]:  # two tokens jump inside m + 7 d's lookback: they are the picks at m + 7 d
            w.set(a, M0 + 4 * D, 1.6)
        w.set(TOP, M0 + 6 * D, 1.0, c=0.9)
        for d in (7, 8, 9):
            w.drop(TOP, M0 + d * D)
        w.set(TOP, M0 + 10 * D, 1.3)  # the third day after the missing m + 7 d candle
        s = w.sim(mondays=[M0, M0 + 7 * D])
        self.assertNotIn(TOP, s["weeks"][1]["chosen"])
        (L,) = legs_of(s, TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"], L["sell"]), ("exit", M0 + 10 * D, 1.3, True))
        w.drop(TOP, M0 + 10 * D)
        w.set(TOP, M0 + 11 * D, 1.5)  # a fourth day is not searched
        (L,) = legs_of(w.sim(mondays=[M0, M0 + 7 * D]), TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"]), ("exit_gap", M0 + 6 * D, 0.9))

    def test_stage1_last_monday_never_searches(self):
        w = world_with_top()
        w.set(TOP, M0 + 6 * D, 1.0, c=0.95)
        w.drop(TOP, M0 + 7 * D)
        w.set(TOP, M0 + 8 * D, 1.5)
        (L,) = legs_of(w.sim(mondays=[M0], stage=1), TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"]), ("exit_gap", M0 + 6 * D, 0.95))

    def test_roll_without_friction(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.1)
        w.level(TOP, M0 + 14 * D, 1.21)
        s = w.sim(mondays=[M0, M0 + 7 * D])
        l1, l2 = sorted(legs_of(s, TOP), key=lambda L: L["start_t"])
        self.assertEqual((l1["end"], l1["sell"], l1["buy"]), ("roll", False, True))
        self.assertEqual((l2["rolled"], l2["buy"], l2["sell"], l2["start_px"]), (True, False, True, 1.1))
        f = 0.8 / 100  # 0.3% pool + 0.5%
        self.assertAlmostEqual(l1["net"], 1.1 / (1 * (1 + f)) - 1)
        self.assertAlmostEqual(l2["net"], 1.21 * (1 - f) / 1.1 - 1)
        self.assertAlmostEqual(l1["gross"], 0.1)
        v1 = w.sim(mondays=[M0, M0 + 7 * D], side_pct=lambda a: 4.0, rebuy=True)
        k1, k2 = sorted(legs_of(v1, TOP), key=lambda L: L["start_t"])
        self.assertAlmostEqual(k1["net"], 1.1 * 0.96 / 1.04 - 1)  # v1: a real sell and a real buy at the roll
        self.assertAlmostEqual(k2["net"], 1.21 * 0.96 / (1.1 * 1.04) - 1)

    def test_no_roll_across_stage_end(self):
        w = world_with_top()
        s = w.sim(mondays=[M0])
        (L,) = legs_of(s, TOP)
        self.assertTrue(L["sell"])
        self.assertEqual(L["end"], "exit")

    def roll_gap_world(self):
        w = world_with_top()
        w.set(TOP, M0 + 6 * D, 1.0, c=0.97)
        w.drop(TOP, M0 + 7 * D)
        w.set(TOP, M0 + 8 * D, 1.2)
        return w

    def test_roll_with_missing_candle_q2_search(self):
        w = self.roll_gap_world()
        s = w.sim(ch(Q2="search"), mondays=[M0, M0 + 7 * D])
        (L,) = legs_of(s, TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"], L["sell"]), ("exit", M0 + 8 * D, 1.2, True))
        self.assertEqual(s["weeks"][1]["no_fill"], [TOP])

    def test_roll_with_missing_candle_q2_close(self):
        w = self.roll_gap_world()
        s = w.sim(ch(Q2="close"), mondays=[M0, M0 + 7 * D])
        (L,) = legs_of(s, TOP)
        self.assertEqual((L["end"], L["end_t"], L["end_px"], L["sell"]), ("exit_gap", M0 + 6 * D, 0.97, True))
        self.assertEqual(s["weeks"][1]["no_fill"], [TOP])


class Stop(unittest.TestCase):
    def one(self, rows, choices=None):
        w = world_with_top()
        for t, row in rows.items():
            w.set(TOP, t, *row)
        (L,) = legs_of(w.sim(choices), TOP)
        return L

    def test_day_one_at_level(self):
        L = self.one({M0: (1.0, 1.0, 0.6, 0.8)})
        self.assertEqual((L["end"], L["end_t"]), ("stop", M0))
        self.assertAlmostEqual(L["end_px"], 0.7)

    def test_day_one_close_below_level(self):
        L = self.one({M0: (1.0, 1.0, 0.5, 0.65)})
        self.assertAlmostEqual(L["end_px"], 0.65)

    def test_gap_open_below_level(self):
        L = self.one({M0 + 2 * D: (0.5, 0.9, 0.4, 0.8)})
        self.assertEqual((L["end"], L["end_t"], L["end_px"]), ("stop", M0 + 2 * D, 0.5))

    def test_low_exactly_at_level(self):
        L = self.one({M0 + 3 * D: (1.0, 1.0, 0.7, 0.9)})
        self.assertEqual(L["end"], "stop")
        L = self.one({M0 + 3 * D: (1.0, 1.0, 0.7001, 0.9)})
        self.assertEqual(L["end"], "exit")

    def test_rolled_leg_stops_from_its_open(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 2.0)
        w.set(TOP, M0 + 9 * D, 2.0, 2.0, 1.39, 1.5)  # 1.39 < 0.7 x 2.0
        s = w.sim(mondays=[M0, M0 + 7 * D])
        l2 = [L for L in legs_of(s, TOP) if L["start_t"] == M0 + 7 * D][0]
        self.assertEqual((l2["end"], l2["stop_ref"]), ("stop", 2.0))
        self.assertAlmostEqual(l2["end_px"], 1.4)
        self.assertTrue(l2["sell"])

    def test_q5_open(self):
        L = self.one({M0: (1.0, 1.0, 0.705, 0.9)}, ch(Q5="open"))
        self.assertEqual(L["end"], "exit")

    def test_q5_fill_picks(self):
        L = self.one({M0: (1.0, 1.0, 0.705, 0.9)}, ch(Q5="fill_picks"))
        self.assertEqual(L["end"], "stop")
        self.assertAlmostEqual(L["stop_ref"], 1.008)
        w = world_with_top()
        w.set(TOP, M0, 1.0, 1.0, 0.705, 0.9)
        (b,) = legs_of(w.sim(ch(Q5="fill_picks")), TOP, "bench")
        self.assertEqual(b["end"], "exit")

    def test_q5_fill_both(self):
        w = world_with_top()
        w.set(TOP, M0, 1.0, 1.0, 0.705, 0.9)
        (b,) = legs_of(w.sim(ch(Q5="fill_both")), TOP, "bench")
        self.assertEqual(b["end"], "stop")

    def stopped_world(self):
        """TOP stops in week 1 (a pick); B, not a pick in week 1, stops in its benchmark leg and has the top MAX at
        m + 7 d."""
        w = world_with_top()
        w.set(TOP, M0 + 2 * D, 1.0, 1.0, 0.6, 0.65)
        w.level(TOP, M0 + 3 * D, 0.65)
        b = w.tokens[-2]  # tokens[-1] is TOP
        self.assertNotEqual(b, TOP)
        w.set(b, M0 + 5 * D, 1.5)
        w.set(b, M0 + 6 * D, 1.5, 1.5, 0.6, 1.5)  # low under 0.7 x 1.0: its benchmark leg stops; close 1.5
        w.level(b, M0 + 7 * D, 1.5)
        return w, b

    def test_stopped_pick_not_eligible_slot_refilled(self):
        w, b = self.stopped_world()
        s = w.sim(ch(Q1="pick_leg"), mondays=[M0, M0 + 7 * D])
        wk = s["weeks"][1]
        self.assertNotIn(TOP, wk["chosen"])
        self.assertIn(TOP, wk["refilled"])
        self.assertEqual(wk["n"], 12)  # TOP stays in n
        self.assertIn(TOP, [L["token"] for L in s["bench"] if L["start_t"] == M0 + 7 * D])  # and in the benchmark
        self.assertIn(b, wk["chosen"])  # pick_leg: B's benchmark stop does not count

    def test_q1_any_leg(self):
        w, b = self.stopped_world()
        s = w.sim(ch(Q1="any_leg"), mondays=[M0, M0 + 7 * D])
        wk = s["weeks"][1]
        self.assertNotIn(b, wk["chosen"])
        self.assertNotIn(TOP, wk["chosen"])
        self.assertEqual(set(wk["refilled"]), {TOP, b})
        r = w.evaluate(ch(Q1="any_leg"), mondays=[M0, M0 + 7 * D])
        self.assertEqual(r["reported"]["slots_refilled_after_stop"], 2)


class FrictionAndEth(unittest.TestCase):
    def test_route(self):
        self.assertAlmostEqual(B.route(fee_entry("a", "0.3"))[0], 0.8)
        hop = {"fee_pct": "0.3", "unknown": False}
        self.assertAlmostEqual(B.route(fee_entry("a", "0.01", hops=[hop, hop]))[0], 1.11)
        self.assertAlmostEqual(B.route(fee_entry("a", unknown=True))[0], 1.5)
        unk = {"fee_pct": None, "unknown": True}
        self.assertAlmostEqual(B.route(fee_entry("a", "0.3", hops=[unk]))[0], 1.8)
        self.assertAlmostEqual(B.route(fee_entry("a", unknown=True, hops=[hop]))[0], 1.8)
        f = B.route(fee_entry("a", "0.3", hooks="0x" + "1" * 40, may_differ=True))[1]
        self.assertTrue(f["hook"] and f["may_differ"])
        self.assertFalse(B.route(fee_entry("a", "0.3", hooks=B.ZERO))[1]["hook"])

    def test_net_on_real_buy_and_sell(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.2)
        (L,) = legs_of(w.sim(), TOP)
        self.assertAlmostEqual(L["net"], 1.2 * (1 - 0.008) / (1 * 1.008) - 1)
        self.assertAlmostEqual(L["pnl"], 5 * L["net"])
        self.assertAlmostEqual(L["gross"], 0.2)

    def test_unknown_fee_is_1pct(self):
        w = world_with_top()
        w.extra_fees = [fee_entry(TOP, unknown=True)]
        w.level(TOP, M0 + 7 * D, 1.2)
        (L,) = legs_of(w.sim(), TOP)
        self.assertAlmostEqual(L["fric_pct"], 1.5)
        self.assertAlmostEqual(L["net"], 1.2 * 0.985 / 1.015 - 1)

    def test_half_and_double(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.2)
        r = w.evaluate()
        rep = r["reported"]
        nets = {k: rep[k]["mean_net"] for k in ("friction_half", "friction_double")}
        top = lambda f, base: (1.2 * (1 - f) / (1 + f) - 1 + (1 - base) / (1 + base) - 1) / 2  # TOP + one flat pick
        self.assertAlmostEqual(nets["friction_half"], top(0.004, 0.004))
        self.assertAlmostEqual(nets["friction_double"], top(0.016, 0.016))
        self.assertAlmostEqual(rep["v1_line"]["mean_net"], top(0.04, 0.04))

    def test_benchmark_is_gross_equal_weight(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.2)
        s = w.sim()
        self.assertEqual(len(s["bench"]), 12)
        self.assertTrue(all(L["net"] == L["gross"] for L in s["bench"]))
        rows = B.week_table(s)
        self.assertAlmostEqual(rows[0]["B_w"], 0.2 / 12)
        self.assertAlmostEqual(rows[0]["P_w"], 0.1)

    def test_hold_eth(self):
        w = world_with_top()
        w.eth[M0 + 7 * D] = (2200.0,) * 4 + (0.0,)
        (L,) = legs_of(w.sim(), TOP)
        self.assertAlmostEqual(L["eth"], 0.1)
        w.set(TOP, M0 + 2 * D, 1.0, 1.0, 0.5, 0.6)
        w.eth[M0 + 2 * D] = (2000.0, 2000.0, 1700.0, 1800.0, 0.0)
        (L,) = legs_of(w.sim(), TOP)
        self.assertAlmostEqual(L["eth"], -0.1)  # a stop: that day's close

    def test_hold_eth_exit_gap_close(self):
        w = world_with_top()
        w.drop(TOP, M0 + 7 * D)
        w.eth[M0 + 6 * D] = (2000.0, 2000.0, 2000.0, 1900.0, 0.0)
        (L,) = legs_of(w.sim(), TOP)
        self.assertEqual(L["end"], "exit_gap")
        self.assertAlmostEqual(L["eth"], -0.05)

    def test_missing_eth_refused(self):
        w = world_with_top()
        w.eth.pop(M0 + 7 * D)
        with self.assertRaises(B.Refused):
            w.sim()


def leg(pnl, eth=0.0, t=M0):
    return {"pnl": pnl, "net": pnl / 5, "eth": eth, "end_t": t}


class PassTests(unittest.TestCase):
    def test_test2_and_without_top2(self):
        x = B.tests_2_4([leg(10), leg(5), leg(1), leg(1)], ch())
        self.assertTrue(x["2"])
        self.assertAlmostEqual(x["mean_pnl_without_top2"], 1)
        x = B.tests_2_4([leg(10), leg(5), leg(-1), leg(-1)], ch())
        self.assertGreater(x["mean_pnl"], 0)
        self.assertFalse(x["2"])
        self.assertFalse(B.tests_2_4([leg(-1)] * 5, ch())["2"])

    def test_test4(self):
        self.assertTrue(B.tests_2_4([leg(1, eth=0.1)] * 3, ch())["4"])  # net 0.2 > 0.1
        self.assertFalse(B.tests_2_4([leg(1, eth=0.3)] * 3, ch())["4"])

    def test_q11(self):
        self.assertFalse(B.tests_2_4([], ch(Q11="fail"))["2"])
        self.assertFalse(B.tests_2_4([leg(1), leg(1)], ch(Q11="fail"))["2"])
        self.assertIsNone(B.tests_2_4([], ch(Q11="not_evaluable"))["2"])
        self.assertIsNone(B.tests_2_4([], ch(Q11="not_evaluable"))["4"])
        self.assertIsNone(B.tests_2_4([leg(1), leg(1)], ch(Q11="not_evaluable"))["2"])

    def rows(self, pairs):
        return [{"P_w": p, "B_w": b} for p, b in pairs]

    def test_test3_count_and_mean(self):
        r = self.rows([(0.1, 0.0)] * 8 + [(0.0, 0.05)] * 4)
        t = B.test3(r, ch(), 8)
        self.assertEqual(t["weeks_won"], 8)
        self.assertTrue(t["pass"])
        r = self.rows([(0.1, 0.0)] * 7 + [(0.0, 0.0)] * 5)  # equal = not won
        self.assertFalse(B.test3(r, ch(), 8)["pass"])
        r = self.rows([(0.01, 0.0)] * 8 + [(0.0, 0.5)] * 4)  # 8 wins but mean < 0
        self.assertFalse(B.test3(r, ch(), 8)["pass"])
        self.assertEqual(B.test3(self.rows([(0.1, 0.0)] * 6 + [(0.0, 0.1)] * 2), ch(), 6)["weeks_won"], 6)

    def test_test3_q3(self):
        r = self.rows([(0.1, 0.0)] * 8 + [(None, 0.5)] * 4)  # 4 weeks without a pick leg
        self.assertAlmostEqual(B.test3(r, ch(Q3="picked_weeks"), 8)["mean_diff"], 0.1)
        self.assertAlmostEqual(B.test3(r, ch(Q3="zero_diff"), 8)["mean_diff"], 0.8 / 12)
        self.assertAlmostEqual(B.test3(r, ch(Q3="cash_zero"), 8)["mean_diff"], (0.8 - 2.0) / 12)
        self.assertEqual(B.test3(r, ch(), 8)["weeks_won"], 8)
        r = self.rows([(0.1, 0.0)] * 7 + [(None, -0.5)])
        self.assertEqual(B.test3(r, ch(), 8)["weeks_won"], 7)  # a week with no pick leg counts as P_w <= B_w

    def test_drawdown(self):
        legs = [leg(5, t=M0), leg(-11, t=M0 + D), leg(-5, t=M0 + 2 * D), leg(20, t=M0 + 3 * D)]
        self.assertAlmostEqual(B.max_drawdown(legs), 16 / 55)  # peak 55, low 39
        legs = [leg(-8, t=M0), leg(-8, t=M0)]  # one exit date, summed
        self.assertAlmostEqual(B.max_drawdown(legs), 16 / 50)
        self.assertEqual(B.max_drawdown([]), 0.0)
        self.assertTrue(B.max_drawdown([leg(-14.9)]) < 0.30 <= B.max_drawdown([leg(-15)]))

    def test_test1_bar(self):
        self.assertEqual((B.STAGES[1]["min_legs"], B.STAGES[2]["min_legs"]), (40, 30))
        self.assertEqual((B.STAGES[1]["weeks_needed"], B.STAGES[2]["weeks_needed"]), (8, 6))


def T(t1=True, t2=True, t3=True, t4=True, t5=True):
    return {"1": t1, "2": t2, "3": t3, "4": t4, "5": t5}


class Outcome(unittest.TestCase):
    def test_pass(self):
        v, n = B.outcome(T(), 1, ch())
        self.assertEqual(v, "pass")
        self.assertIn("stage 2 may run once", n)
        self.assertIn("never 'proven'", n)
        self.assertIn("forward paper", B.outcome(T(), 2, ch())[1])

    def test_retired_whatever_the_leg_count(self):
        for t in (T(t2=False), T(t3=False), T(t4=False), T(t1=False, t2=False), T(t1=False, t4=False, t5=False)):
            with self.subTest(t=t):
                v, n = B.outcome(t, 1, ch())
                self.assertEqual(v, "retired")
                self.assertIn("stage 2 is never read", n)

    def test_inconclusive(self):
        v, n = B.outcome(T(t1=False), 1, ch())
        self.assertEqual(v, "inconclusive")
        self.assertIn("stage 2 is not run and stays unspent", n)

    def test_drawdown_only(self):
        v, n = B.outcome(T(t5=False), 1, ch())
        self.assertEqual(v, "not passed: drawdown")
        self.assertIn("stage 2 is not run", n)
        self.assertIn("forward paper vs the benchmark, never money", n)

    def test_q4(self):
        self.assertEqual(B.outcome(T(t1=False, t5=False), 1, ch(Q4="inconclusive"))[0], "inconclusive")
        self.assertEqual(B.outcome(T(t1=False, t5=False), 1, ch(Q4="drawdown"))[0], "not passed: drawdown")

    def test_q11_not_evaluable(self):
        self.assertEqual(B.outcome(T(t1=False, t2=None, t4=None), 1, ch(Q11="not_evaluable"))[0], "inconclusive")


class Reported(unittest.TestCase):
    def test_counts(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.1)
        r = w.evaluate(mondays=[M0, M0 + 7 * D])
        rep = r["reported"]
        self.assertEqual(rep["legs"], 4)
        self.assertEqual(rep["distinct_tokens"], 2)
        self.assertEqual(rep["legs_per_token"], {"mean": 2.0, "max": 2})
        self.assertAlmostEqual(rep["roll_share"], 0.5)
        self.assertEqual((rep["no_fill"], rep["exit_gap"], rep["stops"]), (0, 0, 0))
        self.assertEqual(len(r["weeks"]), 2)
        self.assertNotIn("legs", {k for k in r if k == "legs" and not r[k]})

    def test_q6_eth_7d_before(self):
        w = world_with_top()
        w.eth[M0 - 8 * D] = (2000.0, 2000.0, 2000.0, 1600.0, 0.0)
        w.eth[M0 - 7 * D] = (2000.0, 2000.0, 2000.0, 2000.0, 0.0)
        w.eth[M0 - D] = (2000.0, 2000.0, 2000.0, 2400.0, 0.0)
        w.eth[M0] = (1800.0, 1800.0, 1800.0, 1800.0, 0.0)
        got = {q: w.evaluate(ch(Q6=q))["weeks"][0]["eth_7d_before"] for q in B.OPTIONS["Q6"]}
        self.assertAlmostEqual(got["open_to_open"], -0.1)
        self.assertAlmostEqual(got["close_to_close"], 0.5)
        self.assertAlmostEqual(got["open_to_close"], 0.2)
        r = w.evaluate(ch(Q6="open_to_open"))
        self.assertEqual(r["reported"]["by_eth_7d_before_m"]["< 0"]["weeks"], 1)

    def test_diff_by_eth_leg_week(self):
        w = world_with_top()
        w.eth[M0 + 7 * D] = (2100.0,) * 4 + (0.0,)
        r = w.evaluate()
        self.assertAlmostEqual(r["weeks"][0]["eth_leg_week"], 0.05)
        self.assertEqual(r["reported"]["diff_by_eth_leg_week"][">= 0"]["weeks"], 1)

    def vol_world(self):
        w = World(n=11)  # 12 tokens with TOP; token i gets swings of size i
        w.tokens.append(TOP)
        w.data[TOP] = {"pool": pool(TOP), "symbol": "TOP", "c": flat(START, 153)}
        w.uni[TOP] = {"pool": pool(TOP), "created": B._ts("2026-01-01"), "symbol": "TOP"}
        for i, a in enumerate(w.tokens):
            for d in range(2, 30, 2):
                w.set(a, M0 - d * D, 1 + 0.01 * (i + 1))
        for a in w.tokens:
            w.level(a, M0, 1.0)
        w.level(w.tokens[-1], M0, 1.0)
        w.level(w.tokens[-1], M0 + 7 * D, 1.3)
        return w

    def test_q7_top_third_rounding(self):
        w = World(n=10)
        for i, a in enumerate(w.tokens):
            for d in range(2, 30, 2):
                w.set(a, M0 - d * D, 1 + 0.01 * (i + 1))
        w.level(w.tokens[-1], M0 + 7 * D, 1.3)
        for q, n3 in (("simple_ceil", 4), ("simple_floor", 3), ("log_ceil", 4), ("log_floor", 3)):
            with self.subTest(q=q):
                r = w.evaluate(ch(Q7=q))
                wk = r["reported"]["B_w_top_third_vol"]["weeks"][0]
                self.assertEqual(wk["bench_legs"], n3)
                self.assertAlmostEqual(wk["B_w"], 0.3 / n3)  # the most volatile token is in the top third

    def test_q7_log_can_rank_differently(self):
        # simple returns: a swings +100% / -50% (sd high); b swings +60% / -60%... log returns rank b above a or tie
        a = [1.0, -0.5] * 15
        b = [0.7, -0.6] * 15
        sd = __import__("statistics").pstdev
        simple = sd(a) > sd(b)
        log = sd([math.log1p(x) for x in a]) > sd([math.log1p(x) for x in b])
        self.assertNotEqual(simple, log)

    def test_b_w_without_low_fee_pools(self):
        w = world_with_top()
        lo = w.tokens[0]
        w.extra_fees = [fee_entry(lo, "0.05"), fee_entry(w.tokens[1], "0.01"), fee_entry(w.tokens[2], unknown=True)]
        w.level(lo, M0 + 7 * D, 2.0)
        r = w.evaluate()
        wk = r["reported"]["B_w_without_001_005"]["weeks"][0]
        self.assertEqual(wk["bench_legs"], 10)  # 12 - 2 low-fee pools; the unknown-fee pool is kept
        self.assertAlmostEqual(wk["B_w"], 0.0)

    def test_q8_flagged_legs_double(self):
        w = world_with_top()
        hop = {"fee_pct": None, "unknown": True}
        w.extra_fees = [fee_entry(TOP, "0.3", hops=[hop])]  # only an unknown hop
        w.level(TOP, M0 + 7 * D, 1.2)
        r1 = w.evaluate(ch(Q8="pool_only"))["reported"]["flagged_pools_double"]
        r2 = w.evaluate(ch(Q8="pool_or_hop"))["reported"]["flagged_pools_double"]
        self.assertEqual(r1["legs"], 0)
        self.assertEqual(r2["legs"], 1)
        f = 2 * (0.3 + 1.0 + 0.5) / 100
        self.assertAlmostEqual(r2["mean_net"], 1.2 * (1 - f) / (1 + f) - 1)
        self.assertFalse(r2["test2"])  # one leg: no leg left without the top 2 (Q11 fail)
        self.assertTrue(r2["test4"])
        w.extra_fees = [fee_entry(TOP, "0.3", may_differ=True)]
        self.assertEqual(w.evaluate(ch(Q8="pool_only"))["reported"]["flagged_pools_double"]["legs"], 1)
        w.extra_fees = [fee_entry(TOP, "0.3", hooks="0x" + "2" * 40)]
        self.assertEqual(w.evaluate(ch(Q8="pool_only"))["reported"]["flagged_pools_double"]["legs"], 1)
        w.extra_fees = [fee_entry(TOP, unknown=True)]
        self.assertEqual(w.evaluate(ch(Q8="pool_only"))["reported"]["flagged_pools_double"]["legs"], 1)

    def test_q9_options(self):
        w = world_with_top()
        w.level(TOP, M0 + 7 * D, 1.2)
        measured = {TOP: 0.1}
        self.assertIn("no Q9s measured cost", w.evaluate()["reported"]["bl45_measured"])
        r = w.evaluate(ch(Q9="replace_metered"), measured=measured)["reported"]["bl45_measured"]
        top = 1.2 * 0.999 / 1.001 - 1
        flat_ = 0.992 / 1.008 - 1
        self.assertEqual(r["legs"], 2)
        self.assertAlmostEqual(r["mean_net"], (top + flat_) / 2)
        r = w.evaluate(ch(Q9="metered_legs_only"), measured=measured)["reported"]["bl45_measured"]
        self.assertEqual(r["legs"], 1)
        self.assertAlmostEqual(r["mean_net"], top)
        r = w.evaluate(ch(Q9="side_by_side"), measured=measured)["reported"]["bl45_measured"]
        self.assertEqual(r, [{"token": TOP, "spec_pct": 0.8, "measured_pct": 0.1}])

    def test_q10_joined(self):
        w = world_with_top()
        late = tok(60)
        w.tokens.append(late)
        w.data[late] = {"pool": pool(late), "symbol": "L", "c": flat(START, 153)}
        w.uni[late] = {"pool": pool(late), "created": M0 - 31 * D, "symbol": "L"}  # joins exactly at M0
        w.jump(late, M0 - 2 * D, 1.5)
        r = w.evaluate(ch(Q10="compute"))
        self.assertEqual(r["reported"]["picks_joined_last_7d"][0], {"monday": "2026-06-15", "joined": 1})
        r = w.evaluate(ch(Q10="na_first"))
        self.assertEqual(r["reported"]["picks_joined_last_7d"][0], {"monday": "2026-06-15", "joined": None})
        r = w.evaluate(ch(Q10="na_first"), mondays=[M0 - 7 * D, M0])
        self.assertEqual(r["reported"]["picks_joined_last_7d"][1]["joined"], 1)  # later Mondays always computed

    def test_exit_gap_at_minus_100(self):
        w = world_with_top()
        w.drop(TOP, M0 + 7 * D)
        r = w.evaluate()["reported"]["exit_gap_at_minus_100"]
        self.assertEqual(r["exit_gap_legs"], 1)
        self.assertAlmostEqual(r["mean_pnl"], (-5 + 5 * (0.992 / 1.008 - 1)) / 2)
        self.assertFalse(r["test2"])

    def test_every_always_reported_line_present(self):
        w = world_with_top()
        rep = w.evaluate()["reported"]
        for k in ("legs", "distinct_tokens", "legs_per_token", "roll_share", "no_fill", "exit_gap", "stops",
                  "mean_pnl_without_top2", "by_eth_7d_before_m", "friction_half", "friction_double", "bl45_measured",
                  "v1_line", "picks_joined_last_7d", "diff_by_eth_leg_week", "B_w_without_001_005",
                  "B_w_top_third_vol", "picks_max_last_day", "flagged_pools_double", "exit_gap_at_minus_100",
                  "picks_lost_no_fill", "slots_refilled_after_stop"):
            self.assertIn(k, rep)


class FullStage(unittest.TestCase):
    """The whole stage-1 path on synthetic files: load (sealed) -> evaluate -> write once."""

    def test_stage1_end_to_end(self):
        w = world_with_top(n=21)
        for i, a in enumerate(w.tokens):
            for d in range(0, 84, 9):
                w.set(a, M0 + d * D, 1.0 + 0.003 * ((i * 7 + d) % 11))
        sep7 = B._ts("2026-09-07")
        for a in w.tokens:
            w.data[a]["c"][sep7 + D] = (SENTINEL,) * 5
        root, pins = make_root(w, APPROVE1)
        try:
            data, uni, family, eth, fees = B.load(root, 1)
            r = B.evaluate(1, data, uni, family, eth, fees, ch())
            self.assertEqual(len(r["weeks"]), 12)
            self.assertEqual(r["weeks"][-1]["monday"], "2026-08-31")
            self.assertTrue(all(L["end_t"] <= sep7 for L in r["legs"]))
            self.assertIn(r["verdict"], ("pass", "retired", "inconclusive", "not passed: drawdown"))
            self.assertNotIn(str(SENTINEL), json.dumps(r, default=str))
            d = B.write_result(root, 1, r, M0, ch())
            s = json.loads((d / "summary.json").read_text())
            self.assertEqual(s["sha256"], B.PINS[B.SPEC])
            self.assertEqual(s["tests"]["1"]["need"], 40)
            self.assertIn("runs once", B.refusal(root, 1, pins, ch()))
        finally:
            shutil.rmtree(root)


class Isolation(unittest.TestCase):
    def test_no_network_module_imported(self):
        code = ("import sys, tools.bl39; bad = [m for m in ('urllib.request', 'http.client', 'socket', 'ssl', "
                "'tools.candles', 'core.tick') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_source_reads_nothing_sealed(self):
        src = (ROOT / "tools/bl39.py").read_text()
        for word in ("backtest/costs", "snapshots", "backtest/candles", "outcomes.csv", "tasks.csv", "trades.csv",
                     "holdout", "bl34", "import urllib", "import socket", "import http"):
            self.assertNotIn(word, src)

    def test_core_and_workflows_never_use_it(self):
        for p in (ROOT / "core").glob("*.py"):
            self.assertNotIn("bl39", p.read_text(), p.name)
        for p in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("bl39", p.read_text(), p.name)

    def test_spec_quotes_present(self):
        """Every rule block quotes the frozen text: a few anchor lines must appear verbatim in the source."""
        src = " ".join((ROOT / "tools/bl39.py").read_text().split())
        for q in ("all 31 daily candles in [m - 31 d, m)", "MAX = the largest of the 30 close-to-close daily returns",
                  "k = ceil(n / 10), n = the universe size at m", "ties by lower address (lowercase hex)",
                  "no candle that day = no_fill, counted", "with no friction on either side",
                  "A low at or below it exits at the lowest of the stop level, that day's open and that day's close",
                  "the fee tiers of the route from USDC", "Unknown fee", "= 1%", "same LEG, ROLL and STOP rules, gross",
                  "ETH's daily open on the leg's start day", "A week with no closed pick leg counts as P_w <= B_w",
                  "Only 5 fails = not passed: drawdown", "would otherwise pass but has fewer legs than (1) is "
                  "inconclusive", "v1's line (4% a side, re-buy every week)", "exit_gap legs at -100%",
                  "max drawdown < 30% on a 50 USD book"):
            self.assertIn(q, src)


if __name__ == "__main__":
    unittest.main()
