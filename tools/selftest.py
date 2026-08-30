"""Regression tests. No pytest, no network, no credentials.

    python tools/selftest.py

Written because the config is going to be tweaked live between sessions, and
every knob in there is one edit away from silently disarming a risk gate. Each
test below corresponds to a failure that would not raise an exception -- it
would just quietly do the wrong thing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


import config
from src import gates, signal as sig
from src.allocation import capital_time, select
from src.data import Contract, parse_occ
from src.decide import coerce, parse_verdict
from src.execution import client_order_id, sell_limit_price
from src.state import State

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def contract(ticker="AAPL", strike=226.0, bid=1.10, ask=1.16, oi=2500,
             delta=-0.18, expiry=None) -> Contract:
    return Contract(
        symbol=f"{ticker}260904P{int(strike * 1000):08d}",
        underlying=ticker, strike=strike,
        expiry=expiry or config.TARGET_EXPIRY,
        bid=bid, ask=ask, open_interest=oi, delta=delta,
    )


# --------------------------------------------------------------- OCC ------

def test_occ():
    print("\nOCC symbol parsing (a wrong parse silently disables gate 6)")
    o = parse_occ("AAPL260904P00226000")
    check("valid symbol parses", o is not None)
    check("strike correct", o and o.strike == 226.0, f"got {o.strike if o else None}")
    check("collateral = strike x 100", o and o.collateral == 22600.0)
    check("root correct", o and o.root == "AAPL")
    check("expiry correct", o and o.expiry == "2026-09-04", f"got {o.expiry if o else None}")
    check("right correct", o and o.right == "P")

    check("garbage rejected", parse_occ("NOTASYMBOL") is None)
    check("empty rejected", parse_occ("") is None)
    check("too short rejected", parse_occ("A260904P001") is None)
    check("bad right rejected", parse_occ("AAPL260904X00226000") is None)
    check("non-numeric strike rejected", parse_occ("AAPL260904Pabcdefgh") is None)
    check("zero strike rejected", parse_occ("AAPL260904P00000000") is None)
    check("bad month rejected", parse_occ("AAPL261304P00226000") is None)
    check("SPY (3-char root) parses", (parse_occ("SPY260904P00640000") or None) is not None)


# ------------------------------------------------------- chain shape ------

def test_chain():
    print("\noption chain parsing (wrong shape = zero candidates all week)")
    import asyncio
    from src.data import Data

    # The shape the REST option-chain endpoint actually returns: a dict keyed
    # by contract symbol, camelCase quote keys, no strike/expiry/OI fields.
    snap = {"snapshots": {
        "AAPL260904P00226000": {
            "latestQuote": {"bp": 1.12, "ap": 1.20},
            "greeks": {"delta": -0.184}, "impliedVolatility": 0.337},
        "AAPL260904C00240000": {
            "latestQuote": {"bp": 0.80, "ap": 0.88},
            "greeks": {"delta": 0.21}},
    }, "next_page_token": "abc"}

    class S:
        async def call(self, *a, **k): return snap

    got = asyncio.run(Data(S()).put_chain("AAPL", "2026-09-04"))
    check("snapshot dict shape yields contracts", len(got) == 1, f"got {len(got)}")
    if got:
        c = got[0]
        check("calls filtered out", c.symbol.endswith("P00226000"))
        check("strike recovered from OCC symbol", c.strike == 226.0, f"got {c.strike}")
        check("expiry recovered from OCC symbol", c.expiry == "2026-09-04", c.expiry)
        check("camelCase bid/ask read", c.bid == 1.12 and c.ask == 1.20)
        check("camelCase impliedVolatility read", c.iv == 0.337)
        check("unreported OI is None, not 0", c.open_interest is None)

    legacy = {"option_contracts": [{
        "symbol": "XOM260904P00110000", "strike_price": 110.0, "type": "put",
        "open_interest": 2400, "expiration_date": "2026-09-04",
        "latest_quote": {"bid_price": 0.40, "ask_price": 0.44},
        "greeks": {"delta": -0.19}, "implied_volatility": 0.25}]}

    class S2:
        async def call(self, *a, **k): return legacy

    got2 = asyncio.run(Data(S2()).put_chain("XOM", "2026-09-04"))
    check("legacy list shape still parses", len(got2) == 1)
    check("reported OI preserved", got2 and got2[0].open_interest == 2400)

    # Unreported OI must not fail the liquidity gate.
    now = datetime(2026, 8, 31, 11, 0, tzinfo=config.ET)
    c = contract(oi=None)
    g = gates.evaluate(c, now=now, equity=100_000.0, spot=230.0,
                       deployed_collateral=0.0, breaker_tripped=False,
                       held_symbols=set())
    check("unreported OI does not fail gate 3", g.passed, g.reason)
    check("but it is recorded as unevaluated",
          any("open interest" in n for n in g.notes), f"notes={g.notes}")


# ------------------------------------------------------------ signal ------

def test_signal():
    print("\nsignal ranking (ties at the floor = zero trades all week)")
    check("all-tied pool ranks mid", sig.rank_within(0.24, [0.24] * 6) == 0.5)
    check("highest of pool ranks high", sig.rank_within(0.30, [0.10, 0.20, 0.30]) > 0.6)
    check("lowest of pool ranks low", sig.rank_within(0.10, [0.10, 0.20, 0.30]) < 0.4)
    check("empty pool is neutral", sig.percentile_of(0.2, []) == 0.5)
    check("single peer is neutral", sig.rank_within(0.2, [0.2]) == 0.5)

    bars = [{"c": 100.0 + (i % 3)} for i in range(30)]
    check("realised vol computes", (sig.realized_vol(bars) or 0) > 0)
    check("realised vol needs data", sig.realized_vol([{"c": 100.0}]) is None)
    check("momentum sign correct",
          sig.momentum([{"c": 100.0}] * 5 + [{"c": 110.0}], 5) > 0)


# -------------------------------------------------------- allocation ------

def test_allocation():
    print("\nallocation (opbt must EXCLUDE self, or size bias inverts)")
    st = State()
    st.begin_cycle(datetime.now(), 100_000.0)
    small, big = contract("NVDA", 175.0), contract("IWM", 235.0)
    for c in (small, big):
        st.mark_qualified(c.underlying)

    winner, scored = select([(small, 0.5), (big, 0.9)], st, 100_000.0, date(2026, 8, 31))
    check("smaller position wins on capital efficiency",
          winner.contract.underlying == "NVDA", f"got {winner.contract.underlying}")

    by = {s.contract.underlying: s for s in scored}
    check("larger position has SMALLER opbt",
          by["IWM"].opbt < by["NVDA"].opbt,
          f"IWM {by['IWM'].opbt:.3f} vs NVDA {by['NVDA'].opbt:.3f}")
    check("signal does not drive selection (admission only)",
          winner.signal < max(s.signal for s in scored))

    d = 4
    check("capital_time = collateral/equity x dte",
          abs(capital_time(small, 100_000.0, d) - (17500 / 100_000 * d)) < 1e-9)

    st.charge_capital("NVDA", small.collateral, 100_000.0, d)
    check("ubt accrues after winning", st.ubt("NVDA") > 0)
    w2, _ = select([(small, 0.5), (big, 0.9)], st, 100_000.0, date(2026, 8, 31))
    check("winner rotates after paying ubt", w2.contract.underlying == "IWM",
          f"got {w2.contract.underlying}")


# ------------------------------------------------------------- gates ------

def test_gates():
    print("\nrisk gates (each must reject; a passing gate is an unenforced limit)")
    now = datetime(2026, 8, 31, 11, 0, tzinfo=config.ET)
    base = dict(now=now, equity=100_000.0, spot=230.0, deployed_collateral=0.0,
                breaker_tripped=False, held_symbols=set())

    check("clean candidate passes", gates.evaluate(contract(), **base).passed)

    g = gates.evaluate(contract(strike=600.0), **base)
    check("g2 rejects oversized collateral", not g.passed and "position_cap" in g.reason)

    g = gates.evaluate(contract(oi=5), **base)
    check("g3 rejects thin open interest", not g.passed and "liquidity" in g.reason)

    g = gates.evaluate(contract(bid=0.10, ask=0.14), **base)
    check("g3 rejects sub-minimum credit", not g.passed and "min_credit" in g.reason)

    g = gates.evaluate(contract(bid=1.00, ask=1.40), **base)
    check("g3 rejects wide spread", not g.passed and "liquidity" in g.reason)

    g = gates.evaluate(contract(strike=240.0), **base)  # spot 230 -> ITM
    check("g4 rejects ITM", not g.passed and "expiry_containment" in g.reason)

    g = gates.evaluate(contract(expiry="2026-09-11"), **base)
    check("g4 rejects wrong expiry", not g.passed and "expiry_containment" in g.reason)

    g = gates.evaluate(contract(), **{**base, "breaker_tripped": True})
    check("g5 rejects when breaker tripped", not g.passed and "circuit_breaker" in g.reason)

    g = gates.evaluate(contract(), **{**base, "deployed_collateral": 58_000.0})
    check("g6 rejects portfolio over-deployment",
          not g.passed and "portfolio_cap" in g.reason)

    blackout = config.MACRO_EVENTS[0][0]
    g = gates.evaluate(contract(), **{**base, "now": blackout})
    check("g7 rejects inside macro blackout", not g.passed and "macro_blackout" in g.reason)

    g = gates.evaluate(contract("AVGO", 300.0), **base)
    saved = config.EARNINGS_EXCLUDED
    config.EARNINGS_EXCLUDED = set(saved) | {"AVGO"}
    g = gates.evaluate(contract("AVGO", 200.0), **base)
    config.EARNINGS_EXCLUDED = saved
    check("g8 rejects earnings names", not g.passed and "earnings" in g.reason)

    late = config.ENTRY_CUTOFF + timedelta(hours=1)
    g = gates.evaluate(contract(), **{**base, "now": late})
    check("entry cutoff enforced", not g.passed and "entry_window" in g.reason)

    c = contract()
    g = gates.evaluate(c, **{**base, "held_symbols": {c.symbol}})
    check("duplicate position rejected", not g.passed)


# --------------------------------------------------------- execution ------

def test_execution():
    print("\nexecution (mid never fills on paper -- must price at/through bid)")
    c = contract(bid=1.10, ask=1.16)
    check("limit is the bid, not the mid", sell_limit_price(c) == 1.10,
          f"got {sell_limit_price(c)} (mid would be {c.mid})")
    check("limit is below mid", sell_limit_price(c) < c.mid)
    check("no bid is unpriceable", sell_limit_price(contract(bid=None)) is None)
    check("zero bid is unpriceable", sell_limit_price(contract(bid=0.0)) is None)

    a = client_order_id(7, c)
    check("client order id is deterministic", a == client_order_id(7, c))
    check("client order id varies by cycle", a != client_order_id(8, c))
    check("client order id within 128 chars", len(a) <= 128)


# ---------------------------------------------------------- decision ------

def test_decision():
    print("\nLLM boundary (must fail CLOSED and must never widen size)")
    check("plain json parses", parse_verdict('{"action":"proceed"}') is not None)
    check("fenced json parses",
          parse_verdict('```json\n{"action":"veto"}\n```') is not None)
    check("json with prose parses",
          parse_verdict('Sure!\n{"action":"shrink"}\nHope that helps')is not None)
    check("garbage returns None", parse_verdict("no json at all") is None)
    check("empty returns None", parse_verdict("") is None)

    d = coerce({"action": "proceed", "size_multiplier": 5.0, "reasoning": "x"},
               "t", "m", "")
    check("multiplier clamped to 1.0 on proceed", d.size_multiplier == 1.0)

    d = coerce({"action": "veto", "size_multiplier": 1.0, "reasoning": "x"}, "t", "m", "")
    check("veto forces size 0", d.size_multiplier == 0.0 and not d.approved)

    d = coerce({"action": "shrink", "size_multiplier": 0.5, "reasoning": "x"},
               "t", "m", "")
    check("shrink preserved and approved", d.size_multiplier == 0.5 and d.approved)

    d = coerce({"action": "banana", "size_multiplier": 1.0}, "t", "m", "")
    check("unknown action fails closed", d.action == "veto" and not d.approved)

    d = coerce({"action": "proceed", "size_multiplier": "abc"}, "t", "m", "")
    check("non-numeric size fails closed", d.action == "veto")

    d = coerce({"action": "shrink", "size_multiplier": 0.0}, "t", "m", "")
    check("shrink-to-zero fails closed", d.action == "veto")


# ----------------------------------------------------------- critique ------

def test_critique():
    print("\nself-critique (the critic may only tighten, never loosen)")
    from src.decide import Decision, combine

    def d(action, mult, who="first"):
        return Decision(action, mult, f"{who} says {action}", who, "m")

    r = combine(d("proceed", 1.0), d("veto", 0.0, "critic"))
    check("critic can veto an approved trade", r.action == "veto" and r.size_multiplier == 0.0)
    check("override is recorded in the reasoning", "CRITIC OVERRODE" in r.reasoning)

    r = combine(d("veto", 0.0), d("proceed", 1.0, "critic"))
    check("critic CANNOT upgrade a veto", r.action == "veto", f"got {r.action}")
    check("upgraded veto still sizes zero", r.size_multiplier == 0.0)

    r = combine(d("proceed", 1.0), d("shrink", 0.4, "critic"))
    check("critic can shrink", r.action == "shrink" and r.size_multiplier == 0.4)

    r = combine(d("shrink", 0.3), d("proceed", 1.0, "critic"))
    check("critic CANNOT widen a shrink", r.size_multiplier == 0.3,
          f"got {r.size_multiplier}")

    r = combine(d("shrink", 0.6), d("shrink", 0.2, "critic"))
    check("two shrinks take the smaller", r.size_multiplier == 0.2)

    base = d("proceed", 1.0)
    check("no critic leaves the verdict alone", combine(base, None) is base)

    failed = Decision("proceed", 1.0, "critique unavailable", "critic", "m",
                      error="timeout")
    r = combine(base, failed)
    check("a failed critic does not block the trade", r.action == "proceed",
          "an optional second opinion must not manufacture a zero-trade week")


# --------------------------------------------------------- sampling ------

def test_temperature():
    print("\nsampling (greedy verdicts; retry a broken FORMAT, never a verdict)")
    import asyncio
    import os

    from src import decide as D

    original, sent = D._featherless, []

    async def stub(system, payload, json_mode, max_tokens, temperature, model_override):
        sent.append({"temp": temperature, "json_mode": json_mode})
        if len(sent) == 1:
            return "thinking out loud, no json here", None, "m"
        return '{"action":"shrink","size_multiplier":0.5,"reasoning":"ok"}', None, "m"

    os.environ["LLM_PROVIDER"] = "featherless"
    try:
        D._featherless = stub
        d = asyncio.run(D.decide({"ticker": "CSCO"}))
        check("primary call decodes greedily", sent and sent[0]["temp"] == 0.0,
              f"got {sent[0]['temp'] if sent else None}")
        check("unparseable response is retried once", len(sent) == 2, f"{len(sent)} calls")
        check("retry nudges temperature off zero",
              len(sent) > 1 and sent[1]["temp"] == config.LLM_RETRY_TEMPERATURE)
        check("retry verdict is used", d.action == "shrink", f"got {d.action}")

        sent.clear()

        async def veto(system, payload, json_mode, max_tokens, temperature, model_override):
            sent.append(temperature)
            return '{"action":"veto","size_multiplier":0,"reasoning":"bid zero"}', None, "m"

        D._featherless = veto
        d = asyncio.run(D.decide({"ticker": "GM"}))
        check("a veto is NEVER re-rolled", len(sent) == 1 and d.action == "veto",
              "re-rolling until a veto turns into a proceed is answer shopping")

        sent.clear()

        async def narr(system, payload, json_mode, max_tokens, temperature, model_override):
            sent.append({"temp": temperature, "json_mode": json_mode})
            return "Nothing traded this cycle.", None, "m"

        D._featherless = narr
        asyncio.run(D.narrate({"cycle": 3}))
        check("narrator is not greedy",
              sent and sent[0]["temp"] == config.LLM_NARRATE_TEMPERATURE)
        check("narrator does not request json mode", sent and sent[0]["json_mode"] is False)
    finally:
        D._featherless = original
        os.environ.pop("LLM_PROVIDER", None)


# ------------------------------------------------------------- state ------

def test_state():
    print("\nstate (session rollover must reset the breaker AND the order cap)")
    st = State()
    st.begin_cycle(datetime(2026, 8, 31, 10, 0), 100_000.0)
    st.trip_breaker("test")
    st.orders_today = 7
    check("drawdown from high-water", abs(st.drawdown(97_000.0) - 0.03) < 1e-9)

    st.begin_cycle(datetime(2026, 9, 1, 10, 0), 99_000.0)
    check("new session clears breaker", not st.breaker_tripped)
    check("new session resets order count", st.orders_today == 0)
    check("new session resets high-water", st.day_high_water == 99_000.0)
    check("cycle counter survives rollover", st.cycle == 2)

    st.mark_qualified("AAPL")
    first = st.first_qualified["AAPL"]
    st.begin_cycle(datetime(2026, 9, 1, 10, 15), 99_000.0)
    st.mark_qualified("AAPL")
    check("first_qualified never overwritten", st.first_qualified["AAPL"] == first)
    check("age accrues while waiting", st.age("AAPL") == 1)


def main() -> int:
    for fn in (test_occ, test_chain, test_signal, test_allocation, test_gates,
               test_execution, test_decision, test_critique, test_temperature, test_state):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
