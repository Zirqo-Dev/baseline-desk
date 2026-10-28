"""Lead memory: final 24h skips and the GoPlus watchlist, kept under "leads" in data/state.json.

A skip is final for 24h (non-negotiable 3). One exception (DECISIONS 2026-10-23, BL-6 3a):
"goplus_not_ready" puts the lead on a watchlist, re-checked every WATCH["recheck_min"],
until WATCH["max_h"] after first seen; then a final "goplus_timeout".
"fetch_error" (429, 5xx, timeout) is not locked either: retried next run, at most
FETCH_TRIES in a row, then a final "fetch_error_x3". A GoPlus answer code other than 1 is logged
as goplus_code=<n> in the row detail. A check settled without GoPlus (BL-20 DexScreener-first gate)
adds goplus=skipped to its row. A lead that got past the missing-data gate with no LP-lock number adds
lp_unknown (BL-21); a watched lead waiting only on lp_locked_pct leaves the watchlist with its real outcome.
Every check logs one VERIFY task row. Wiring into the tick is BL-7.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import config
from core.book import ROOT, TASK_FIELDS, _append, day, load_state, save_key, ts
from core.verify import LP, hard_skip, lp_unknown

SKIP_H = 24
FETCH_TRIES = 3
BOOK = "desk"  # VERIFY runs before the books split; rows go under the desk


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Leads:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.state_path = self.root / config.STATE
        self.s = load_state(self.state_path).get("leads") or {"skips": {}, "watch": {}}
        self.s.setdefault("retry", {})

    def save(self):
        save_key(self.state_path, "leads", self.s)

    # ---- queries ----
    def skipped(self, address, now):
        """Reason of an active final skip, or ""."""
        k = self.s["skips"].get(address.lower())
        return k["reason"] if k and _t(k["until"]) > now else ""

    def due(self, address, now):
        """True if the lead should be checked now: not final-skipped, and a watched lead's recheck time has come."""
        if self.skipped(address, now):
            return False
        w = self.s["watch"].get(address.lower())
        return w is None or now >= _t(w["last_check"]) + timedelta(minutes=config.WATCH["recheck_min"])

    # ---- one check ----
    def record(self, address, facts, now):
        """Apply hard_skip() to fresh facts, update skips/watch, log one VERIFY row. Returns the reason ("" = PASS)."""
        address = address.lower()
        self.s["skips"] = {a: k for a, k in self.s["skips"].items() if _t(k["until"]) > now}
        reason = hard_skip(facts)
        sym = facts.get("symbol") or ""
        if reason == "fetch_error":
            return self._fetch_error(address, sym, facts, now)
        self.s["retry"].pop(address, None)
        w = self.s["watch"].pop(address, None)
        detail = []
        if w:
            detail += [f"first_seen={w['first_seen']}", f"checks={w['checks'] + 1}"]

        if reason == "goplus_not_ready":
            w = w or {"first_seen": ts(now), "checks": 0}
            w.update(symbol=sym, last_check=ts(now), checks=w["checks"] + 1)
            if now >= _t(w["first_seen"]) + timedelta(hours=config.WATCH["max_h"]):
                reason = "goplus_timeout"
            else:
                self.s["watch"][address] = w
                if not detail:
                    detail = [f"first_seen={w['first_seen']}", "checks=1"]
                detail.append("pending=" + ",".join(facts.get("goplus_pending") or []))
                self._row(now, sym, address, "WATCH", reason, detail)
                return reason

        pending = [k for k in facts.get("goplus_pending") or [] if k != LP]  # BL-21: LP lock may stay unknown
        if w and not pending and not facts.get("goplus_skipped"):
            detail.append(f"goplus_ready_at={ts(now)}")
        if facts.get("goplus_skipped"):
            detail.append("goplus=skipped")
        if lp_unknown(facts):
            detail.append("lp_unknown")
        if reason:
            until = now + timedelta(hours=SKIP_H)
            self.s["skips"][address] = {"symbol": sym, "reason": reason, "at": ts(now), "until": ts(until)}
            self._row(now, sym, address, "SKIP", reason, detail + [f"until={ts(until)}"])
        else:
            self._row(now, sym, address, "PASS", "", detail)
        return reason

    def _fetch_error(self, address, sym, facts, now):
        """A failed request says nothing about the token: retry next run, final only after FETCH_TRIES in a row."""
        tries = self.s["retry"].pop(address, {"tries": 0})["tries"] + 1
        detail = [f"try={tries}", "hosts=" + ",".join(facts.get("fetch_errors") or [])]
        if facts.get("goplus_code") is not None:
            detail.append(f"goplus_code={facts['goplus_code']}")
        if facts.get("goplus_skipped"):
            detail.append("goplus=skipped")
        w = self.s["watch"].get(address)
        if w:
            detail.insert(0, f"first_seen={w['first_seen']}")
        if tries < FETCH_TRIES:
            self.s["retry"][address] = {"tries": tries, "last": ts(now)}
            self._row(now, sym, address, "RETRY", "fetch_error", detail)
            return "fetch_error"
        self.s["watch"].pop(address, None)
        until = now + timedelta(hours=SKIP_H)
        self.s["skips"][address] = {"symbol": sym, "reason": "fetch_error_x3", "at": ts(now), "until": ts(until)}
        self._row(now, sym, address, "SKIP", "fetch_error_x3", detail + [f"until={ts(until)}"])
        return "fetch_error_x3"

    def _row(self, now, symbol, address, action, reason, detail):
        _append(self.root / config.TASKS.format(day=day(now)), TASK_FIELDS, {
            "ts": ts(now), "book": BOOK, "stage": "VERIFY", "symbol": symbol, "address": address,
            "action": action, "reason": reason, "detail": " ".join(detail)})
        self.save()
