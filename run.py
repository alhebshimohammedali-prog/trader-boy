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
from src.agent import Agent, now_et
from src.logbook import Logbook
from src.mcp_client import AlpacaMCP


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

    if len(chosen) < config.UNIVERSE_MIN_NAMES:
        log.note(f"  scan produced {len(chosen)} tradeable names, below "
                 f"UNIVERSE_MIN_NAMES={config.UNIVERSE_MIN_NAMES}. Keeping the "
                 f"configured universe.")
        log.note(f"universe ({len(config.UNIVERSE_CANDIDATES)}, configured): "
                 f"{', '.join(config.UNIVERSE_CANDIDATES)}")
        return

    config.UNIVERSE_CANDIDATES = chosen
    log.note(f"  {len(rejected)} rejected by the gates before ranking")
    log.note(f"universe ({len(chosen)}, scanned): {', '.join(chosen)}")

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

        if config.AUTO_SCAN and not args.no_scan:
            await _scan_universe(mcp, log)
        else:
            log.note(f"universe ({len(config.UNIVERSE_CANDIDATES)}): "
                     f"{', '.join(config.UNIVERSE_CANDIDATES)}")

        agent = Agent(mcp, log, place_orders=not args.dry)
        try:
            if args.flatten:
                await agent.flatten()
            elif args.once:
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
