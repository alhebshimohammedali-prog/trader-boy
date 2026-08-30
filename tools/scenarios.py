"""Failure-path tests through the whole agent. No credentials, no network.

selftest.py checks units. offline_cycle.py walks the happy path. Neither
exercises what happens when things go wrong, which is the only part of this
system that has to work under pressure.

Each scenario below drives the real Agent against a broker fake rigged to
produce one specific bad situation, then asserts the agent responded the way
the spec says it should.

    python tools/scenarios.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import config
from src.agent import Agent
from src.logbook import Logbook
from src.state import State

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def occ(ticker: str, strike: float) -> str:
    return f"{ticker}260904P{int(strike * 1000):08d}"


class Broker:
    """Configurable fake. Defaults to a benign market; each scenario rigs it."""

    def __init__(self, *, equity=100_000.0, positions=None, fill_status="filled",
                 spot_override=None):
        self.account = "fake"
        self.equity = equity
        self.book = positions if positions is not None else []
        self.fill_status = fill_status
        self.spot_override = spot_override or {}
        self.orders: dict[str, dict] = {}
        self.closed: list[str] = []
        self.submissions = 0
        self.resolved = {k: k for k in (
            "account", "positions", "orders", "order_by_client_id", "cancel_order",
            "option_chain", "option_snapshot", "place_option_order",
            "stock_bars", "stock_quote")}
        self.available = {}

    def assert_paper(self): return None
    def tool_report(self): return "Broker fake"
    def fit_args(self, logical, **kw): return {k: v for k, v in kw.items() if v is not None}, []

    async def call(self, logical, **kw):
        return await getattr(self, f"_{logical}")(**kw)

    async def _account(self, **_):
        return {"equity": str(self.equity), "buying_power": str(self.equity),
                "options_trading_level": 2, "status": "ACTIVE", "trading_blocked": False}

    async def _positions(self, **_):
        return list(self.book)

    async def _orders(self, **_):
        return list(self.orders.values())

    async def _stock_quote(self, symbol=None, **_):
        px = self.spot_override.get(symbol, {"AAPL": 231.0, "NVDA": 178.0,
                                             "XOM": 114.0, "IWM": 239.0}.get(symbol, 200.0))
        return {"quote": {"bid_price": px - 0.02, "ask_price": px + 0.02}}

    async def _stock_bars(self, symbol=None, **_):
        base = (await self._stock_quote(symbol=symbol))["quote"]["bid_price"]
        return [{"c": base * (1 + ((i * 37) % 11 - 5) / 500.0)} for i in range(40)]

    async def _option_chain(self, underlying_symbol=None, **_):
        spot = (await self._stock_quote(symbol=underlying_symbol))["quote"]["bid_price"]
        iv = 0.18 + (sum(map(ord, underlying_symbol)) % 17) * 0.01
        return {"option_contracts": [{
            "symbol": occ(underlying_symbol, round(spot * (1 - 0.005 * s), 0)),
            "strike_price": round(spot * (1 - 0.005 * s), 0),
            "expiration_date": config.TARGET_EXPIRY, "type": "put",
            "open_interest": 2400, "implied_volatility": round(iv + s * 0.002, 4),
            "latest_quote": {"bid_price": round(max(0.30, 2.2 - 0.22 * s), 2),
                             "ask_price": round(max(0.30, 2.2 - 0.22 * s) + 0.04, 2)},
            "greeks": {"delta": round(-(0.36 - 0.035 * s), 4)},
        } for s in range(1, 9)]}

    async def _option_snapshot(self, **_): return {}

    async def _place_option_order(self, **kw):
        self.submissions += 1
        coid, qty = kw.get("client_order_id"), int(kw.get("qty") or 1)
        if kw.get("side") == "buy":
            self.closed.append(kw.get("symbol"))
        self.orders[coid] = {
            "id": f"ord_{len(self.orders)+1}", "client_order_id": coid,
            "symbol": kw.get("symbol"), "status": self.fill_status, "qty": qty,
            "filled_qty": qty if self.fill_status == "filled" else 0,
            "limit_price": kw.get("limit_price"),
            "filled_avg_price": kw.get("limit_price") if self.fill_status == "filled" else None,
        }
        return self.orders[coid]

    async def _order_by_client_id(self, client_order_id=None, **_):
        o = self.orders.get(client_order_id)
        if o is None:
            raise RuntimeError("order not found")
        return o

    async def _cancel_order(self, **_): return {"status": "canceled"}


def short_put(ticker: str, strike: float, mv: float = -300.0, qty: float = -1.0) -> dict:
    return {"symbol": occ(ticker, strike), "qty": qty, "market_value": mv,
            "asset_class": "us_option", "underlying_symbol": ticker}


def agent_for(broker, tmp: pathlib.Path, state: State | None = None) -> Agent:
    log = Logbook(run_dir=str(tmp / "run"), echo=False)
    a = Agent(broker, log, place_orders=True)
    a.state = state or State()
    a.state.save(str(tmp / "state.json"))
    a._state_path = str(tmp / "state.json")
    return a


# ---------------------------------------------------------------------------


async def scenario_breaker(tmp):
    print("\n1. circuit breaker: 3% drawdown must halt entries AND cut exposure")
    held = [short_put("AAPL", 226.0, mv=-900.0), short_put("NVDA", 175.0, mv=-200.0)]
    b = Broker(equity=100_000.0, positions=held)
    a = agent_for(b, tmp)

    await a.run_cycle()                       # sets the high-water mark
    check("breaker not tripped at flat equity", not a.state.breaker_tripped)

    b.equity = 96_400.0                       # -3.6%
    # AAPL is nearest the money, so it is the highest-delta short.
    b.spot_override = {"AAPL": 227.0, "NVDA": 178.0}
    rec = await a.run_cycle()

    check("breaker tripped on drawdown", a.state.breaker_tripped,
          f"dd={rec.get('drawdown')}")
    check("drawdown recorded", (rec.get("drawdown") or 0) >= config.DRAWDOWN_LIMIT)
    check("exposure actually reduced", bool(b.closed), "no close order was sent")
    closed = (rec.get("breaker_action") or {}).get("closed") or ""
    check("closed the nearest-the-money short", "AAPL" in closed,
          f"closed {closed or 'nothing'}")
    check("no new entry while tripped", rec.get("no_trade_reason") is not None)


async def scenario_assignment(tmp):
    print("\n2. assignment: detected by diffing positions, never from activities")
    b = Broker(positions=[short_put("AAPL", 226.0)])
    a = agent_for(b, tmp)
    await a.run_cycle()                       # seeds the baseline

    # Put vanishes, 100 shares of the underlying appear. That is assignment.
    b.book = [{"symbol": "AAPL", "qty": 100.0, "market_value": 22_600.0,
                     "asset_class": "us_equity"}]
    rec = await a.run_cycle()

    assigned = (rec.get("reconciliation") or {}).get("assigned") or []
    check("assignment detected", "AAPL" in assigned, f"got {assigned}")
    check("reported in the cycle summary",
          "ASSIGNED" in ((rec.get("reconciliation") or {}).get("summary") or ""))


async def scenario_idempotency(tmp):
    print("\n3. crash restart: the same intent must never be sent twice")
    from src.data import Contract
    from src.execution import Executor, client_order_id

    b = Broker()
    c = Contract(symbol=occ("AAPL", 226.0), underlying="AAPL", strike=226.0,
                 expiry=config.TARGET_EXPIRY, bid=1.10, ask=1.16,
                 open_interest=2400, delta=-0.18)

    # First submission, from a process that then "crashes".
    f1 = await Executor(b).sell_put(c, cycle=7)
    check("order submitted", b.submissions == 1 and f1.filled_qty == 1)
    check("client id is derived from cycle + contract",
          f1.client_order_id == client_order_id(7, c))

    # Restart: brand-new Executor, no memory, same cycle and contract. It must
    # ask the broker whether this intent already exists before sending.
    f2 = await Executor(b).sell_put(c, cycle=7)
    check("no duplicate submission after restart", b.submissions == 1,
          f"submissions went 1 -> {b.submissions}")
    check("recognised the pre-existing order", "pre-existing" in f2.note,
          f"note={f2.note!r}")

    # A different cycle is a different intent and SHOULD send.
    await Executor(b).sell_put(c, cycle=8)
    check("a genuinely new intent still sends", b.submissions == 2,
          f"submissions={b.submissions}")


async def scenario_unparseable(tmp):
    print("\n4. unparseable position symbol must fail closed, not mis-cap")
    b = Broker(positions=[{"symbol": "N0TAN0CCSYM", "qty": -1.0,
                           "market_value": -400.0, "asset_class": "us_option"}])
    a = agent_for(b, tmp)
    rec = await a.run_cycle()

    check("symbol flagged", bool(rec.get("unparsed_positions")))
    check("breaker tripped rather than deploy blind", a.state.breaker_tripped)
    check("no entry made", rec.get("no_trade_reason") is not None)


async def scenario_nofill(tmp):
    print("\n5. order that never fills: reprice, then stop cleanly")
    b = Broker(fill_status="new")             # accepted, never fills
    a = agent_for(b, tmp)
    rec = await a.run_cycle()

    fill = rec.get("fill") or {}
    check("recorded as unfilled", (fill.get("filled_qty") or 0) == 0)
    check("repriced before giving up", (fill.get("attempts") or 0) > 1,
          f"attempts={fill.get('attempts')}")
    check("capital not charged for a non-fill",
          a.state.ubt(fill.get("symbol", "")[:4]) == 0.0)
    check("cycle completed without raising", rec.get("cycle") is not None)


async def main() -> int:
    config.ORDER_TIMEOUT_SECONDS = 2          # keep the no-fill path quick
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        for fn in (scenario_breaker, scenario_assignment, scenario_idempotency,
                   scenario_unparseable, scenario_nofill):
            await fn(tmp)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all failure paths behaved correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
