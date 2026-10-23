"""Baseline settings. Pete owns this file: thresholds change only when Pete asks."""

# Switches
PAPER = True              # paper only, always (CLAUDE.md non-negotiable 1)
PAUSED = False            # True = no new entries and no scans

# Book and sizing
START_BALANCE = 50.0      # paper USD
SIZE_PCT = 0.10           # ticket = cash * SIZE_PCT
MIN_TICKET = 5            # USD
MAX_TICKET = 250          # USD
MAX_OPEN = 4              # open positions per book
SESSION_LOSS_HALT_PCT = 30  # vs total value at the start of the UTC day

# Hard skips (VERIFY). A missing number is a skip.
SKIP_IF = {
    "min_liquidity_usd": 50000,
    "min_lp_locked_pct": 90,
    "max_dev_pct": 10,
    "max_top10_pct": 35,
    "min_age_min": 15,
    "max_age_h": 48,
    "max_ticket_share": 0.01,   # ticket <= liq_usd * this
    "max_buy_tax_pct": 10,
    "max_sell_tax_pct": 10,
}

# Watchlist for leads GoPlus has not indexed yet (DECISIONS 2026-10-23, BL-6 3a)
WATCH = {"recheck_min": 15, "max_h": 3}

# GoPlus calls per tick; the free API failed from the 11th call on (DECISIONS 2026-10-23, BL-7)
GOPLUS_MAX_PER_TICK = 9

# Exits
EXIT = {
    "hard_stop_pct": -25,
    "ladder": [(40, 0.33), (100, 0.33)],  # (gain %, share of position to sell)
    "trail_pct": 25,
    "time_stop_min": 45,
    "time_stop_min_pct": 10,
    "liq_drop_pct": 40,
}

# Friction per side (CLAUDE.md section 6)
FRICTION = {
    "base_pct": 3.0,
    "thin_pct": 5.0,              # used when liq_usd < thin_liq_usd
    "thin_liq_usd": 100000,
    "pool_fee_pct": {"clanker": 1.0},  # launchpad pool fee, added per side
}

# Grok X-read (can only SKIP)
GROK_ENABLED = False
GROK_DAILY_CAP = 25
MODEL = "grok-4.3"        # placeholder, verify at BL-9
MIN_X_CONFIDENCE = 0.7

# Files (relative to the repo root)
STATE = "data/state.json"
TASKS = "data/daily/{day}/tasks.csv"
TRADES = "data/daily/{day}/trades.csv"
