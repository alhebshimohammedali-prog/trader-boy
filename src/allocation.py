"""Layer 5: allocation -- the novelty.

Capital allocation under contention is a scheduling problem: a finite resource,
jobs of known duration, arriving asynchronously. We score every runnable
candidate on one scalar and take the highest.

Three properties fall out, none of them separately coded:

  - a passed-over candidate gains priority every cycle, so nothing starves and
    the loudest signal is not re-selected forever;
  - a candidate demanding more capital for longer scores lower, so capital
    efficiency is intrinsic rather than bolted on;
  - a ticker already holding capital scores lower, which is diversification
    without a concentration rule written anywhere.

Sizing is computed before scoring, because opbt needs collateral. The other way
round gives cycle one a circular dependency.

The scoring function is adapted from the authors' own earlier, unpublished
research on resource allocation under contention. Its derivation is
deliberately not reproduced here.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.data import Contract
from src.state import State


@dataclass
class Scored:
    contract: Contract
    signal: float
    age: int
    ubt: float
    opbt: float
    pwt: float
    selected: bool = False

    def row(self) -> dict:
        """One line of the runnable-candidate table (§8). This table is the
        evidence the allocation layer is load-bearing rather than decorative --
        without it the novelty claim is unsupported."""
        return {
            "ticker": self.contract.underlying,
            "symbol": self.contract.symbol,
            "strike": self.contract.strike,
            "collateral": round(self.contract.collateral, 2),
            "signal": round(self.signal, 4),
            "age": self.age,
            "ubt": round(self.ubt, 4),
            "opbt": round(self.opbt, 4),
            "pwt": round(self.pwt, 4),
            "selected": self.selected,
        }


def dte(contract: Contract, today) -> int:
    from datetime import date

    y, m, d = (int(x) for x in contract.expiry.split("-"))
    return max((date(y, m, d) - today).days, 0)


def capital_time(contract: Contract, equity: float, days_to_expiry: int) -> float:
    """This candidate's own resource demand: fraction of equity tied up, times
    how long it stays tied up. This candidate's claim on the shared resource."""
    return (contract.collateral / equity) * days_to_expiry if equity > 0 else 0.0


def pwt(age: int, ubt: float, opbt: float) -> float:
    """pwt = age - ubt + opbt.

    age   cycles since this ticker first became runnable and was passed over
    ubt   collateral-days this ticker has already consumed
    opbt  committed resource-time of every OTHER queued candidate

    opbt is the subtle term and it must exclude the candidate itself. Include
    its own size and the sign of the mechanism inverts: the allocator starts
    preferring the largest, longest-dated position instead of the cheapest.
    """
    return age - ubt + opbt


def select(
    runnable: list[tuple[Contract, float]],
    state: State,
    equity: float,
    today,
) -> tuple[Scored | None, list[Scored]]:
    """Score every runnable candidate, pick one.

    Ties break on (-spread, ticker) so the choice is deterministic and a
    replayed log reproduces the same selection.
    """
    if not runnable:
        return None, []

    # Sizing is computed before scoring: OPBT needs collateral, so doing it the
    # other way round gives cycle one a circular dependency.
    entries = [
        (contract, signal, dte(contract, today)) for contract, signal in runnable
    ]
    own = {
        id(c): capital_time(c, equity, d) for c, _s, d in entries
    }
    queued_total = sum(own.values())

    scored: list[Scored] = []
    for contract, signal, d in entries:
        ticker = contract.underlying
        age = state.age(ticker)
        ubt = state.ubt(ticker)
        # OTHER candidates' resource-time -- excludes this one by design.
        opbt = queued_total - own[id(contract)]
        scored.append(Scored(contract, signal, age, ubt, opbt, pwt(age, ubt, opbt)))

    winner = max(
        scored,
        key=lambda s: (s.pwt, -(s.contract.spread_abs or 9.99), s.contract.underlying),
    )
    winner.selected = True
    scored.sort(key=lambda s: s.pwt, reverse=True)
    return winner, scored
