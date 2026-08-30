"""End-to-end cycle against a fake broker. Orders are actually placed and
filled, so this exercises the one path --dry cannot reach.

Exercises layers 1-10 exactly as they run live: real gates, real signal, real
PWT allocation, real executor, real reconciliation. Only the MCP transport is
replaced.

Deliberate stressors baked into the fixture:
  - NVDA has NULL deltas          -> exercises the moneyness fallback
  - XOM and CVX have wide spreads -> should be rejected by gate 3
  - IBM is priced over the cap    -> should be rejected by gate 2 (position cap)
  - DIS reports this week         -> should be rejected by gate 8 (earnings)

    python tools/offline_cycle.py            # no network, no credentials
    python tools/offline_cycle.py --llm      # same, but the REAL model decides
    python tools/offline_cycle.py --llm --cycles 5

Default stays offline on purpose: it runs in the regression suite, where a
network dependency would make a green tick meaningless. --llm is the dress
rehearsal -- fake broker, real decisions, real fills -- which is the closest
you can get to a live session while the market is shut.
"""

from __future__ import annotations

import argparse
import asyncio
import os


import config
from src.agent import Agent
from src.logbook import Logbook

# The live universe, at roughly the levels the dry run observed. A fixture on
# tickers the agent no longer trades rehearses nothing.
SPOT = {
    "NVDA": 178.0, "IBM": 268.0, "CVX": 152.0, "PLTR": 158.0,
    "XOM": 114.0, "PEP": 143.0, "CSCO": 67.5, "DIS": 114.0,
    "INTC": 23.4, "GM": 56.2, "UBER": 74.8,
}
NULL_DELTA = {"NVDA"}
WIDE_SPREAD = {"XOM", "CVX"}


class FakeMCP:
    """Duck-types AlpacaMCP. Agent code is unmodified."""

    def __init__(self):
        self.account = "fake"
        self.resolved = {
            k: k for k in (
                "account", "positions", "orders", "order_by_client_id",
                "cancel_order", "option_chain", "option_snapshot",
                "place_option_order", "stock_bars", "stock_quote",
            )
        }
        self.available = {}
        self.orders: dict[str, dict] = {}

    def assert_paper(self):
        return None

    def tool_report(self):
        return "FakeMCP: 10 synthetic tools"

    def fit_args(self, logical, **kw):
        return {k: v for k, v in kw.items() if v is not None}, []

    async def call(self, logical, **kw):
        fn = getattr(self, f"_{logical}")
        return await fn(**kw)

    # -- reads ---------------------------------------------------------------

    async def _account(self, **_):
        return {"equity": "100000", "buying_power": "100000",
                "options_trading_level": 2, "status": "ACTIVE",
                "trading_blocked": False}

    async def _positions(self, **_):
        return []

    async def _orders(self, **_):
        return list(self.orders.values())

    async def _stock_quote(self, symbol=None, **_):
        px = SPOT.get(symbol, 100.0)
        return {"quote": {"bid_price": px - 0.02, "ask_price": px + 0.02}}

    async def _stock_bars(self, symbol=None, **_):
        base = SPOT.get(symbol, 100.0)
        # Mild deterministic wiggle so realised vol is non-zero.
        return [{"c": base * (1 + ((i * 37) % 11 - 5) / 500.0)} for i in range(40)]

    async def _option_chain(self, underlying_symbol=None, **_):
        spot = SPOT.get(underlying_symbol, 100.0)
        wide = underlying_symbol in WIDE_SPREAD
        # Vary base IV per ticker so the cross-sectional rank has something to
        # rank. A fixture where every IV is identical hides real dispersion.
        base_iv = 0.18 + (sum(map(ord, underlying_symbol)) % 17) * 0.01
        out = []
        for step in range(1, 9):
            strike = round(spot * (1 - 0.005 * step), 0)
            bid = round(max(0.05, 2.2 - 0.22 * step), 2)
            ask = round(bid + (0.40 if wide else 0.04), 2)
            delta = None if underlying_symbol in NULL_DELTA else -(0.36 - 0.035 * step)
            out.append({
                "symbol": f"{underlying_symbol}260904P{int(strike * 1000):08d}",
                "strike_price": strike,
                "expiration_date": config.TARGET_EXPIRY,
                "type": "put",
                "open_interest": 2400,
                "implied_volatility": round(base_iv + step * 0.002, 4),
                "latest_quote": {"bid_price": bid, "ask_price": ask},
                "greeks": {} if delta is None else {"delta": round(delta, 4)},
            })
        return {"option_contracts": out}

    async def _option_snapshot(self, symbol=None, **_):
        return {}

    # -- writes --------------------------------------------------------------

    async def _place_option_order(self, **kw):
        coid = kw.get("client_order_id")
        qty = int(kw.get("qty") or kw.get("quantity") or 1)
        limit = kw.get("limit_price")
        self.orders[coid] = {
            "id": f"ord_{len(self.orders) + 1}", "client_order_id": coid,
            "symbol": kw.get("symbol"), "status": "filled",
            "qty": qty, "filled_qty": qty,
            "limit_price": limit, "filled_avg_price": limit,
        }
        return self.orders[coid]

    async def _order_by_client_id(self, client_order_id=None, **_):
        o = self.orders.get(client_order_id)
        if o is None:
            raise RuntimeError("order not found")
        return o

    async def _cancel_order(self, **_):
        return {"status": "canceled"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true",
                    help="use the real model instead of the disabled stub")
    ap.add_argument("--cycles", type=int, default=3)
    args = ap.parse_args()

    if args.llm:
        from dotenv import load_dotenv

        load_dotenv(".env")
        if (os.getenv("LLM_PROVIDER") or "none").lower() == "none":
            print("--llm needs LLM_PROVIDER set in .env")
            return 1
        print(f"REAL model: {os.getenv('FEATHERLESS_MODEL') or os.getenv('ANTHROPIC_MODEL')}"
              f"   critic: {os.getenv('FEATHERLESS_CRITIC_MODEL') or '(same model)'}")
    else:
        # Pin it off rather than inherit the ambient environment. An offline
        # test that sometimes reaches the network is worse than one that never
        # does, because its green tick stops meaning anything.
        os.environ["LLM_PROVIDER"] = "none"
        print("model layer disabled (pass --llm to exercise it)")

    # Gate 8 is dormant against the live universe, so point it at a name that
    # is actually in it. An exclusion list with no overlap tests nothing.
    config.EARNINGS_EXCLUDED = set(config.EARNINGS_EXCLUDED) | {"DIS"}
    log = Logbook(run_dir="runs/offline", echo=True)
    agent = Agent(FakeMCP(), log, place_orders=True)
    agent.state.cycle = 0

    for _ in range(args.cycles):
        await agent.run_cycle()

    log.finalise()
    print(f"\nOK: {args.cycles} cycles completed end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
