"""Offline tests for BL-30: the dead flag (on best_qliq), sight/label pair addresses, token age at sight, and replay.

Logging only: no rule, verdict or exit reads any of it. No network.
"""
import contextlib
import csv
import io
import math
import unittest
from datetime import timedelta

from adapters import base
from core import labels, verify
from core.book import TASK_FIELDS, ts
from tests.test_adapter_base import FRESH, NOW, OLDER, FakeClient, fresh_pairs, gt_pool, load
from tests.test_labels import Batch, Root, pair
from tests.test_replay import D1, D2, V17, Data, a, keys, label, task
from tests.test_tick import T0, addr
from tools import replay

SIGHT_PAIR = "0x" + "5a" * 32  # a v4 pool id
FLOOR = labels.DEAD["floor_usd"]


def dust(i, quote_eth, liq=4, pool=None):
    """A label pair whose WETH side holds quote_eth (WETH at 2,000 USD here: price 2.0 / native 0.001)."""
    p = pair(addr(i), liq=liq, dex="uniswap", labels_=("v2",))
    p["liquidity"].update(base=1.0, quote=quote_eth)
    if pool:
        p["pairAddress"] = pool
    return p


DEEP, OTHER = "0x" + "de" * 20, "0x" + "0b" * 20


class DeadRuleTest(unittest.TestCase):
    def test_numbers_live_in_one_place(self):
        self.assertEqual(labels.DEAD, {"floor_usd": 100, "share": 0.10})

    def test_gone_is_dead_whatever_else(self):
        for q, sq in (("na", "na"), (5000, "na"), (5000, 1000)):
            self.assertEqual(labels.dead(1, q, sq), 1)
            self.assertEqual(labels.dead("1", q, sq), 1)  # as read back from a csv

    def test_under_the_floor_is_dead_without_the_sight(self):
        self.assertEqual(labels.dead(0, 99, "na"), 1)
        self.assertEqual(labels.dead(0, 2, 34_000), 1)

    def test_qliq_zero_is_a_real_value(self):
        self.assertEqual(labels.dead(0, 0, "na"), 1)  # BL-28: a near-empty real side, not missing
        self.assertEqual(labels.dead("0", "0", "na"), 1)

    def test_unknown_inputs_give_na(self):
        self.assertEqual(labels.dead(0, "na", 5000), "na")      # label qliq unknown
        self.assertEqual(labels.dead(0, None, 5000), "na")
        self.assertEqual(labels.dead(0, "", "na"), "na")
        self.assertEqual(labels.dead(0, FLOOR, "na"), "na")     # at the floor: depends on the sight qliq
        self.assertEqual(labels.dead(0, 5000, "na"), "na")
        self.assertEqual(labels.dead(0, 5000, None), "na")
        self.assertEqual(labels.dead(0, True, 5000), "na")      # odd shapes are unknown, never a guess

    def test_exact_edges(self):
        # the share: sight 2,000 -> line max(100, 200) = 200; strictly below is dead, the edge is alive
        self.assertEqual(labels.dead(0, 199, 2000), 1)
        self.assertEqual(labels.dead(0, 200, 2000), 0)
        self.assertEqual(labels.dead(0, 199.99, 2000), 1)
        self.assertEqual(labels.dead("0", "200", "2000"), 0)    # csv strings
        # the floor: sight 500 -> max(100, 50) = 100
        self.assertEqual(labels.dead(0, 99, 500), 1)
        self.assertEqual(labels.dead(0, 100, 500), 0)
        # sight 0 (a real empty side at sight): only the floor counts
        self.assertEqual(labels.dead(0, 100, 0), 0)


class SightTest(unittest.TestCase):
    def facts(self, qliq=34_000):
        return {"symbol": "D", "price": 1.0, "pair": SIGHT_PAIR, "trade": {"qliq": qliq}}

    def test_first_sight_keeps_pair_and_qliq(self):
        s = labels.sight(addr(1), self.facts(), ts(T0), T0)
        self.assertEqual((s["sight_pair"], s["sight_qliq"]), (SIGHT_PAIR, 34_000))
        self.assertEqual(labels.sight(addr(1), self.facts(qliq=0), ts(T0), T0)["sight_qliq"], 0)  # a real 0

    def test_a_later_recheck_never_gives_them(self):
        s = labels.sight(addr(1), self.facts(), ts(T0), T0 + timedelta(minutes=15))
        self.assertEqual((s["sight_pair"], s["sight_qliq"], s["first_price"]), ("na", "na", "na"))

    def test_missing_is_na(self):
        s = labels.sight(addr(1), {"symbol": "D", "price": 1.0}, ts(T0), T0)
        self.assertEqual((s["sight_pair"], s["sight_qliq"]), ("na", "na"))
        s = labels.sight(addr(1), dict(self.facts(qliq="na"), pair=""), ts(T0), T0)
        self.assertEqual((s["sight_pair"], s["sight_qliq"]), ("na", "na"))


class LabelRowTest(Root):
    def one(self, pairs, qliq=34_000, minutes=60):
        f = {"symbol": "D", "price": 1.0, "pair": SIGHT_PAIR, "trade": {"qliq": qliq}}
        self.run_labels(0, [labels.sight(addr(1), f, ts(T0), T0)])
        self.run_labels(minutes, batch=Batch({addr(1): pairs}))
        return self.rows()[0]

    def test_dust_pair_looks_alive_but_is_dead(self):
        r = self.one([dust(1, 0.001)])  # 0.001 WETH = 2 USD left; the price still answers
        self.assertEqual((r["gone"], r["price"], r["qliq"], r["dex"]), ("0", "2.0", "2", "uniswap:v2"))
        self.assertEqual(r["dead"], "1")
        self.assertEqual(r["sight_pair"], SIGHT_PAIR)
        self.assertEqual(r["label_pair"], pair(addr(1))["pairAddress"])
        self.assertEqual(r["sight_qliq"], "34000")

    def test_share_edge_through_the_row(self):
        self.assertEqual(self.one([dust(1, 1.7)])["dead"], "0")  # 3,400 USD = 10% of 34,000: alive at the edge
        self.tearDown(); self.setUp()
        self.assertEqual(self.one([dust(1, 1.6995)])["dead"], "1")  # 3,399 USD

    def test_a_real_pair_behind_a_token_heavy_deepest_pair_is_alive(self):
        # (a) the deepest pair by total liquidity holds almost only our token (qliq 2); another pair holds real depth
        token_heavy = dust(1, 0.001, liq=900_000, pool=DEEP)
        for sight_q, other_eth, want in ((500, 0.05, "0"),       # 100 USD exactly, line max(100, 50) = 100: alive
                                         (34_000, 2.0, "0"),     # 4,000 USD >= 10% of 34,000
                                         (34_000, 1.0, "1")):    # 2,000 USD < 3,400: dead on the share
            with self.subTest(sight=sight_q, other=other_eth):
                self.tearDown(); self.setUp()
                r = self.one([token_heavy, dust(1, other_eth, liq=5_000, pool=OTHER)], qliq=sight_q)
                self.assertEqual((r["qliq"], r["label_pair"], r["liq"]), ("2", DEEP, "900000"))  # BL-28 meaning kept
                self.assertEqual((r["best_qliq"], r["dead"]), (str(round(other_eth * 2000)), want))

    def test_every_pair_under_the_floor_is_dead(self):
        # (b) the deepest pair and every other pair under 100 USD of real depth
        r = self.one([dust(1, 0.001, liq=900_000, pool=DEEP), dust(1, 0.03, liq=500, pool=OTHER),
                      dust(1, 0.0495, liq=300, pool="0x" + "0c" * 20)], qliq="na")
        self.assertEqual((r["qliq"], r["best_qliq"], r["dead"]), ("2", "99", "1"))

    def test_best_qliq_reads_base_pairs_only_and_na_when_none_known(self):
        other_chain = dict(dust(1, 5.0, liq=10, pool=OTHER), chainId="ethereum")
        self.assertEqual(labels.best_qliq([dust(1, 0.001), other_chain], addr(1)), 2)
        self.assertEqual(labels.best_qliq([pair(addr(1))], addr(1)), "na")  # no side amounts
        self.assertEqual(labels.best_qliq([pair(addr(1)), dust(1, 0.0001)], addr(1)), 0)  # a real 0 counts
        self.assertEqual(labels.best_qliq("odd", addr(1)), "na")

    def test_label_qliq_zero_is_dead(self):
        p = dust(1, 0.0001)  # 0.20 USD rounds to a real 0 (BL-28)
        r = self.one([p], qliq="na")
        self.assertEqual((r["qliq"], r["dead"]), ("0", "1"))

    def test_gone_row(self):
        r = self.one([])
        self.assertEqual((r["gone"], r["dead"], r["label_pair"], r["qliq"], r["best_qliq"]), ("1", "1", "na", "na", "na"))
        self.assertEqual((r["sight_pair"], r["sight_qliq"]), (SIGHT_PAIR, "34000"))

    def test_unknown_label_qliq_is_na(self):
        r = self.one([pair(addr(1))])  # no side amounts: qliq "na"
        self.assertEqual((r["gone"], r["qliq"], r["dead"]), ("0", "na", "na"))

    def test_unknown_sight_qliq_above_the_floor_is_na(self):
        r = self.one([dust(1, 1.0)], qliq="na")  # 2,000 USD now, nothing at sight
        self.assertEqual((r["qliq"], r["sight_qliq"], r["dead"]), ("2000", "na", "na"))

    def test_entry_pending_before_bl30_has_no_sight_values(self):
        s = labels.load(self.root)
        s["pending"][addr(1)] = {"symbol": "D", "first_seen": ts(T0), "first_price": 1.0, "done": []}  # old shape
        labels.save(self.root, s)
        self.run_labels(60, batch=Batch({addr(1): [dust(1, 1.0)]}))
        r = self.rows()[0]
        self.assertEqual((r["sight_pair"], r["sight_qliq"], r["dead"]), ("na", "na", "na"))

    def test_todays_17_column_file_keeps_its_columns(self):
        path = self.root / "data/daily/2026-10-23/outcomes.csv"
        path.parent.mkdir(parents=True)
        path.write_text(",".join(labels.FIELDS[:17]) + "\n")  # started before BL-30 today
        self.one([dust(1, 0.001)])
        with path.open() as fh:
            rows = list(csv.reader(fh))
        self.assertEqual({len(r) for r in rows}, {17})
        self.assertEqual(rows[0][-1], "qliq")
        self.assertEqual(labels.FIELDS[17:], ["sight_pair", "label_pair", "sight_qliq", "best_qliq", "dead"])
        self.assertEqual(len(labels.FIELDS), 22)


class AgeTest(unittest.TestCase):
    def test_keys_on_the_rows(self):
        self.assertEqual(verify.TRADE_KEYS[-2:], ["pair_age", "token_age"])

    def test_pair_age_is_the_age_rule_input_and_token_age_the_oldest_pair(self):
        pairs = fresh_pairs()
        old = dict(pairs[0], pairAddress="0x" + "77" * 20, pairCreatedAt=pairs[0]["pairCreatedAt"] - 90 * 60_000)
        old["liquidity"] = {"usd": 10}  # thinner: not the chosen pair
        c = FakeClient({"token-pairs": pairs + [old], "gopluslabs": load("goplus_fresh")})
        f = base.facts(FRESH, now=NOW, client=c)
        self.assertEqual(f["trade"]["pair_age"], math.floor(f["age_min"]))
        self.assertEqual(f["trade"]["token_age"], math.floor(f["age_min"] + 90))
        self.assertEqual(len(c.calls), 2)  # DexScreener + GoPlus: no request added

    def test_geckoterminal_age_counts_for_both(self):
        c = FakeClient({"token-pairs": load("dexscreener_older"),
                        "networks/base/pools/0x4e829f8a": gt_pool("2026-10-23T10:30:00Z")})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual((f["age_min"], f["trade"]["pair_age"], f["trade"]["token_age"]), (60, 60, 60))

    def test_missing_is_na_and_never_raises(self):
        c = FakeClient({"token-pairs": load("dexscreener_older"), "gopluslabs": load("goplus_older"),
                        "networks/base/pools/": None})
        f = base.facts(OLDER, NOW, c)
        self.assertEqual((f["trade"]["pair_age"], f["trade"]["token_age"]), ("na", "na"))
        self.assertEqual(base.ages("garbage", FRESH, NOW, None), {"pair_age": "na", "token_age": "na"})
        self.assertEqual(base.ages([], FRESH, NOW, float("inf")), {"pair_age": "na", "token_age": "na"})
        self.assertEqual(base.ages([], FRESH, NOW, 14.9), {"pair_age": 14, "token_age": 14})  # whole minutes, floored


class ReplayDeadTest(Data):
    """Replay on an old 17-column file: dead derived from gone + label qliq + the sight qliq at first_seen."""
    T = "2026-11-01T01:00:00Z"

    def setUp(self):
        super().setUp()
        t, later = self.T, "2026-11-01T01:15:00Z"
        self.write(D1, "tasks", TASK_FIELDS, [
            task(t, "VERIFY", "WATCH", a(1), "goplus_not_ready", f"first_seen={t} checks=1 " + keys("5000")),
            task(later, "VERIFY", "WATCH", a(1), "goplus_not_ready", f"first_seen={t} checks=2 " + keys("1000")),
            task(t, "VERIFY", "SKIP", a(2), "mint_open", "until=x " + keys("5000")),
            task(t, "VERIFY", "SKIP", a(3), "mint_open", "until=x " + keys("5000")),
            task(t, "VERIFY", "SKIP", a(4), "mint_open", "until=x " + keys("5000")),
            task(t, "VERIFY", "SKIP", a(5), "mint_open", "until=x " + keys("5000")),
            task(t, "VERIFY", "SKIP", a(6), "mint_open", "until=x " + keys("5000")),
        ])
        self.write(D1, "outcomes", V17, [
            label(a(1), t, 1, "60", qliq="400"),               # join: 400 < 10% of 5,000 (the re-check's 1,000 would say alive)
            label(a(2), t, 1, "60", qliq="2"),                 # dust: dead without the join
            label(a(3), t, 1, "60", gone="1", qliq="na"),      # gone
            label(a(4), t, 1, "60", qliq="na"),                # unknown label qliq
            label(a(5), t, 1, "60", qliq="0"),                 # a real 0
            label(a(6), t, 1, "60", qliq="500"),               # exactly 10% of 5,000: alive
        ])
        self.win, self.tasks, _, self.outcomes = replay.load(self.root, D1, D1)
        self.sq = replay.sight_qliq_index(self.tasks)

    def dead(self, i):
        return replay.label_dead(next(r for r in self.outcomes if r["address"] == a(i)), self.sq)

    def test_derived_on_the_old_file(self):
        self.assertNotIn("dead", self.outcomes[0])
        self.assertEqual([self.dead(i) for i in range(1, 7)],
                         [("1", "joined"), ("1", "no_join"), ("1", "no_join"), ("na", "qliq_na"), ("1", "no_join"),
                          ("0", "joined")])

    def test_join_is_the_row_at_first_seen_only(self):
        self.assertEqual(self.sq[(a(1), self.T)], "5000")
        self.assertEqual(self.sq[(a(1), "2026-11-01T01:15:00Z")], "1000")  # the re-check, under its own ts only
        sq ={k: v for k, v in self.sq.items() if k != (a(1), self.T)}  # no row at first_seen: stays "na"
        r = next(r for r in self.outcomes if r["address"] == a(1))
        self.assertEqual(replay.label_dead(r, sq), ("na", "join_na"))

    def test_depth_at_sight_split(self):
        r = next(r for r in self.outcomes if r["address"] == a(1))
        self.assertEqual(replay.sight_depth(r, self.sq), ">=100")                        # join: 5,000 at first_seen
        self.assertEqual(replay.sight_depth(dict(r, sight_qliq="99"), self.sq), "<100")  # logged: born thin
        self.assertEqual(replay.sight_depth(dict(r, sight_qliq="100"), self.sq), ">=100")  # the floor edge
        self.assertEqual(replay.sight_depth(dict(r, sight_qliq="0"), {}), "<100")        # a real 0
        self.assertEqual(replay.sight_depth(dict(r, sight_qliq="na"), self.sq), ">=100")  # logged "na": the join
        self.assertEqual(replay.sight_depth(r, {}), "na")
        idx = {(x["address"], x["first_seen"], x["horizon"]): dict(x, _dead=d, _how=h)
               for x in self.outcomes for d, h in [replay.label_dead(x, self.sq)]}
        thin = dict(label(a(7), self.T, 1, "60", qliq="2"), sight_qliq="40", dead="1", _dead="1", _how="logged")
        idx[(a(7), self.T, "1h")] = thin
        sights = {(x[0], x[1]): {} for x in idx}
        t, how = replay.death_table(sights, idx, self.sq)
        self.assertEqual({g: c["n"] for g, c in t["1h"].items()}, {"all": 7, "<100": 1, ">=100": 6, "na": 0})
        self.assertEqual((t["1h"]["<100"]["dead"], how["logged"]), (1, 1))

    def test_logged_dead_is_used_as_is(self):
        r = dict(label(a(1), self.T, 1, "60", qliq="400"), dead="0", sight_qliq="na")
        self.assertEqual(replay.label_dead(r, self.sq), ("0", "logged"))
        self.assertEqual(replay.label_dead(dict(r, dead=""), self.sq), ("na", "logged"))

    def test_sixteen_columns_have_no_qliq(self):
        self.assertEqual(replay.label_dead(label(a(1), self.T, 1, "60"), self.sq), ("na", "qliq_na"))
        self.assertEqual(replay.label_dead(label(a(1), self.T, 1, "60", gone="1"), self.sq), ("1", "no_join"))

    def test_eve_of_the_window_is_joined(self):
        self.write(D2, "outcomes", V17, [label(a(1), self.T, 6, "360", qliq="400")])
        _, tasks, _, outcomes = replay.load(self.root, D2, D2)  # window starts the day after the first sight
        sq = replay.sight_qliq_index(replay.join_tasks(self.root, [D2]) + tasks)
        self.assertEqual(replay.label_dead(outcomes[0], sq), ("1", "joined"))

    def test_table_and_counts_in_the_output(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            replay.main(["--root", str(self.root), "--from", D1, "--to", D1])
        text = out.getvalue()
        self.assertIn("Table 0: death rate per horizon, gone only vs dead", text)
        rows = {tuple(x.split()[:2]): x.split()[2:] for x in text.splitlines() if x.strip().startswith("1h ")}
        self.assertEqual(rows[("1h", "all")], ["6", "1", "16.7", "4", "1", "80.0", "66.7"])
        self.assertEqual(rows[("1h", ">=100")], ["6", "1", "16.7", "4", "1", "80.0", "66.7"])  # every sight was 5,000
        self.assertEqual(rows[("1h", "<100")][:3], ["0", "0", "na"])
        self.assertIn("APPROXIMATION on old rows", text)
        self.assertIn("old rows (no dead column, derived): 6 = decided without the join 3", text)
        self.assertIn("label qliq na 1 + needed the join 2", text)
        self.assertIn("sight qliq found 2, stayed na 0", text)


if __name__ == "__main__":
    unittest.main()
