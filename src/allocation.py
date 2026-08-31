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

import config
from src.data import Contract
from src.signal import rank_within
from src.state import State


@dataclass
class Scored:
    contract: Contract
    signal: float
    age: int
    ubt: float
    opbt: float
    pwt: float
    yield_pcd: float = 0.0
    reward: float = 0.0
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
            "yield_pcd": round(self.yield_pcd, 6),
            "reward": round(self.reward, 4),
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


def expected_yield(contract: Contract, days_to_expiry: int) -> float:
    """Observed payoff rate: premium kept per unit collateral per day.

        (bid / strike) x P(keep) / dte

    Deliberately MECHANICAL, not predictive. Every input is read off the quote
    -- the bid we would actually receive, the strike we would actually post,
    the days we would actually wait. Nothing here forecasts anything, which is
    what separates this from the signal. The signal is a weak opinion about the
    future and is why it gates admission but never ranks; this is arithmetic on
    the contract in front of us.

    The P(keep) factor is load-bearing. Raw premium yield ranks the contract
    closest to the money highest, every time, because that is where the premium
    is. An index that maximised it would systematically select maximum
    assignment risk and call it efficiency. Delta is the risk-neutral
    probability of finishing ITM, so 1 - |delta| prices that back out.

    Delta is missing on a large fraction of this chain. When it is, use the
    band's target rather than solving for it: a delta computed from a guessed
    sigma is a fabricated number wearing the costume of a measured one.
    """
    if contract.bid is None or contract.strike <= 0:
        return 0.0
    import config

    keep = 1.0 - abs(contract.delta) if contract.delta is not None \
        else 1.0 - config.DELTA_TARGET
    keep = max(0.0, min(1.0, keep))
    return (contract.bid / contract.strike) * keep / max(days_to_expiry, 1)


def pwt(age: int, ubt: float, opbt: float, reward: float = 0.0) -> float:
    """pwt = w*age - ubt + opbt + reward.

    age     cycles since this ticker first became runnable and was passed over
    ubt     collateral-days this ticker has already consumed
    opbt    committed resource-time of every OTHER queued candidate
    reward  lambda x the candidate's rank on observed premium yield

    opbt is the subtle term and it must exclude the candidate itself. Include
    its own size and the sign of the mechanism inverts: the allocator starts
    preferring the largest, longest-dated position instead of the cheapest.

    reward defaults to zero, so lambda = 0 reproduces the original three-term
    index exactly. That is not politeness, it is what makes the term testable:
    the ablation can turn it off and the two policies are otherwise identical.

    Two properties make the reward safe to add, and both come from it being
    RANK-based and therefore bounded:

    1. It cannot break anti-starvation. Reward is capped at lambda while ubt
       accrues without limit on whoever keeps winning, so the incumbent's score
       decays past any fixed reward advantage after roughly lambda / capital_time
       consecutive wins. A raw (unbounded) yield would have no such bound, and a
       single mispriced quote could then starve the field for the whole session.

    2. It cannot be hijacked by a bad print. Rank is invariant to magnitude, so
       a quote ten times too large moves a candidate to the top of the ordering
       and no further -- worth exactly lambda, the same as a quote one cent
       better than second place.
    """
    # age is weighted because the terms are not in the same units. age counts
    # CYCLES; ubt and opbt are equity-fraction-days, and a typical win charges
    # about 0.09 of ubt. Unweighted, a single cycle of waiting outranks eleven
    # wins' worth of capital consumed, which is not an index policy, it is
    # round-robin with extra arithmetic.
    return config.AGE_WEIGHT * age - ubt + opbt + reward


def select(
    runnable: list[tuple[Contract, float]],
    state: State,
    equity: float,
    today,
    edge: dict[str, float] | None = None,
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

    # What the reward term measures.
    #
    # `edge` is the variance risk premium per ticker: implied vol minus what
    # the underlying is actually realising. That is the strategy's claimed
    # source of return, and it is the right thing to allocate toward. Premium
    # yield is NOT a substitute: measured live, the highest-yielding contract
    # in the runnable set was routinely the one whose underlying was moving
    # more than its option price assumed -- CIFR paid the most at an implied
    # 1.06 against realised 1.20, and PLTR was realising double its implied.
    # Ranking on yield finds the names where selling volatility is underpriced,
    # which is the opposite of the edge.
    #
    # Yield remains the fallback when vol is unmeasurable, because between two
    # contracts of unknown edge the one paying more per capital-day is still
    # the better use of the capital.
    if edge:
        vals = {id(c): edge.get(c.underlying) for c, _s, _d in entries}
    else:
        vals = {id(c): expected_yield(c, d) for c, _s, d in entries}
    pool = [v for v in vals.values() if v is not None]

    scored: list[Scored] = []
    for contract, signal, d in entries:
        ticker = contract.underlying
        age = state.age(ticker)
        ubt = state.ubt(ticker)
        # OTHER candidates' resource-time -- excludes this one by design.
        opbt = queued_total - own[id(contract)]
        y = vals[id(contract)]
        # A candidate we could not measure ranks NEUTRAL, never best or worst.
        # Missing data is not evidence in either direction, and the same
        # convention already governs a null delta and an unreported open
        # interest elsewhere in this system.
        r = rank_within(y, pool) if (y is not None and pool) else 0.5
        reward = config.REWARD_LAMBDA * r
        scored.append(Scored(contract, signal, age, ubt, opbt,
                             pwt(age, ubt, opbt, reward),
                             y if y is not None else 0.0, reward))

    winner = max(
        scored,
        key=lambda s: (s.pwt, -(s.contract.spread_abs or 9.99), s.contract.underlying),
    )
    winner.selected = True
    scored.sort(key=lambda s: s.pwt, reverse=True)
    return winner, scored
