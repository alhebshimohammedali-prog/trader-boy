"""Demonstrates that the allocation layer is load-bearing, not decorative.

Runs the real `allocation.select()` over a synthetic universe for N cycles and
prints the runnable-candidate table each time. No credentials needed.

The point it proves: NVDA has a permanently higher signal than everything else.
A naive `max(runnable, key=signal)` selects NVDA every single cycle. Under PWT,
NVDA wins early, accumulates `ubt` from the capital it consumed, and the
passed-over names accumulate `age` until they overtake it -- so capital rotates
and nothing starves, with no diversification rule written anywhere.

    python tools/allocation_demo.py
"""

from __future__ import annotations

from datetime import date


from src.allocation import select
from src.data import Contract
from src.state import State

TODAY = date(2026, 8, 31)
EQUITY = 100_000.0
CYCLES = 12

# (ticker, strike, fixed signal) -- NVDA is deliberately the loudest.
UNIVERSE = [
    ("NVDA", 178.0, 0.91),
    ("AAPL", 226.0, 0.62),
    ("XOM", 112.0, 0.58),
    ("IWM", 238.0, 0.55),
]


def candidates():
    out = []
    for ticker, strike, signal in UNIVERSE:
        c = Contract(
            symbol=f"{ticker}260904P{int(strike * 1000):08d}",
            underlying=ticker,
            strike=strike,
            expiry="2026-09-04",
            bid=1.10,
            ask=1.18,
            open_interest=2500,
            delta=-0.22,
        )
        out.append((c, signal))
    return out


def main() -> int:
    state = State()
    naive_picks: list[str] = []
    pwt_picks: list[str] = []

    for _ in range(CYCLES):
        state.begin_cycle(__import__("datetime").datetime.now(), EQUITY)
        runnable = candidates()
        for c, _s in runnable:
            state.mark_qualified(c.underlying)

        naive_picks.append(max(runnable, key=lambda x: x[1])[0].underlying)

        winner, scored = select(runnable, state, EQUITY, TODAY)
        pwt_picks.append(winner.contract.underlying)

        print(f"\ncycle {state.cycle:2d}   equity {EQUITY:,.0f}")
        print(f"  {'ticker':7s} {'signal':>7s} {'age':>4s} {'ubt':>7s} {'opbt':>6s} "
              f"{'pwt':>8s}   sel")
        for s in scored:
            print(f"  {s.contract.underlying:7s} {s.signal:7.2f} {s.age:4d} "
                  f"{s.ubt:7.3f} {s.opbt:6.3f} {s.pwt:8.3f}   "
                  f"{'<-- WINS' if s.selected else ''}")

        # Winner consumes capital -> its ubt rises -> it scores lower next time.
        state.charge_capital(
            winner.contract.underlying, winner.contract.collateral, EQUITY, 4
        )

    print("\n" + "=" * 64)
    print("naive max(signal):", " ".join(f"{t:5s}" for t in naive_picks))
    print("PWT allocation   :", " ".join(f"{t:5s}" for t in pwt_picks))
    print("=" * 64)
    print(f"naive touched {len(set(naive_picks))}/{len(UNIVERSE)} tickers; "
          f"PWT touched {len(set(pwt_picks))}/{len(UNIVERSE)}.")
    starved = set(t for t, _, _ in UNIVERSE) - set(pwt_picks)
    print("starved under PWT:", ", ".join(sorted(starved)) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
