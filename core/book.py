"""Paper book: cash, open positions and the daily loss halt, kept in data/state.json.

One state file holds every book by name ("desk", later "baseline"), so the
random baseline runs on the same code as the desk.
"""
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from core import config

ROOT = Path(__file__).resolve().parents[1]

TASK_FIELDS = ["ts", "book", "stage", "symbol", "address", "action", "reason", "detail"]
TRADE_FIELDS = ["ts", "book", "side", "symbol", "address", "qty", "usd",
                "intended_price", "fill_price", "friction_pct", "liq_usd",
                "launchpad", "reason", "pnl_usd"]


def utcnow():
    return datetime.now(timezone.utc)


def ts(now):
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(now):
    return now.strftime("%Y-%m-%d")


def _append(path, fields, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


class Book:
    def __init__(self, name="desk", root=ROOT):
        self.name = name
        self.root = Path(root)
        self.state_path = self.root / config.STATE
        everything = self._load_all()
        self.s = everything.get(name) or {
            "cash": float(config.START_BALANCE),
            "positions": {},
            "day": "",
            "day_start_value": float(config.START_BALANCE),
            "halted_day": "",
        }

    # ---- state file ----
    def _load_all(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {}

    def save(self):
        everything = self._load_all()
        everything[self.name] = self.s
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(everything, indent=2, sort_keys=True))
        os.replace(tmp, self.state_path)

    # ---- values ----
    @property
    def cash(self):
        return self.s["cash"]

    @property
    def positions(self):
        return self.s["positions"]

    def value(self):
        """Cash + open positions at last price."""
        return self.cash + sum(p["qty"] * p["last_price"] for p in self.positions.values())

    def mark(self, address, price):
        p = self.positions[address]
        p["last_price"] = price
        p["peak_price"] = max(p["peak_price"], price)

    # ---- daily loss halt ----
    def roll_day(self, now):
        """At the first call of a new UTC day, store day_start_value and clear the halt."""
        today = day(now)
        if self.s["day"] != today:
            self.s["day"] = today
            self.s["day_start_value"] = self.value()
            self.s["halted_day"] = ""

    def halted(self, now):
        """True once value falls SESSION_LOSS_HALT_PCT below day_start_value; stays true all day."""
        self.roll_day(now)
        today = day(now)
        if self.s["halted_day"] == today:
            return True
        floor = self.s["day_start_value"] * (1 - config.SESSION_LOSS_HALT_PCT / 100)
        if self.value() <= floor:
            self.s["halted_day"] = today
            self.task(now, "EXECUTE", "", "", "HALT", "loss_halt",
                      f"value={self.value():.2f} day_start={self.s['day_start_value']:.2f}")
            return True
        return False

    # ---- logs ----
    def task(self, now, stage, symbol, address, action, reason="", detail=""):
        _append(self.root / config.TASKS.format(day=day(now)), TASK_FIELDS, {
            "ts": ts(now), "book": self.name, "stage": stage, "symbol": symbol,
            "address": address, "action": action, "reason": reason, "detail": detail})

    def trade(self, now, row):
        _append(self.root / config.TRADES.format(day=day(now)), TRADE_FIELDS,
                {"ts": ts(now), "book": self.name, **row})
