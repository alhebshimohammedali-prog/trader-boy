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

from src.agent import Agent
from src.logbook import Logbook
from src.mcp_client import AlpacaMCP


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comp", action="store_true", help="use the competition account")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry", action="store_true", help="never submit an order")
    ap.add_argument("--quiet", action="store_true", help="jsonl only, no table")
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
