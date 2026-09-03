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
    crowding: float = 0.0
    corr: float = 0.0
    selected: bool = False
    held: bool = False

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
            "corr": round(self.corr, 3),
            "crowding": round(self.crowding, 4),
            "pwt": round(self.pwt, 4),
            "selected": self.selected,
            "held": self.held,
        }


def dte(contract: Contract, today) -> int:
    """Days to expiry. `today` MUST be the ET date, not the machine's.

    The operator's machine runs UTC+8, so date.today() was already tomorrow
    from the market's point of view and every contract looked a day shorter
    than it was. A 4 Sep expiry read as 2 DTE on 1 Sep instead of 3, which
    inflated expected_yield by half, understated capital_time, and had the
    critic vetoing every candidate for a "2-day tenor inconsistent with a
    4 Sep expiry" -- correctly spotting an inconsistency the agent had
    manufactured itself.
    """
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


def pwt(age: int, ubt: float, opbt: float, reward: float = 0.0,
        crowding: float = 0.0) -> float:
    """pwt = w*age - ubt + opbt + reward - crowding.

    Not invented here. This is the ADR algorithm -- Al-Hebshi, Daileg & Ramos,
    "ADR Algorithm: A Next Gen Scheduling for Minimizing Waiting Time",
    Zenodo, 2025, doi:10.5281/zenodo.22265132 -- a CPU scheduler with the
    resource swapped from processor time to capital.

    ADR ranks jobs by Predicted Waiting Time: how long a process WOULD wait if
    it were least prioritised. It runs whichever is closest to starving.

        PWT = (CTL - AT) - UBT + OPBT         select max(PWT)

        CTL - AT   time in the ready queue      -> age
        UBT        burst time already used      -> collateral-days consumed
        OPBT       OTHER jobs' remaining burst  -> other candidates' resource-time

    The OPBT exclusion is load-bearing and it is inherited rather than designed:
    a job's own remaining burst is left out of the sum over others, so a longer
    job scores lower and shorter jobs win. Ported to capital, a candidate tying
    up more equity for longer is deprioritised -- capital efficiency that falls
    out of the scheduling semantics instead of being bolted on afterwards.

    Two things deliberately differ from the source. The objective: ADR
    minimises waiting time, this minimises idle capital. The tie-break: ADR uses
    lowest PID, this uses tightest spread then ticker, because determinism
    matters more here than arrival order. `reward` and `crowding` have no
    counterpart in ADR and are argued for on their own terms below.

    age       cycles since this ticker last received capital
    ubt       collateral-days this ticker has already consumed
    opbt      committed resource-time of every OTHER queued candidate
    reward    lambda x the candidate's rank on variance risk premium
    crowding  mu x how correlated this name is with what we already hold

    crowding is the term that makes diversification mean something. ubt stops
    the book doubling into one TICKER and nothing more, so NVDA, MRVL, SMCI and
    DRAM together read as four-way diversification while being one
    semiconductor bet in four wrappers. Herfindahl would report 0.25 and look
    healthy right up to the morning the sector gaps and every leg goes ITM at
    once. Correlation is measured from the same daily bars the signal uses.

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
    # age SATURATES rather than growing without limit. It was the only
    # unbounded term here, and by cycle 27 it contributed 2.70 against a
    # crowding penalty that tops out at 0.30, so a patient name won regardless
    # of how correlated it was with the book.
    #
    # A hard cap would have flattened every name past the ceiling to the same
    # value, cancelling the term out of the comparison -- the same defect this
    # index already suffered when first_qualified never moved. The smooth form
    # keeps the ordering (25 cycles still ranks below 27) while bounding the
    # total, and preserves the original slope at age 1.
    # Clamped at zero because the saturating form has a pole at
    # age = -AGE_HALF_CYCLES and blows up approaching it: age -9 yields -9.0,
    # a larger distortion than the unbounded term this replaced, and age -10
    # raises ZeroDivisionError inside the allocator.
    #
    # A negative age is not hypothetical. `age` is cycle - first_qualified, and
    # a state restore can leave first_qualified ahead of a rewound cycle
    # counter; the live logs already show a minimum of -2. It is also
    # meaningless -- "cycles since last funded" cannot be negative -- so
    # clamping is the correct reading, not merely the safe one.
    age = max(0, age)
    aged = config.AGE_WEIGHT * config.AGE_HALF_CYCLES * age / (age + config.AGE_HALF_CYCLES)
    return aged - ubt + opbt + reward - crowding


def select(
    runnable: list[tuple[Contract, float]],
    state: State,
    equity: float,
    today,
    edge: dict[str, float] | None = None,
    crowd: dict[str, float] | None = None,
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

        # Correlation against what is ALREADY held. An unmeasured pair
        # contributes nothing rather than a assumed zero: "we could not tell"
        # and "they move independently" are different claims, and only one of
        # them is safe to act on.
        c = (crowd or {}).get(ticker)
        crowding = config.CROWDING_MU * max(0.0, c) if c is not None else 0.0

        scored.append(Scored(contract, signal, age, ubt, opbt,
                             pwt(age, ubt, opbt, reward, crowding),
                             y if y is not None else 0.0, reward,
                             crowding=crowding,
                             corr=c if c is not None else 0.0))

    winner = max(
        scored,
        key=lambda s: (s.pwt, -(s.contract.spread_abs or 9.99), s.contract.underlying),
    )
    winner.selected = True
    scored.sort(key=lambda s: s.pwt, reverse=True)
    return winner, scored


def score_book(
    held: list[tuple[Contract, float]],
    state: State,
    equity: float,
    today,
    edge: dict[str, float] | None = None,
) -> list[Scored]:
    """Score the positions we already hold on the same index as candidates.

    `pwt()` is a pure function of (age, ubt, opbt, reward, crowding). It never
    cared whether a contract is owned -- `select()` simply never called it on
    the book. Doing so costs nothing new and answers the one question the agent
    currently cannot ask: *is a position I hold worse than one I could open?*

    Why this is a completion rather than a feature. The README grounds PWT in
    Gittins, and a real index policy scores the incumbent alongside the
    alternatives and drops it when it loses. Ours scored only the alternatives,
    so an incumbent was immortal until expiry no matter how far its edge decayed
    -- which is not an index policy, it is an index policy for admissions and a
    buy-and-hold for everything after.

    Term semantics carry over unchanged, and the carry-over is the point:

      ubt       still collateral-days consumed. A held position has spent them,
                so ubt is large -> low score. This is the term that already
                encodes "you have had your turn".
      opbt      still every OTHER contender's committed resource-time, held and
                queued alike, so held and candidate rows are directly
                comparable. Excluding self is as load-bearing here as in
                select(): include it and the index inverts and starts
                preferring the largest position.
      age       deliberately NOT applied, and this one is a trap. Since
                `charge_capital` resets first_qualified on funding, age means
                "cycles since this ticker last received capital" -- so for a
                position we still hold it counts how long we have held it, and
                pwt ADDS it. Scored naively, a position would become harder to
                displace the longer it was held, which is the exact opposite of
                rotation. Worse for this strategy specifically: longer held
                means closer to expiry means LESS premium left to collect, so
                the term would rank remaining value upside down.
                age is an anti-starvation term for candidates WAITING on
                capital. A funded position is not starving; it has the capital.
      crowding  also NOT applied, for the neighbouring reason. A held position
                correlating with the book is mostly correlating with itself,
                and charging it for that double-counts what ubt already takes.

    Both excluded terms describe a candidate's relationship to the QUEUE, and
    neither means anything for something already funded. What remains --
    -ubt + opbt + reward -- decays as a position consumes capital and rises
    with the edge it still offers, which is exactly the quantity a rotation
    decision needs.

    `reward` uses the same edge map as `select()`, so a name whose realised vol
    has caught its implied -- the premium no longer compensating for the
    movement -- ranks low here exactly as it would as a candidate. That is the
    signal that a held position has stopped being worth its collateral, and it
    is already computed every cycle and currently discarded.
    """
    if not held:
        return []

    entries = [(c, sig, dte(c, today)) for c, sig in held]
    own = {id(c): capital_time(c, equity, d) for c, _s, d in entries}
    book_total = sum(own.values())

    if edge:
        vals = {id(c): edge.get(c.underlying) for c, _s, _d in entries}
    else:
        vals = {id(c): expected_yield(c, d) for c, _s, d in entries}
    pool = [v for v in vals.values() if v is not None]

    scored: list[Scored] = []
    for contract, signal, d in entries:
        ticker = contract.underlying
        ubt = state.ubt(ticker)
        opbt = book_total - own[id(contract)]
        y = vals[id(contract)]
        r = rank_within(y, pool) if (y is not None and pool) else 0.5
        reward = config.REWARD_LAMBDA * r
        # age is carried on the row for the log -- "cycles held" is worth
        # reading -- but passed as 0 into the index. See the docstring: for a
        # funded position the term points the wrong way.
        scored.append(Scored(contract, signal, state.age(ticker), ubt, opbt,
                             pwt(0, ubt, opbt, reward, 0.0),
                             y if y is not None else 0.0, reward,
                             held=True))

    scored.sort(key=lambda s: s.pwt)  # weakest first: the rotation candidate
    return scored


def rotation(
    book: list[Scored],
    candidates: list[Scored],
    margin: float,
) -> tuple[Scored, Scored] | None:
    """The weakest held position and the strongest candidate, if swapping them
    clears `margin`. Returns None when holding is the better answer.

    The margin is not a tuning knob, it is the round trip. Rotating pays the
    spread twice -- once to buy the position back, once to sell the new one --
    so a candidate that merely edges ahead on the index is a worse trade than
    doing nothing, and an unmargined comparison would churn the book every time
    two scores crossed. Requiring the gap to exceed the cost of crossing it is
    what makes this a rotation rule rather than a coin flip with commissions.

    Deliberately returns at most ONE pair per cycle. The daily order cap already
    bounds churn, but rotating several positions on a single cycle's numbers
    would act on far more conviction than one snapshot of the book supports.
    """
    if not book or not candidates:
        return None
    weakest = min(book, key=lambda s: s.pwt)
    best = max(candidates, key=lambda s: s.pwt)
    if best.pwt - weakest.pwt <= margin:
        return None
    return weakest, best
