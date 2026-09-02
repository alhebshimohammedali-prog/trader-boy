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

    async def _stock_quote(self, symbols=None, symbol=None, **_):
        """Data.spot calls this with `symbols=` and reads it through
        by_symbol(raw, "quotes", ticker).

        Both of those were wrong here: the parameter was `symbol=` (so the
        override was never found and every name priced at the 200.0 default),
        and the payload was a bare {"quote": ...} with no per-symbol container
        (so by_symbol returned None and Data.spot returned None for EVERY
        ticker, always).

        The effect was invisible because nothing failed: the breaker scenario
        below still passed, having silently fallen through _close_largest's
        moneyness ranking to its market_value fallback. A fake that cannot
        reproduce the real payload shape does not test the primary path, it
        tests the fallback and reports the primary path's name.
        """
        t = symbols or symbol
        px = self.spot_override.get(t, {"AAPL": 231.0, "NVDA": 178.0,
                                        "XOM": 114.0, "IWM": 239.0}.get(t, 200.0))
        return {"quotes": {t: {"bid_price": px - 0.02, "ask_price": px + 0.02}}}

    def _px(self, ticker: str) -> float:
        return self.spot_override.get(ticker, {"AAPL": 231.0, "NVDA": 178.0,
                                               "XOM": 114.0, "IWM": 239.0}.get(ticker, 200.0))

    async def _stock_bars(self, symbol=None, symbols=None, **_):
        base = self._px(symbols or symbol) - 0.02
        return [{"c": base * (1 + ((i * 37) % 11 - 5) / 500.0)} for i in range(40)]

    async def _option_chain(self, underlying_symbol=None, **_):
        spot = self._px(underlying_symbol) - 0.02
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
            # A market close carries no limit_price, and returning None for
            # filled_avg_price meant the ledger could never compute a realised
            # P&L from a fake fill -- so the MEASURED path went untested while
            # the suite reported green. Same fidelity gap as the quote payload.
            "filled_avg_price": (kw.get("limit_price") or "1.00")
            if self.fill_status == "filled" else None,
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
    action = rec.get("breaker_action") or {}
    closed = action.get("closed") or ""
    check("closed the nearest-the-money short", "AAPL" in closed,
          f"closed {closed or 'nothing'}")
    # Pin the RANKING BASIS, not just the outcome. _close_largest falls back to
    # market_value when spot is unavailable, and for years this scenario passed
    # on that fallback because the fake's stock_quote never returned a usable
    # quote -- so the moneyness path the test is named after was never run. The
    # two happened to pick the same position here, which is precisely why the
    # gap was invisible.
    check("ranked by moneyness, not the market_value fallback",
          action.get("ranked_by") == "moneyness",
          f"ranked_by={action.get('ranked_by')}")
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


async def scenario_exits(tmp):
    print("\n6. exits: a position through its strike must close ITSELF")
    # The gap this covers. is_itm lived only in gates.py, the ENTRY gate, so it
    # chose what we could open and never revisited what we owned. The only
    # automatic close was _close_largest, reachable solely from the 3% breaker
    # -- so a single position could go deep ITM while the account sat at -1%
    # and nothing in the agent would act, or even say so.
    # Far-dated expiry ON PURPOSE. short_put() stamps the 4 Sep expiry, which
    # makes DTE -- and therefore whether the ITM signal counts as urgent --
    # depend on the day the suite happens to run. A test whose branch changes
    # with the wall clock is not a test. This one pins the non-urgent path, so
    # it must take the confirmation route on every date.
    far = "261204"
    # Cheap to leave: spot 220 vs strike 226 -> 600 intrinsic, mark 620, so only
    # 20 of time value is surrendered against a 100 credit. Closing here is the
    # exit doing its job. The fixture below it is the opposite case.
    held = [{"symbol": f"AAPL{far}P00226000", "qty": -1.0, "market_value": -620.0,
             "unrealized_pl": 100.0 - 620.0, "asset_class": "us_option",
             "underlying_symbol": "AAPL"},
            {"symbol": f"NVDA{far}P00175000", "qty": -1.0, "market_value": -40.0,
             "unrealized_pl": 60.0, "asset_class": "us_option",
             "underlying_symbol": "NVDA"}]
    b = Broker(equity=100_000.0, positions=held,
               spot_override={"AAPL": 220.0, "NVDA": 178.0})  # AAPL is through 226
    a = agent_for(b, tmp)

    rec = await a.run_cycle()
    rows = rec.get("exits") or []
    aapl = next((r for r in rows if r["ticker"] == "AAPL"), None)
    check("the ITM position was identified", aapl is not None, f"exits={rows}")
    check("identified by the ITM rule", aapl and aapl["rule"] == "itm")
    check("not closed on a single sighting", not b.closed, f"closed={b.closed}")

    rec = await a.run_cycle()
    check("closed without a breaker and without a human",
          any("AAPL" in s for s in b.closed) and not a.state.breaker_tripped,
          f"closed={b.closed} breaker={a.state.breaker_tripped}")
    check("the healthy position was left alone",
          not any("NVDA" in s for s in b.closed))
    check("entries stand down for the cycle after a close",
          rec.get("no_trade_reason") is not None)

    # No model exists on this fake, and the close still happened. Entries fail
    # closed on a model timeout because the safe default when OPENING risk is
    # don't; exits invert that, so this path deliberately has no model in it.
    check("exit needs no model in the path", any("AAPL" in s for s in b.closed))


async def scenario_exit_price(tmp):
    print("\n8. exits: never pay away more time value than the trade earned")
    # The PLTR trade, as it actually happened. Sold for 0.94, marked at 6.55,
    # of which 4.76 is intrinsic owed anyway on a cash-secured put and 1.79 is
    # time value. The first version of this layer called it an emergency and
    # surrendered the 1.79 to exit a position that had collected 0.94.
    far = "261204"
    held = [{"symbol": f"PLTR{far}P00175000", "qty": -1.0, "market_value": -655.0,
             "unrealized_pl": 94.0 - 655.0, "asset_class": "us_option",
             "underlying_symbol": "PLTR"}]
    b = Broker(equity=100_000.0, positions=held, spot_override={"PLTR": 170.24})
    a = agent_for(b, tmp)
    for _ in range(3):                      # well past the confirmation window
        rec = await a.run_cycle()
    check("deep ITM but expensive to leave is never closed", not b.closed,
          f"closed={b.closed}")
    check("and it is not even flagged as an exit candidate",
          not (rec.get("exits") or []), f"exits={rec.get('exits')}")


async def scenario_exit_confirmation(tmp):
    print("\n7. exits: one bad print must never close a good position")
    held = [short_put("AAPL", 226.0, mv=-250.0)]
    held[0]["unrealized_pl"] = -150.0          # 250 to close vs 100 credit = 2.5x
    b = Broker(equity=100_000.0, positions=held, spot_override={"AAPL": 400.0})
    a = agent_for(b, tmp)

    rec = await a.run_cycle()
    rows = rec.get("exits") or []
    check("first sighting only watches",
          rows and rows[0]["action"] == "watching" and not b.closed,
          f"rows={rows} closed={b.closed}")

    # Recovers -- a single bad print, not a trend. The streak must reset.
    held[0]["market_value"], held[0]["unrealized_pl"] = -40.0, 60.0
    await a.run_cycle()
    check("a clean cycle clears the streak", not a.state.exit_streak)

    # Deteriorates again, but this is cycle 1 of a NEW streak: still no close.
    held[0]["market_value"], held[0]["unrealized_pl"] = -250.0, -150.0
    rec = await a.run_cycle()
    check("an isolated re-trigger still does not close", not b.closed,
          f"closed={b.closed}")

    # Two consecutive now, so it acts.
    rec = await a.run_cycle()
    check("two consecutive cycles do close",
          any("AAPL" in s for s in b.closed), f"closed={b.closed}")


async def main() -> int:
    config.ORDER_TIMEOUT_SECONDS = 2
    config.LEDGER_PATH = str(pathlib.Path(tempfile.gettempdir()) / "aw_test_ledger.jsonl")          # keep the no-fill path quick
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        for fn in (scenario_breaker, scenario_assignment, scenario_idempotency,
                   scenario_unparseable, scenario_nofill, scenario_exits,
                   scenario_exit_confirmation, scenario_exit_price):
            await fn(tmp)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all failure paths behaved correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
