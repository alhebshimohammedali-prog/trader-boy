"""Entry point.

    python run.py --once --dry        # one cycle, no orders sent
    python run.py --once              # one cycle, will place an order
    python run.py                     # live loop until the Thursday mark
    python run.py --comp              # the competition account

The --comp flag is the only thing standing between the dev account and the
account you are scored on. It is deliberately explicit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

import config
from src.agent import Agent, market_open, now_et
from src.logbook import Logbook
from src.mcp_client import AlpacaMCP


async def _held_roots(mcp) -> list[str]:
    """Underlyings of the short option positions currently open, in order.

    Never raises, for the same reason `universe.scan` never raises: a universe
    is not worth a session. If the broker call fails we lose visibility on held
    names for this scan, which is bad -- but losing the whole universe would be
    worse, and the next rescan gets another attempt.
    """
    from src.data import Data, parse_occ

    roots: list[str] = []
    try:
        for p in await Data(mcp).positions():
            if not (p.is_option and p.qty < 0):
                continue
            occ = parse_occ(p.symbol)
            if occ is not None and occ.root not in roots:
                roots.append(occ.root)
    except Exception:  # noqa: BLE001 - deliberately total; see docstring
        return []
    return roots


async def _scan_universe(mcp, log) -> None:
    """Replace the configured universe with one built from the live market.

    Three levels of fallback, because a universe is not worth a session. A scan
    that fails, or that returns fewer names than PWT needs to arbitrate
    between, leaves config.UNIVERSE_CANDIDATES exactly as it was. src.universe
    .scan never raises for the same reason.
    """
    from src import universe

    acct = await mcp.call("account")
    equity = float((acct or {}).get("equity") or 100_000.0)

    log.note("\nscanning the market for a universe...")
    rows, rejected = await universe.scan(mcp, equity, now_et(),
                                         pool_size=config.SCAN_POOL,
                                         note=log.note)
    chosen = [r["ticker"] for r in rows[:config.SCAN_TOP]]

    # Fall back only when the scan finds NOTHING. It used to fall back below
    # UNIVERSE_MIN_NAMES, on the reasoning that PWT wants several candidates to
    # arbitrate between -- but that confused two different things. The
    # configured list is not safer: every name on it goes through the same
    # gates, every cycle. Falling back does not skip a single risk check, it
    # just swaps one candidate pool for another that a human typed.
    #
    # So if the market offers four tradeable names, four is what the market
    # offers, and the agent trades them. A thin universe is information, not a
    # malfunction. The fallback exists for a scan that failed outright, which
    # is a different thing entirely.
    if not chosen:
        log.note("  scan returned nothing (data problem, not a thin market). "
                 "Falling back to the configured universe.")
        log.note(f"universe ({len(config.UNIVERSE_CANDIDATES)}, configured): "
                 f"{', '.join(config.UNIVERSE_CANDIDATES)}")
        return

    if len(chosen) < config.UNIVERSE_MIN_NAMES:
        log.note(f"  thin market: {len(chosen)} names cleared the gates, under "
                 f"the {config.UNIVERSE_MIN_NAMES} PWT prefers. Trading them "
                 f"anyway -- they passed the same gates the configured list "
                 f"would have to.")

    # Names we HOLD stay in the universe whether or not they are still
    # most-active, because the universe is not only the shopping list -- it is
    # also the only thing the signal layer computes over. A held name that
    # drops out of the scan gets no iv, no rv_iv, no momentum and no re-solved
    # delta that cycle, so the agent stops measuring a position it still owns.
    #
    # Seen live on 2 Sep: the scan returned INTC, PLTR, DRAM, NVDA while the
    # book was HOOD, INTC, NFLX, DRAM, PLTR. Two of five positions were not
    # merely unmanaged, they were unobserved.
    #
    # This adds no new risk of doubling up: gate 2 still rejects a contract we
    # already hold, and the crowding term still penalises a second position on
    # the same underlying. It only restores visibility.
    held = await _held_roots(mcp)
    readded = [r for r in held if r not in chosen]
    chosen = chosen + readded

    config.UNIVERSE_CANDIDATES = chosen
    log.note(f"  {len(rejected)} rejected by the gates before ranking")
    log.note(f"universe ({len(chosen)}, scanned): {', '.join(chosen)}")
    if readded:
        log.note(f"  re-added {len(readded)} held name(s) absent from the scan: "
                 f"{', '.join(readded)}")

    # Said plainly every run, because it is the one risk the scan cannot see.
    # Alpaca exposes no earnings calendar through this server, so a scanned
    # name may be reporting inside the window, and selling a put into a print
    # is the reliable way to lose a short-put book in four sessions.
    log.note("  NOTE: earnings are NOT verified for scanned names. "
             "Run with --no-scan to use the hand-checked list instead.")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comp", action="store_true", help="use the competition account")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry", action="store_true", help="never submit an order")
    ap.add_argument("--quiet", action="store_true", help="jsonl only, no table")
    ap.add_argument("--force", action="store_true",
                    help="allow --once outside market hours (stale quotes)")
    ap.add_argument("--no-scan", action="store_true",
                    help="skip the market scan; use the configured universe")
    ap.add_argument("--flatten", action="store_true",
                    help="EMERGENCY: close every short option position and stop")
    args = ap.parse_args()

    load_dotenv()
    account = "comp" if args.comp else None
    log = Logbook(echo=not args.quiet)

    async with AlpacaMCP(account=account) as mcp:
        log.note(mcp.tool_report())
        if mcp.account == "comp":
            log.note("\n*** COMPETITION ACCOUNT: orders here are scored ***\n")

        scanning = config.AUTO_SCAN and not args.no_scan
        if not scanning:
            log.note(f"universe ({len(config.UNIVERSE_CANDIDATES)}): "
                     f"{', '.join(config.UNIVERSE_CANDIDATES)}")

        # Handed to the agent rather than run once here, so it can rebuild the
        # universe at each session open and periodically through the day. A
        # universe chosen on after-hours quotes is not the market it will be
        # trading in three hours later.
        async def _rescan():
            await _scan_universe(mcp, log)

        agent = Agent(mcp, log, place_orders=not args.dry,
                      rescan=_rescan if scanning else None)

        # --once has no loop to rescan inside, so do it now.
        if scanning and (args.once or args.flatten):
            await _scan_universe(mcp, log)
        try:
            if args.flatten:
                await agent.flatten()
            elif args.once:
                # run_forever refuses to trade outside the session; --once did
                # not, because it calls run_cycle directly. Running it after
                # the close placed a real order on stale quotes, which Alpaca
                # queued for the next open -- a Monday-afternoon price waiting
                # to fill into Tuesday's market. Nobody asks for that.
                if not market_open(now_et()) and not args.dry and not args.force:
                    log.note("\nMarket is closed. --once would price off stale "
                             "quotes and leave a resting order for the next "
                             "open.\nUse --dry to exercise the cycle without "
                             "ordering, or --force if you meant it.")
                    return 0
                await agent.run_cycle()
            else:
                await agent.run_forever()
        except KeyboardInterrupt:
            log.note("\ninterrupted")
        finally:
            agent.state.save()
            log.finalise()
    return 0


def sync_main() -> int:
    """Console-script entry point (see pyproject [project.scripts])."""
    try:
        return asyncio.run(main())
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(sync_main())
