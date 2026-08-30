"""Dry-run steps 1-2 (§10): prove the MCP wiring, then count null Greeks.

    python tools/probe.py            # dev account (per .env)
    python tools/probe.py --comp     # competition account

Answers, before market open:
  - does alpaca-mcp-server start and expose the tools we need?
  - what are the V2 tool names actually called?
  - is options trading enabled, and what is buying power?
  - how often does Alpaca return null delta on the 4 Sep chain?

That last number decides whether the §3 moneyness fallback is the primary
path or the exception, which is not something to discover at 09:31 Monday.
"""

from __future__ import annotations

import asyncio
import os
import sys


from dotenv import load_dotenv

import config
from src.data import Data
from src.mcp_client import AlpacaMCP


def _num(d: dict, *names, default=None):
    """Field names vary across MCP versions; try several."""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            try:
                return float(d[n])
            except (TypeError, ValueError):
                return d[n]
    return default


async def main() -> int:
    load_dotenv()
    account = "comp" if "--comp" in sys.argv else os.getenv("ALPACA_ACCOUNT", "dev")

    print(f"=== probe: {account.upper()} account ===\n")
    if account == "comp":
        print("!! COMPETITION ACCOUNT. Must be brand new, funded at exactly")
        print("!! $100,000, and untraded before Mon 31 Aug 09:30 ET.\n")

    async with AlpacaMCP(account=account) as mcp:
        print(mcp.tool_report(), "\n")

        acct = await mcp.call("account")
        if isinstance(acct, dict):
            equity = _num(acct, "equity", "portfolio_value")
            bp = _num(acct, "buying_power", "options_buying_power")
            lvl = acct.get("options_trading_level", acct.get("options_approved_level"))
            print(f"equity           : {equity}")
            print(f"buying power     : {bp}")
            print(f"options level    : {lvl}")
            print(f"status           : {acct.get('status')}")
            print(f"trading blocked  : {acct.get('trading_blocked')}")
            if lvl is not None and str(lvl).isdigit() and int(lvl) < 1:
                print("\n!! options level < 1 -- cannot sell cash-secured puts.")
        else:
            print(f"account (raw)    : {str(acct)[:400]}")

        positions = await mcp.call("positions")
        n_pos = len(positions) if isinstance(positions, list) else "?"
        print(f"open positions   : {n_pos}")

        # --- null-Greek census on the target expiry -------------------------
        print(f"\n=== {config.TARGET_EXPIRY} chain: null-Greek census ===")
        total = nulls = 0
        for ticker in config.UNIVERSE_CANDIDATES:
            try:
                chain = await mcp.call(
                    "option_chain",
                    underlying_symbol=ticker,
                    expiration_date=config.TARGET_EXPIRY,
                    type="put",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {ticker:6s} chain error: {str(exc)[:110]}")
                continue

            # Use the same parser the agent uses; a second inline copy just
            # rots and reports "no contracts" while the data is sitting there.
            contracts = Data._chain_rows(chain)
            if not contracts:
                print(f"  {ticker:6s} no contracts returned")
                continue

            n_null = sum(
                1
                for c in contracts
                if isinstance(c, dict)
                and (c.get("greeks") or {}).get("delta") in (None, "")
            )
            total += len(contracts)
            nulls += n_null
            print(f"  {ticker:6s} {len(contracts):4d} puts, {n_null:4d} null delta")

        if total:
            pct = 100 * nulls / total
            print(f"\n  overall: {nulls}/{total} null delta ({pct:.1f}%)")
            if pct > 50:
                print("  -> moneyness fallback is the PRIMARY strike path, not the exception.")

        # --- §2 collateral table: which names are tradeable at 1 contract ---
        equity = _num(acct, "equity", "portfolio_value", default=100000.0) or 100000.0
        cap = config.PER_POSITION_CAP * equity
        print(f"\n=== §2 collateral check (cap {config.PER_POSITION_CAP:.0%} "
              f"of ${equity:,.0f} = ${cap:,.0f}) ===")
        print(f"  {'ticker':7s} {'spot':>9s} {'target K':>9s} {'collateral':>12s}  ok?")

        tradeable = []
        for ticker in config.UNIVERSE_CANDIDATES:
            spot = await Data(mcp).spot(ticker)
            if not spot:
                print(f"  {ticker:7s} {'no quote':>9s}")
                continue
            # Must match what agent.pick_contract actually targets, or this
            # table reports strikes the agent will never choose.
            target = spot * (1 - config.MONEYNESS_TARGET)
            collateral = round(target) * 100
            ok = collateral <= cap
            if ok:
                tradeable.append(ticker)
            print(f"  {ticker:7s} {spot:9.2f} {round(target):9.0f} "
                  f"{collateral:12,.0f}  {'yes' if ok else 'NO'}")

        print(f"\n  tradeable at 1 contract: {len(tradeable)}/"
              f"{len(config.UNIVERSE_CANDIDATES)} -> {', '.join(tradeable)}")
        if len(tradeable) < config.UNIVERSE_MIN_NAMES:
            print(f"  !! below UNIVERSE_MIN_NAMES={config.UNIVERSE_MIN_NAMES}: capital is")
            print("  !! never scarce, candidates never queue, and the allocation layer")
            print("  !! arbitrates nothing. Raise PER_POSITION_CAP or add names.")

    print("\nprobe complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
