"""Pick the universe from the live market instead of a list written days ago.

    python tools/scan.py                 # show the ranking, change nothing
    python tools/scan.py --write         # also write universe.json
    python tools/scan.py --top 12 --pool 60

Deliberately a PRE-FLIGHT tool, not a runtime step. It writes universe.json;
config reads that file when it exists and falls back to the hardcoded list when
it does not. So a scanner that fails, hangs, or returns nonsense cannot stop
the agent from trading -- the worst case is yesterday's universe, which is
exactly what we had before this existed.

Running it inside the cycle would be the opposite trade: a data-source problem
at 09:30 would take the whole session down, to save an operator one command.

The funnel, cheapest filter first:

  most-active names        liquidity is the precondition for everything else
  -> batch quote           one call for the lot; price bounds are nearly free
  -> collateral filter     strike x 100 must fit PER_POSITION_CAP x equity,
                           which alone removes most of the market by price
  -> fetch 4 Sep chains    the only expensive step, and only for survivors
  -> rank by yield         the same expected_yield the allocator scores on

Ranking on the allocator's own metric is the point. A universe chosen by one
measure and allocated by another would be two policies arguing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from src.agent import pick_contract  # noqa: E402
from src.allocation import expected_yield  # noqa: E402
from src import gates  # noqa: E402
from src.agent import now_et  # noqa: E402
from src.data import Data, by_symbol  # noqa: E402
from src.mcp_client import AlpacaMCP  # noqa: E402

# Below this the premium is rounding error against the spread; above it the
# collateral filter does the work. Both bounds are about tradeability, not a
# view on the companies.
MIN_PRICE = 12.0

# Ranking on yield ranks VOLATILITY, because that is what pays premium. Run
# unfiltered against the live most-active list, this scan put SOXL, SOXS, TQQQ
# and SQQQ in the top ten -- 3x leveraged ETFs, including a long/short pair on
# the same underlying, which it wanted to sell puts on simultaneously.
#
# A 3x fund can gap 25% on a normal day for the index it tracks. Against a
# 2%-OTM put that is not a tail risk, it is a Tuesday, and no delta adjustment
# in expected_yield prices it, since delta describes the contract and not the
# leverage of the thing underneath. The premium is high because the risk is
# real, and a yield ranking reads that as opportunity.
#
# So the scan is deliberately not free to pick anything the market offers.
LEVERAGED = {
    "SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS", "TNA", "TZA", "LABU", "LABD",
    "FAS", "FAZ", "YINN", "YANG", "NUGT", "DUST", "JNUG", "JDST", "UVXY", "SVXY",
    "VIXY", "UPRO", "SPXU", "UDOW", "SDOW", "TMF", "TMV", "BOIL", "KOLD", "ERX",
    "ERY", "DRIP", "GUSH", "WEBL", "WEBS", "TECL", "TECS", "CURE", "NAIL", "MSTU",
    "MSTZ", "TSLL", "TSLQ", "NVDL", "NVD", "CONL", "AMDL", "BITX", "ETHU",
}

# Single-asset crypto vehicles. Same objection: weekend gaps the option market
# cannot hedge, and volatility that is structural rather than event-driven.
CRYPTO = {"IBIT", "ETHA", "FBTC", "GBTC", "ARKB", "BITB", "ETHE", "BITO", "BTF"}


# Alpaca's screener rejects top > 100 with a 400. Asking for more does not
# degrade gracefully: the request fails, and a naive retry without the
# parameter comes back with the ten-name default, which looks like a thin
# market rather than a bad argument.
SCREENER_MAX = 100


def _symbols(payload) -> list[str]:
    """Pull tickers out of whatever shape the screener returns."""
    found: list[str] = []
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in ("symbol", "S") and isinstance(v, str):
                    found.append(v)
                else:
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


async def build_pool(mcp, n: int) -> list[str]:
    """Union the screener endpoints, most-liquid first.

    Most-active is the primary source because liquidity is the precondition
    for everything downstream -- a name with no option volume fails the spread
    gate no matter how attractive it looks. Movers are unioned in for breadth,
    after, so ordering still favours liquidity.
    """
    out: list[str] = []

    def add(syms):
        for s in syms:
            if s.isalpha() and 1 <= len(s) <= 5 and s not in out:
                out.append(s)

    for op, kw in (("most_active", {"top": min(n, SCREENER_MAX)}),
                   ("movers", {"top": min(n, SCREENER_MAX)}),
                   ("movers", {})):
        if op not in mcp.resolved or len(out) >= n:
            continue
        try:
            add(_symbols(await mcp.call(op, **kw)))
        except Exception as exc:  # noqa: BLE001
            print(f"  {op} unavailable: {str(exc)[:90]}")
    return out[:n]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=50, help="most-active names to consider")
    ap.add_argument("--top", type=int, default=11, help="size of the resulting universe")
    ap.add_argument("--write", action="store_true", help="write universe.json")
    ap.add_argument("--comp", action="store_true")
    args = ap.parse_args()

    load_dotenv(".env")
    async with AlpacaMCP(account="comp" if args.comp else None) as mcp:
        acct = await mcp.call("account")
        equity = float((acct or {}).get("equity") or 100_000.0)
        cap = config.PER_POSITION_CAP * equity
        max_strike = cap / 100.0
        print(f"equity ${equity:,.0f}   position cap ${cap:,.0f}   "
              f"=> max strike ${max_strike:,.2f}\n")

        pool = await build_pool(mcp, args.pool)
        if len(pool) < config.UNIVERSE_MIN_NAMES:
            pool = list(dict.fromkeys(pool + list(config.UNIVERSE_CANDIDATES)))
            print("screener returned too little; topping up from the configured list")
        print(f"pool: {len(pool)} names")

        # One call for the lot. This is what makes scanning cheap enough to be
        # worth doing at all.
        spots: dict[str, float] = {}
        for i in range(0, len(pool), 25):
            chunk = pool[i:i + 25]
            try:
                q = await mcp.call("stock_quote", symbols=",".join(chunk))
            except Exception as exc:  # noqa: BLE001
                print(f"  quote batch failed: {str(exc)[:120]}")
                continue
            for sym in chunk:
                row = by_symbol(q, "quotes", sym) or {}
                bid = row.get("bp") or row.get("bid_price")
                ask = row.get("ap") or row.get("ask_price")
                px = next((float(v) for v in (ask, bid) if v), 0.0)
                if px > 0:
                    spots[sym] = px

        priced = {s: px for s, px in spots.items() if MIN_PRICE <= px <= max_strike}
        excluded = sorted(set(priced) & (LEVERAGED | CRYPTO))
        affordable = [s for s in priced if s not in LEVERAGED and s not in CRYPTO]
        print(f"priced: {len(spots)}   affordable (${MIN_PRICE:.0f}-"
              f"${max_strike:,.0f}): {len(priced)}")
        if excluded:
            print(f"excluded as leveraged/crypto: {excluded}")
        print()

        from datetime import date

        y, m, dd = (int(x) for x in config.TARGET_EXPIRY.split("-"))
        days_to_expiry = max((date(y, m, dd) - date.today()).days, 1)

        data = Data(mcp)
        rows = []
        rejected: list[tuple[str, str]] = []
        for sym in affordable:
            try:
                chain = await data.put_chain(sym, config.TARGET_EXPIRY)
            except Exception:  # noqa: BLE001
                continue
            if not chain:
                continue
            contract, why = pick_contract(chain, spots.get(sym))
            if contract is None:
                rejected.append((sym, why))
                continue

            # Screen with the REAL gates rather than a lookalike. Ranking on
            # yield alone selected names whose target contract the agent would
            # reject on sight -- PATH quoted 0.15 wide on a 0.22 bid, a 51%
            # spread against a 20% limit, and it ranked second. A universe of
            # contracts that fail gate 3 is a zero-trade week wearing the
            # costume of a market scan.
            g = gates.evaluate(
                contract, now=now_et(), equity=equity, spot=spots[sym],
                deployed_collateral=0.0, breaker_tripped=False, held_symbols=set())
            if not g.passed:
                rejected.append((sym, g.reason or "gate"))
                continue
            d = days_to_expiry
            rows.append({
                "ticker": sym,
                "spot": round(spots[sym], 2),
                "strike": contract.strike,
                "collateral": round(contract.collateral, 0),
                "pct_equity": contract.collateral / equity,
                "bid": contract.bid,
                "spread": round((contract.ask or 0) - (contract.bid or 0), 3),
                "oi": contract.open_interest,
                "delta": contract.delta,
                "yield": expected_yield(contract, d),
                "why": why,
            })

        rows.sort(key=lambda r: r["yield"], reverse=True)

        print(f"{'ticker':7s} {'spot':>8s} {'strike':>8s} {'collat':>9s} {'%eq':>6s} "
              f"{'bid':>6s} {'sprd':>6s} {'OI':>7s} {'delta':>7s} {'yield':>9s}")
        for r in rows:
            print(f"{r['ticker']:7s} {r['spot']:>8.2f} {r['strike']:>8.2f} "
                  f"{r['collateral']:>9,.0f} {r['pct_equity']:>5.1%} "
                  f"{(r['bid'] or 0):>6.2f} {r['spread']:>6.2f} "
                  f"{(r['oi'] if r['oi'] is not None else -1):>7d} "
                  f"{(r['delta'] if r['delta'] is not None else 0):>7.3f} "
                  f"{r['yield']:>9.6f}")

        if rejected:
            print(f"\nrejected ({len(rejected)}):")
            for sym, why in rejected[:14]:
                print(f"  x {sym:6s} {str(why)[:84]}")

        chosen = [r["ticker"] for r in rows[:args.top]]
        print(f"\ntop {len(chosen)}: {chosen}")

        if len(chosen) < config.UNIVERSE_MIN_NAMES:
            print(f"\nonly {len(chosen)} names, below UNIVERSE_MIN_NAMES="
                  f"{config.UNIVERSE_MIN_NAMES}. PWT arbitrates nothing with a "
                  "universe this small; not writing.")
            return 1

        print("\nEARNINGS ARE NOT CHECKED HERE. Alpaca exposes no earnings "
              "calendar through this server, so confirm by hand that none of "
              f"these reports before {config.MARK_AT:%d %b} and add any that "
              "do to config.EARNINGS_EXCLUDED.")

        if args.write:
            out = Path("universe.json")
            out.write_text(json.dumps({
                "generated": config.now_et().isoformat() if hasattr(config, "now_et")
                else None,
                "expiry": config.TARGET_EXPIRY,
                "equity": equity,
                "universe": chosen,
                "detail": rows[:args.top],
            }, indent=2, default=str), encoding="utf-8")
            print(f"\nwrote {out} -- config picks this up on next start")
        else:
            print("\n(--write to persist; nothing changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
