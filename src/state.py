"""Layer 2: state.

Principle (§5): Alpaca is the source of truth; this file is a cache that must
converge to it. Positions, orders and equity are re-read from the broker every
cycle and never trusted from disk.

What the broker cannot tell us, and therefore lives here:
  - first_qualified   : the cycle a ticker first became runnable  -> `age`
  - capital_days_used : collateral-days already spent per ticker  -> `ubt`
  - day_high_water    : intraday equity peak -> the circuit breaker's reference
  - cycle             : monotonic counter, survives restarts

Writes are atomic (temp file + replace) so a kill mid-write cannot leave a
truncated state file behind -- dry-run step 6 exercises exactly that.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import config


@dataclass
class State:
    cycle: int = 0
    session_date: str = ""
    day_high_water: float = 0.0

    # --- allocation bookkeeping (§6) ---
    first_qualified: dict[str, int] = field(default_factory=dict)
    capital_days_used: dict[str, float] = field(default_factory=dict)

    # --- observed IV per ticker, so the signal layer can move from a
    # cross-sectional rank to a real time-series percentile as the week runs.
    iv_history: dict[str, list[float]] = field(default_factory=dict)

    # --- circuit breaker latch (gate 5) ---
    breaker_tripped: bool = False
    breaker_reason: str = ""

    # --- runaway guard: order submissions this session ---
    orders_today: int = 0

    # --- equity snapshot, for logging/metrics only ---
    equity: float = 0.0
    last_cycle_at: str = ""

    # ---------------------------------------------------------------- io ----

    @classmethod
    def load(cls, path: str | None = None) -> "State":
        p = Path(path or config.STATE_FILE)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt state is recoverable: everything load-bearing is re-read
            # from Alpaca. Losing age/ubt costs allocation memory, not safety.
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: str | None = None) -> None:
        p = Path(path or config.STATE_FILE)
        # Keep the last good copy. `load()` recovers from corruption by
        # returning a fresh State, which is safe but silently forgets age/ubt
        # -- the allocation layer's entire memory. A one-file backup makes that
        # recoverable by hand instead of gone.
        if p.exists():
            try:
                p.replace(p.with_suffix(p.suffix + ".bak"))
            except OSError:
                pass
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, p)  # atomic on Windows and POSIX

    # ------------------------------------------------------------ session ---

    def begin_cycle(self, now: datetime, equity: float) -> None:
        today = now.date().isoformat()
        if today != self.session_date:
            # New session: the breaker's reference resets, allocation memory
            # deliberately does not -- `age` accrues across the whole window.
            self.session_date = today
            self.day_high_water = equity
            self.breaker_tripped = False
            self.breaker_reason = ""
            self.orders_today = 0

        self.cycle += 1
        self.equity = equity
        self.day_high_water = max(self.day_high_water, equity)
        self.last_cycle_at = now.isoformat()

    # -------------------------------------------------------- allocation ------

    def mark_qualified(self, ticker: str) -> None:
        """First cycle a ticker became runnable. Never overwritten -- that is
        what makes `age` accumulate for passed-over candidates."""
        self.first_qualified.setdefault(ticker, self.cycle)

    def age(self, ticker: str) -> int:
        first = self.first_qualified.get(ticker)
        return 0 if first is None else self.cycle - first

    def ubt(self, ticker: str) -> float:
        return self.capital_days_used.get(ticker, 0.0)

    def charge_capital(self, ticker: str, collateral: float, equity: float,
                       dte: int) -> None:
        """Book collateral-days against a ticker when it wins capital. Drives
        `ubt`, which is what gives held tickers a lower score and produces
        diversification without a hardcoded rule."""
        if equity <= 0:
            return
        self.capital_days_used[ticker] = self.ubt(ticker) + (collateral / equity) * dte

        # Winning resets the clock, which is what makes `age` mean anything.
        #
        # first_qualified was set once and never moved, so every candidate that
        # became runnable on the same cycle carried an IDENTICAL age forever --
        # the term cancelled out of every comparison and contributed nothing.
        # Live cycles showed exactly that, every runnable name sitting at the
        # same number. All the anti-starvation was coming from ubt alone.
        #
        # Reset on funding and `age` becomes "cycles since this ticker last
        # received capital", which is the classic aging term and the thing the
        # index was documented as having all along.
        self.first_qualified[ticker] = self.cycle

    # --------------------------------------------------------- breaker ------

    def drawdown(self, equity: float) -> float:
        if self.day_high_water <= 0:
            return 0.0
        return (self.day_high_water - equity) / self.day_high_water

    def trip_breaker(self, reason: str) -> None:
        self.breaker_tripped = True
        self.breaker_reason = reason
