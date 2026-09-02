"""Layer 4b: rule-driven exits on positions we already hold.

The agent gated hard on entry and then stopped looking. `is_itm` lived in
exactly one place -- gates.py, the ENTRY gate -- so it decided what we could
open and never revisited what we owned. The only automatic close was
`_close_largest`, reachable solely from the 3% drawdown breaker, which is an
account-level backstop rather than position-level risk management. A put that
travelled from 0.18 delta to 0.60 was not the trade we put on; it was a
different and much worse trade held by default rather than by decision.

Three deliberate choices, each of which could reasonably have gone the other
way:

**No model in this path.** Entries consult the LLM because the safe default
when opening risk is *don't*, so a timeout failing closed costs us a trade and
nothing more. Exits invert that: the dangerous answer is inaction, so a model
timeout must not be able to keep a position open. Rather than build a fail-open
model call -- a safety control whose correctness depends on a network -- there
is no model call here at all. These rules are arithmetic on numbers the agent
already computes every cycle.

**Scoped, never a flatten.** README: "The agent has no self-flatten path,
because one would eventually fire for a bad reason." That reasoning is about
closing the whole book on one global signal, where a false trigger costs the
week. These rules close ONE position on ITS OWN state, so a false trigger costs
that position's remaining decay. `_close_largest` already establishes that the
agent may close a position without a human; this extends that path rather than
opening a new one.

**Confirmation before acting.** A trigger must hold for EXIT_PERSIST_CYCLES
consecutive cycles, so a single bad print cannot close a good position. The one
exception is an ITM breach inside EXIT_GAMMA_DTE, where there is no next cycle
worth waiting for -- and that check reads a FRESH quote for the same reason
`_close_largest` does: the breaker only matters in a fast market, which is
exactly when a cached price is most wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import config

# Rule names are logged verbatim, so they are part of the audit trail.
ITM = "itm"
DELTA_DRIFT = "delta_drift"
LOSS_MULTIPLE = "loss_multiple"
EXPIRY_GAMMA = "expiry_gamma"
EXTRINSIC_GONE = "extrinsic_gone"


def intrinsic_value(strike: float, spot: float | None, qty: int) -> float | None:
    """Dollar intrinsic of a short put: what assignment would cost anyway."""
    if spot is None:
        return None
    return max(0.0, strike - spot) * 100.0 * abs(qty)


def extrinsic_value(mark: float | None, intrinsic: float | None) -> float | None:
    """Dollar time value left in the contract -- the part closing gives away.

    Buying a short put back costs intrinsic + extrinsic. The intrinsic is owed
    either way: on a CASH-SECURED put, assignment means buying the underlying
    at the strike, which is the trade that was sold in the first place and for
    which the cash is already posted. The extrinsic is the only part that is
    genuinely surrendered by closing early, and it is also the only part still
    being earned by holding.

    That makes it the number an exit decision actually turns on, and it is not
    the number any of the original rules looked at.
    """
    if mark is None or intrinsic is None:
        return None
    return max(0.0, mark - intrinsic)


@dataclass
class ExitSignal:
    symbol: str
    ticker: str
    rule: str
    why: str
    urgent: bool = False  # skips the persistence requirement

    def row(self) -> dict:
        return {"symbol": self.symbol, "ticker": self.ticker,
                "rule": self.rule, "why": self.why, "urgent": self.urgent}


def credit_received(market_value: float | None,
                    unrealized_pl: float | None) -> float | None:
    """What we were paid to open this short, from fields the broker already returns.

    Alpaca does not hand us the opening credit on a position, but it is
    recoverable and does not need storing: we sold for C, the position is now
    worth M, so P&L = C - M, and a short's market_value is reported as -M.
    Therefore C = P&L + |market_value|.

    Deriving it beats persisting it. A credit written to state.json is lost by
    the same crash that loses everything else, and this layer must keep working
    on a restart onto a clean checkout -- which is precisely when a position is
    held by a process that never opened it.
    """
    if market_value is None or unrealized_pl is None:
        return None
    credit = unrealized_pl + abs(market_value)
    return credit if credit > 0 else None


def cushion(strike: float, spot: float) -> float:
    """How far the underlying sits ABOVE the strike. Negative once ITM."""
    return (spot - strike) / strike if strike > 0 else 0.0


def dte_from_expiry(expiry: str, today) -> int:
    """Days to expiry from an OCC expiry string. `today` MUST be the ET date.

    Same discipline as allocation.dte and for the same reason: the operator's
    machine runs UTC+8, so date.today() is already tomorrow in market terms.
    That error previously made a 4 Sep contract read as 2 DTE instead of 3 --
    here it would misclassify exactly the 1-DTE positions the gamma rule exists
    to catch, in the direction that fires it a day early.
    """
    from datetime import date as _date

    try:
        y, m, d = (int(x) for x in expiry.split("-"))
    except (ValueError, AttributeError):
        return 0  # unreadable expiry -> treat as expiring now, the safe end
    return max((_date(y, m, d) - today).days, 0)


def evaluate(
    symbol: str,
    ticker: str,
    strike: float,
    spot: float | None,
    delta: float | None,
    market_value: float | None,
    unrealized_pl: float | None,
    dte: int,
    qty: int = 1,
) -> ExitSignal | None:
    """The first rule that fires, or None.

    Ordered by what closing COSTS, not by how alarming the position looks.

    The first version of this ordered by alarm and got PLTR badly wrong. It saw
    spot through the strike, called it an emergency, and paid 6.55 to buy back a
    put sold for 0.94. Of that 6.55, 4.76 was intrinsic -- owed anyway, since the
    cash was already secured and assignment is the trade we sold -- and 1.79 was
    extrinsic. So it surrendered $179 of time value to exit a position that had
    ever earned $94, and called it risk management.

    An exit is a purchase. It has a price, and the price is the extrinsic value
    given up. A rule that never looks at that price is not managing risk, it is
    just reacting to a red number.
    """
    credit = credit_received(market_value, unrealized_pl)
    mark = abs(market_value) if market_value is not None else None
    intr = intrinsic_value(strike, spot, qty)
    extr = extrinsic_value(mark, intr)

    # THE PRICE OF LEAVING. Checked before any rule may fire.
    #
    # Paying away more time value than the position ever collected cannot be
    # correct on a cash-secured put: the downside is already funded, assignment
    # is the contracted outcome, and the extrinsic is the one component that
    # decays in our favour if we simply wait. Buying it back at a premium is
    # paying to cancel the only part of the trade still working.
    #
    # This is a HARD block, not a weight. Every rule below is a reason to want
    # out; none of them is a reason to overpay for it.
    #
    # It applies ONLY while the put is in the money, and that restriction is
    # load-bearing rather than cautious. The whole argument is "the intrinsic is
    # owed anyway, so only the extrinsic is truly surrendered" -- and that is
    # only true once intrinsic exists. An OUT-of-the-money put has none: its
    # entire mark is the market's price for the risk still outstanding, and
    # paying that to step out of the way is a legitimate risk decision rather
    # than a giveaway.
    #
    # Blocking on extrinsic alone made loss_multiple unreachable, since an OTM
    # mark is all extrinsic and therefore always exceeded the credit at 2.5x.
    # That silently deleted the only rule standing between a position and a
    # catastrophic run, which is a far worse failure than the one being fixed.
    too_expensive = (intr is not None and intr > 0
                     and extr is not None and credit
                     and extr > config.EXIT_MAX_EXTRINSIC_GIVEUP * credit)

    if spot is not None:
        cush = cushion(strike, spot)

        # 1. Deep enough that there is nothing left to earn.
        #
        # This is the ONLY unambiguously good exit on a cash-secured put. With
        # the extrinsic gone the contract has stopped paying us to hold it,
        # buying it back costs essentially just the intrinsic we already owe,
        # and closing releases the collateral. Cheap to leave and nothing left
        # to stay for -- the exact inverse of the PLTR case.
        if (extr is not None and credit
                and extr <= config.EXIT_EXTRINSIC_FLOOR * credit):
            return ExitSignal(
                symbol, ticker, EXTRINSIC_GONE,
                f"only {extr:.0f} time value left on {credit:.0f} credit; "
                f"nothing further to earn",
                urgent=dte <= config.EXIT_GAMMA_DTE,
            )

        # 2. Through the strike.
        #
        # No longer urgent by itself, and no longer sufficient by itself. ITM
        # says assignment is likely; on a cash-secured put that is a funded,
        # contracted outcome, not an emergency. It only justifies paying to
        # leave when leaving is cheap, which is what the block below enforces.
        if cush <= 0 and not too_expensive:
            return ExitSignal(
                symbol, ticker, ITM,
                f"spot {spot:.2f} through strike {strike:.2f}, "
                f"exit gives up {extr:.0f} of {credit:.0f} credit"
                if extr is not None and credit else
                f"spot {spot:.2f} through strike {strike:.2f}",
            )

        # 3. Near the strike with no time left. At <=1 DTE gamma is at its
        #    maximum: delta moves fastest, and the mark we are scored on can
        #    swing on a move too small to trip any other rule.
        if (dte <= config.EXIT_GAMMA_DTE and cush < config.EXIT_GAMMA_CUSHION
                and not too_expensive):
            return ExitSignal(
                symbol, ticker, EXPIRY_GAMMA,
                f"{cush:.2%} cushion at {dte} DTE (gamma)",
                urgent=True,
            )

    if too_expensive:
        # Every remaining rule is a loss rule, and a loss rule that fires here
        # would be buying back time value at a premium to avoid a loss already
        # marked into equity. Hold, keep earning the decay, and let the gates
        # stop us adding more.
        return None

    # 3. Delta has drifted past what the entry band would ever have accepted.
    #    EXIT_DELTA_MAX is not a new number: it is MAX_EMPIRICAL_ITM, already
    #    derived as DELTA_MAX + 0.10 for the assignment-history gate. A position
    #    beyond it is one this agent would refuse to open today.
    if delta is not None and abs(delta) > config.EXIT_DELTA_MAX:
        return ExitSignal(symbol, ticker, DELTA_DRIFT,
                          f"|delta| {abs(delta):.3f} over {config.EXIT_DELTA_MAX:.2f}")

    # 4. The loss has run past a multiple of the credit. Priced in the units the
    #    trade was opened in, which is the only scale on which "this went wrong"
    #    is comparable across a $52 strike and a $167 one.
    credit = credit_received(market_value, unrealized_pl)
    if credit and market_value is not None:
        cost_to_close = abs(market_value)
        if cost_to_close >= config.EXIT_LOSS_MULTIPLE * credit:
            return ExitSignal(
                symbol, ticker, LOSS_MULTIPLE,
                f"costs {cost_to_close:.0f} to close vs {credit:.0f} credit "
                f"({cost_to_close / credit:.1f}x)")

    return None


def confirmed(signal: ExitSignal | None, streak: int) -> bool:
    """Should this signal be acted on now?

    `streak` is how many consecutive cycles this position has fired, including
    this one. Urgent signals act immediately because the next cycle is fifteen
    minutes away and, at <=1 DTE through the strike, fifteen minutes is the
    whole remaining life of the option.
    """
    if signal is None:
        return False
    if signal.urgent:
        return True
    return streak >= config.EXIT_PERSIST_CYCLES
