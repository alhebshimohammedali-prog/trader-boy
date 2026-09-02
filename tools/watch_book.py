"""Read-only watch on the open book: how close is each short put to its strike?

    python tools/watch_book.py             # dev account (per .env), one snapshot
    python tools/watch_book.py --comp      # competition account
    python tools/watch_book.py --comp --loop 60   # refresh every 60s

Why this exists
---------------
The agent gates hard on entry and then stops looking. `is_itm` appears in
exactly one place in the codebase -- gates.py, the ENTRY gate -- so it decides
which contracts we may open and never revisits the ones we hold. The only
automatic close is `_close_largest`, reachable solely from the 3% drawdown
breaker, which is an account-level backstop rather than position-level risk
management.

That leaves a real blind spot into the mark: five short puts at 1 DTE is the
point of maximum gamma, where a small move in the underlying moves the mark a
lot, and nothing in the running agent will say so.

Worse, the universe is scanned fresh each session and is not unioned with what
we hold, so a held name that drops out of the most-actives gets no signal, no
rv/iv and no delta that cycle. It is not merely unmanaged, it is unobserved.
This tool reads the book directly from the broker, so it sees those positions
regardless of what the scan returned.

This process NEVER places, cancels, or modifies an order. It calls exactly two
read tools -- positions and latest quote -- and prints. It is safe to run
against a live scored account mid-session, which is the entire point: the agent
keeps its autonomy, and the human gets a number the agent does not surface.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

import config
from src.data import Data, parse_occ
from src.mcp_client import AlpacaMCP

# A put seller's cushion is how far the underlying sits ABOVE the strike.
# Negative means the stock has come through the strike and the put is ITM.
# NEAR reuses MONEYNESS_MIN -- the agent will not OPEN a put closer than this
# to the money, so a position that has drifted inside it is, by the agent's own
# standard, no longer a position it would take.
NEAR_CUSHION = config.MONEYNESS_MIN


def cushion(strike: float, spot: float) -> float:
    """Fractional distance of spot above strike. Negative when ITM."""
    return (spot - strike) / strike if strike > 0 else 0.0


def status_of(cush: float) -> str:
    if cush <= 0:
        return "ITM"
    if cush < NEAR_CUSHION:
        return "NEAR"
    return "ok"


def dte_of(expiry: str, today) -> int:
    """Calendar days to expiry, floored at 0.

    Deliberately computed from an ET `today` passed in by the caller. The
    machine this runs on is not on New York time, and a day-boundary error here
    would misreport exactly the 1-DTE positions this tool exists to watch.
    """
    try:
        y, m, d = (int(x) for x in expiry.split("-"))
    except (ValueError, AttributeError):
        return -1
    return max((datetime(y, m, d).date() - today).days, 0)


async def snapshot(data: Data) -> int:
    """Print one pass over the book. Returns the count needing attention."""
    now = datetime.now(config.ET)
    account = await data.account()
    positions = await data.positions()

    shorts = [p for p in positions if p.is_option and p.qty < 0]
    if not shorts:
        print("no short option positions open")
        return 0

    hours_to_mark = (config.MARK_AT - now).total_seconds() / 3600
    equity = account.equity or 0.0
    print(f"{now:%a %d %b %H:%M} ET   {hours_to_mark:+.1f}h to mark   "
          f"equity ${equity:,.2f}")
    print()
    print(f"{'symbol':<24}{'strike':>9}{'spot':>9}{'cushion':>10}"
          f"{'dte':>5}  status")

    flagged = 0
    collateral = 0.0
    unparsed: list[str] = []

    # Sort by cushion so whatever is closest to trouble reads first. An
    # unparseable symbol sorts to the top for the same reason it halts entries
    # in the agent: we cannot say it is safe, so it should not look safe.
    rows = []
    for p in shorts:
        occ = parse_occ(p.symbol)
        if occ is None:
            unparsed.append(p.symbol)
            continue
        spot = await data.spot(occ.root, fresh=True)
        qty = abs(p.qty)
        collateral += qty * occ.collateral
        cush = cushion(occ.strike, spot) if spot else None
        rows.append((cush if cush is not None else -9.99, p, occ, spot, cush))

    for _, p, occ, spot, cush in sorted(rows, key=lambda r: r[0]):
        if spot is None or cush is None:
            print(f"{p.symbol:<24}{occ.strike:>9.2f}{'--':>9}{'--':>10}"
                  f"{dte_of(occ.expiry, now.date()):>5}  NO QUOTE")
            flagged += 1
            continue
        state = status_of(cush)
        if state != "ok":
            flagged += 1
        print(f"{p.symbol:<24}{occ.strike:>9.2f}{spot:>9.2f}"
              f"{cush:>+9.2%}{dte_of(occ.expiry, now.date()):>5}  {state}")

    print()
    deployed = collateral / equity if equity else 0.0
    print(f"{len(shorts)} short put(s), ${collateral:,.0f} collateral "
          f"({deployed:.0%} of equity)")

    if unparsed:
        # Same reasoning as the agent's own gate: an unparseable symbol means
        # the collateral total above is understated, so do not trust it.
        print(f"  !! {len(unparsed)} unparseable symbol(s): {', '.join(unparsed)}")
        print("  !! collateral above is an UNDER-count; treat it as unknown")
        flagged += len(unparsed)

    if flagged:
        print(f"  !! {flagged} position(s) at or near the strike")
        print("  !! the agent will not act on this; --flatten is the manual exit")
    return flagged


async def main() -> int:
    load_dotenv()
    account = "comp" if "--comp" in sys.argv else os.getenv("ALPACA_ACCOUNT", "dev")

    loop_seconds = 0
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        loop_seconds = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 60

    print(f"=== book watch: {account.upper()} account (read-only) ===\n")

    async with AlpacaMCP(account=account) as mcp:
        mcp.assert_paper()
        data = Data(mcp)
        while True:
            await snapshot(data)
            if not loop_seconds:
                return 0
            print("\n" + "-" * 60 + "\n")
            await asyncio.sleep(loop_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nstopped.")
        raise SystemExit(0)
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
