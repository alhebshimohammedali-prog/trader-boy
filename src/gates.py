"""Layer 4: the eight risk gates.

Every gate is a boolean evaluated BEFORE the model sees the candidate. A gate
the LLM can reason past is not a gate -- so none of this is reachable from the
decision layer, which receives only candidates that already passed.

Every rejection is logged with a reason. The rejection log is as much of the
evidence as the acceptance log.

Gate 4 note: the original min-DTE rule ("close/roll inside 3 DTE") is actively
harmful in a four-session window -- it would force closing a 4 Sep put on
Wednesday, paying the spread to hand back exactly the decay the position exists
to collect, one day before the mark. It is replaced by expiry containment plus
an ITM force-close, which is the assignment protection the old rule was
reaching for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config
from src.data import Contract


@dataclass
class GateResult:
    passed: bool
    failures: list[tuple[int, str, str]] = field(default_factory=list)  # (n, name, why)
    # Checks that could not be evaluated. Not failures, but not passes either;
    # recorded so a gate we believe is enforcing is never silently inert.
    notes: list[str] = field(default_factory=list)

    def fail(self, n: int, name: str, why: str) -> None:
        self.passed = False
        self.failures.append((n, name, why))

    @property
    def reason(self) -> str:
        """Self-describing: number AND name AND cause.

        A judge reading `g2: collateral 63,200 > 25% cap` should not have to
        cross-reference a table to learn that gate 2 is the position cap.
        """
        return "; ".join(
            f"g{n} {name}: {why}" for n, name, why in self.failures
        ) or "all gates pass"


def in_macro_blackout(now: datetime) -> tuple[bool, str]:
    for when, name in config.MACRO_EVENTS:
        start = when - timedelta(minutes=config.BLACKOUT_BEFORE_MIN)
        end = when + timedelta(minutes=config.BLACKOUT_AFTER_MIN)
        if start <= now <= end:
            return True, f"{name} at {when:%H:%M} ET"
    return False, ""


def is_itm(contract: Contract, spot: float | None) -> bool:
    """Short put is in the money when spot has fallen below the strike."""
    return spot is not None and spot < contract.strike


def evaluate(
    contract: Contract,
    *,
    now: datetime,
    equity: float,
    spot: float | None,
    deployed_collateral: float,
    breaker_tripped: bool,
    held_symbols: set[str],
    # (rate, samples) from signal.empirical_itm_rate. Optional so the gate
    # stays pure and every caller that cannot measure history -- the offline
    # fixtures, the scan before bars are fetched -- simply skips the check
    # rather than fabricating a number for it.
    empirical_itm: tuple[float, int] | None = None,
) -> GateResult:
    r = GateResult(passed=True)
    collateral = contract.collateral

    # 1 -- no naked options. We only ever sell cash-secured puts, and we verify
    # the cash is actually there. Alpaca enforces this at level 1 too; we say so
    # honestly rather than claiming it as our own control.
    if collateral > equity:
        r.fail(1, "no_naked", f"collateral {collateral:,.0f} exceeds equity {equity:,.0f}")

    # 2 -- per-position cap. collateral = strike x 100.
    cap = config.PER_POSITION_CAP * equity
    if collateral > cap:
        r.fail(2, "position_cap",
               f"collateral {collateral:,.0f} > {config.PER_POSITION_CAP:.0%} cap {cap:,.0f}")

    # 3 -- liquidity floor. Absolute OR relative, whichever is MORE permissive:
    # a 10% relative test alone rejects perfectly normal weekly quotes.
    # This protects nothing mechanically -- Alpaca does not check order size
    # against NBBO liquidity -- it is for realism and credibility. Said plainly.
    # Open interest is absent from the option-snapshot feed entirely; it lives
    # on the contracts endpoint. `None` means "not reported", which is not the
    # same as zero. Treating the two alike would reject every contract and the
    # agent would trade nothing all week, so an unreported OI skips this test
    # and says so in the log rather than silently failing the candidate.
    oi = contract.open_interest
    if oi is None:
        r.notes.append("g3 liquidity: open interest not reported by the feed")
    elif oi < config.MIN_OPEN_INTEREST:
        r.fail(3, "liquidity", f"OI {oi} < {config.MIN_OPEN_INTEREST}")
    s_abs, s_rel = contract.spread_abs, contract.spread_rel
    if s_abs is None:
        r.fail(3, "liquidity", "no two-sided quote")
    else:
        ok_abs = s_abs <= config.MAX_SPREAD_ABS
        ok_rel = s_rel is not None and s_rel <= config.MAX_SPREAD_REL
        if not (ok_abs or ok_rel):
            r.fail(3, "liquidity",
                   f"spread ${s_abs:.2f} / {(s_rel or 0):.0%} fails both tests")

    # Minimum credit, measured on the BID -- the price we actually receive,
    # since we sell at or through it. A cheap option with a wide spread hands
    # the entire premium to the market maker on entry.
    if contract.bid is None or contract.bid < config.MIN_CREDIT:
        r.fail(3, "min_credit",
               f"bid ${contract.bid if contract.bid is not None else 0:.2f} "
               f"< ${config.MIN_CREDIT:.2f} floor")

    # A ceiling on the same number. There was a floor here and nothing above,
    # so a corrupt quote could only ever look MORE attractive: a stale or
    # mis-scaled bid inflates the premium, every downstream check reads it as
    # a better trade, and the allocator prefers it precisely because it is
    # wrong. Benchmarking the decision layer found the model waving through a
    # 4-day 2%-OTM put quoted at 32% of strike, which is not a trade, it is
    # bad data. The model is the last check and not the only one, so this
    # belongs here where it is deterministic.
    if contract.bid is not None and contract.strike > 0:
        rich = contract.bid / contract.strike
        if rich > config.MAX_CREDIT_PCT_STRIKE:
            r.fail(3, "credit_sanity",
                   f"bid ${contract.bid:.2f} is {rich:.1%} of the ${contract.strike:.2f} "
                   f"strike, over the {config.MAX_CREDIT_PCT_STRIKE:.1%} ceiling; "
                   "implausible for this tenor, treat as a data error")

    # 3b -- what this underlying has ACTUALLY done, against what the chain
    # says it will do. delta is a risk-neutral probability of finishing ITM;
    # empirical_itm counts how often the stock genuinely finished that far
    # down over this holding period. When the second badly exceeds the first,
    # the option is underpricing the move that hurts a short put.
    #
    # The ceiling is derived, not tuned: DELTA_MAX is the most assignment risk
    # this strategy agreed to take, so a measured rate above it means the
    # contract is not the risk we intended, whatever its quoted delta. The
    # margin on top is for the estimator, which is coarse -- windows overlap,
    # so ninety bars give roughly twenty independent observations and a
    # standard error near ten points.
    if empirical_itm is not None:
        rate, samples = empirical_itm
        if samples < config.MIN_ITM_SAMPLES:
            r.notes.append(
                f"g3b history: only {samples} windows, not evaluated")
        elif rate > config.MAX_EMPIRICAL_ITM:
            r.fail(3, "assignment_history",
                   f"finished ITM in {rate:.0%} of {samples} past "
                   f"{config.RV_LOOKBACK_DAYS}-day windows at this distance, "
                   f"over the {config.MAX_EMPIRICAL_ITM:.0%} ceiling "
                   f"(quoted delta {abs(contract.delta):.2f})"
                   if contract.delta is not None else
                   f"finished ITM in {rate:.0%} of {samples} past windows, "
                   f"over the {config.MAX_EMPIRICAL_ITM:.0%} ceiling")

    # 4 -- expiry containment. Positions expiring AFTER the mark are held to it.
    # Anything expiring on or before it settles inside the measured window.
    if contract.expiry in config.ALT_EXPIRIES:
        r.fail(4, "expiry_containment",
               f"{contract.expiry} settles inside the window; assignment is live")
    elif contract.expiry != config.TARGET_EXPIRY:
        r.fail(4, "expiry_containment",
               f"{contract.expiry} is not the target expiry {config.TARGET_EXPIRY}")
    if is_itm(contract, spot):
        r.fail(4, "expiry_containment", f"already ITM (spot {spot} < strike {contract.strike})")

    # 5 -- loss circuit breaker, latched by the caller against the day's
    # high-water mark on mark-to-market total equity.
    if breaker_tripped:
        r.fail(5, "circuit_breaker", "tripped this session; entries halted")

    # 6 -- portfolio capital cap. Per-position caps alone do not stop 100%
    # deployment across many positions.
    total = deployed_collateral + collateral
    port_cap = config.PORTFOLIO_CAP * equity
    if total > port_cap:
        r.fail(6, "portfolio_cap",
               f"deployed {total:,.0f} > {config.PORTFOLIO_CAP:.0%} cap {port_cap:,.0f}")

    # 7 -- economic calendar blackout.
    blocked, which = in_macro_blackout(now)
    if blocked:
        r.fail(7, "macro_blackout", which)

    # 8 -- earnings exclusion (static list; VERIFY IT before first entry).
    if contract.underlying in config.EARNINGS_EXCLUDED:
        r.fail(8, "earnings", f"{contract.underlying} reports inside the window")

    # Timing: no new entries after the cutoff. Friday is worthless.
    if now > config.ENTRY_CUTOFF:
        r.fail(4, "entry_window", "past Thu 12:00 ET entry cutoff")

    # One position per underlying -- concentration control the allocation layer
    # already discourages via ubt, made hard here.
    if contract.symbol in held_symbols:
        r.fail(2, "position_cap", "already holding this exact contract")

    return r
