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

    # A corrupt quote can only ever look MORE attractive, so the floor on
    # credit needs a ceiling. Benchmarking found the LLM proceeding on this.
    rich = contract(ticker="PEP", strike=141.0, bid=44.80, ask=45.60, delta=-0.17)
    g = gates.evaluate(rich, now=now, equity=100_000.0, spot=144.0,
                       deployed_collateral=0.0, breaker_tripped=False,
                       held_symbols=set())
    check("implausible premium is rejected deterministically", not g.passed,
          "a mis-scaled bid must not reach the model")
    check("and it names credit_sanity", "credit_sanity" in (g.reason or ""),
          f"got {g.reason!r}")

    normal = contract(ticker="CSCO", strike=67.0, bid=0.44, ask=0.48, delta=-0.18)
    g = gates.evaluate(normal, now=now, equity=100_000.0, spot=68.5,
                       deployed_collateral=0.0, breaker_tripped=False,
                       held_symbols=set())
    check("a real quote does NOT trip the ceiling", g.passed, g.reason)

    # History gate: what the stock actually did, against what delta claims.
    ok = gates.evaluate(normal, now=now, equity=100_000.0, spot=68.5,
                        deployed_collateral=0.0, breaker_tripped=False,
                        held_symbols=set(), empirical_itm=(0.24, 86))
    check("ordinary assignment history passes", ok.passed, ok.reason)

    bad = gates.evaluate(normal, now=now, equity=100_000.0, spot=68.5,
                         deployed_collateral=0.0, breaker_tripped=False,
                         held_symbols=set(), empirical_itm=(0.55, 86))
    check("history above the ceiling is rejected",
          not bad.passed and "assignment_history" in (bad.reason or ""),
          f"got {bad.reason!r}")

    thin = gates.evaluate(normal, now=now, equity=100_000.0, spot=68.5,
                          deployed_collateral=0.0, breaker_tripped=False,
                          held_symbols=set(), empirical_itm=(0.90, 5))
    check("too few windows does not reject", thin.passed,
          "a rate from five samples is noise, not evidence")
    check("but it is recorded as unevaluated",
          any("history" in n for n in thin.notes), f"notes={thin.notes}")

    none_ = gates.evaluate(normal, now=now, equity=100_000.0, spot=68.5,
                           deployed_collateral=0.0, breaker_tripped=False,
                           held_symbols=set(), empirical_itm=None)
    check("unmeasured history is skipped entirely", none_.passed)

    # The estimator itself.
    from src.signal import empirical_itm_rate
    flat = [{"c": 100.0} for _ in range(60)]
    r = empirical_itm_rate(flat, 0.02, 4)
    check("a flat series never finishes ITM", r and r[0] == 0.0)
    crash = [{"c": 100.0 - i} for i in range(60)]
    r2 = empirical_itm_rate(crash, 0.02, 4)
    check("a falling series always does", r2 and r2[0] > 0.9, f"got {r2}")
    check("too little data returns None", empirical_itm_rate(flat[:3], 0.02, 4) is None)

    # The moneyness fallback exists for MISSING delta, not unwelcome delta. A
    # live scan found AAL quoting deltas on every strike, none in band, and the
    # fallback returning a 0.40-delta put -- twice the assignment risk the band
    # caps, reached through a path meant to handle absent data.
    from src.agent import pick_contract

    def p(strike, delta):
        return Contract(symbol=f"AAL260904P{int(strike * 1000):08d}",
                        underlying="AAL", strike=strike, expiry=config.TARGET_EXPIRY,
                        bid=0.22, ask=0.24, open_interest=900, delta=delta)

    got, _ = pick_contract([p(13.5, -0.399), p(13.0, -0.34)], spot=13.68)
    check("known out-of-band delta is NOT reachable via moneyness", got is None,
          f"got delta {got.delta if got else None}")
    got, why = pick_contract([p(13.5, None), p(13.0, None)], spot=13.68)
    check("genuinely missing delta still uses the fallback", got is not None, why)


# ------------------------------------------------------------ signal ------

def test_signal():
    print("\nsignal ranking (ties at the floor = zero trades all week)")
    check("all-tied pool ranks mid", sig.rank_within(0.24, [0.24] * 6) == 0.5)

    # Correlation: None when unmeasurable, never a fabricated 0.0.
    import random
    random.seed(7)
    walk = [100.0]
    for _ in range(40):
        walk.append(walk[-1] * (1 + random.gauss(0, 0.02)))
    other = [100.0]
    for _ in range(40):
        other.append(other[-1] * (1 + random.gauss(0, 0.02)))
    bars_of = lambda xs: [{"c": v} for v in xs]
    ra = sig.log_returns(bars_of(walk))
    rb = sig.log_returns(bars_of(other))
    rinv = sig.log_returns(bars_of([200.0 - p for p in walk]))
    check("identical series correlate at 1", sig.correlation(ra, ra) > 0.99)
    check("inverse series correlate at -1", sig.correlation(ra, rinv) < -0.99)
    check("independent series correlate near 0", abs(sig.correlation(ra, rb)) < 0.4)
    check("too few samples returns None, not 0.0",
          sig.correlation(ra[:5], rb[:5]) is None,
          "0.0 asserts independence; None admits ignorance")
    check("a flat series returns None", sig.correlation([0.0] * 20, [0.0] * 20) is None)
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

    # age must be LIVE, not a constant. first_qualified was set once and never
    # moved, so every candidate that became runnable together carried the same
    # age forever and the term cancelled out of every comparison.
    # Uses its OWN state -- charging capital here would otherwise pollute the
    # rotation check further down, which shares `st`.
    aged = State()
    aged.begin_cycle(datetime.now(), 100_000.0)
    for c in (small, big):
        aged.mark_qualified(c.underlying)
    aged.cycle += 4
    check("age accrues while waiting", aged.age("IWM") == 4, f"got {aged.age('IWM')}")
    aged.charge_capital("IWM", big.collateral, 100_000.0, d)
    check("winning resets age", aged.age("IWM") == 0,
          "age must mean cycles since last funded, not since first seen")
    check("the passed-over name keeps its age", aged.age("NVDA") == 4)

    st.charge_capital("NVDA", small.collateral, 100_000.0, d)
    check("ubt accrues after winning", st.ubt("NVDA") > 0)
    w2, _ = select([(small, 0.5), (big, 0.9)], st, 100_000.0, date(2026, 8, 31))
    check("winner rotates after paying ubt", w2.contract.underlying == "IWM",
          f"got {w2.contract.underlying}")


# ------------------------------------------------------------ reward ------

def test_reward():
    print("\nreward term (must not turn the index policy into greedy)")
    from src.allocation import expected_yield, pwt

    original = config.REWARD_LAMBDA
    try:
        # lambda = 0 must reproduce the original three-term index EXACTLY.
        config.REWARD_LAMBDA = 0.0
        st = State()
        st.begin_cycle(datetime.now(), 100_000.0)
        cheap = contract("GM", 55.0, bid=0.40, ask=0.44)
        rich = contract("CSCO", 67.0, bid=1.60, ask=1.64)
        for c in (cheap, rich):
            st.mark_qualified(c.underlying)
        w0, s0 = select([(cheap, 0.5), (rich, 0.5)], st, 100_000.0, date(2026, 8, 31))
        check("lambda=0 reproduces the 3-term index",
              all(abs(s.pwt - (s.age - s.ubt + s.opbt)) < 1e-9 for s in s0))
        check("lambda=0 gives zero reward to everyone",
              all(s.reward == 0.0 for s in s0))

        # With lambda on, the better-paying contract should win a tie.
        config.REWARD_LAMBDA = 0.3
        st2 = State()
        st2.begin_cycle(datetime.now(), 100_000.0)
        for c in (cheap, rich):
            st2.mark_qualified(c.underlying)
        w1, s1 = select([(cheap, 0.5), (rich, 0.5)], st2, 100_000.0, date(2026, 8, 31))
        by = {s.contract.underlying: s for s in s1}
        check("higher-yielding contract scores a higher reward",
              by["CSCO"].reward > by["GM"].reward,
              f"CSCO {by['CSCO'].reward:.3f} vs GM {by['GM'].reward:.3f}")
        check("reward is bounded by lambda",
              all(s.reward <= config.REWARD_LAMBDA + 1e-9 for s in s1))

        # THE property that matters. An incumbent must not hold the seat
        # forever just because it pays best -- ubt has to reclaim it.
        st3 = State()
        wins: list[str] = []
        for _ in range(25):
            st3.begin_cycle(datetime.now(), 100_000.0)
            for c in (cheap, rich):
                st3.mark_qualified(c.underlying)
            w, _ = select([(cheap, 0.5), (rich, 0.5)], st3, 100_000.0,
                          date(2026, 8, 31))
            wins.append(w.contract.underlying)
            st3.charge_capital(w.contract.underlying, w.contract.collateral,
                               100_000.0, 4)
        streak = 1
        longest = 1
        for a, b in zip(wins, wins[1:]):
            streak = streak + 1 if a == b else 1
            longest = max(longest, streak)
        check("the loser is not starved", len(set(wins)) == 2,
              f"only {set(wins)} ever won across 25 cycles")
        check("no unbounded incumbency", longest <= 8,
              f"longest streak {longest}: reward is overpowering ubt")

        # Risk adjustment: same premium, more delta, lower reward. Without
        # this the index prefers whatever sits closest to the money.
        near = contract("A", 100.0, bid=1.00, ask=1.04, delta=-0.30)
        far = contract("B", 100.0, bid=1.00, ask=1.04, delta=-0.16)
        check("higher delta yields LESS for the same premium",
              expected_yield(near, 4) < expected_yield(far, 4),
              "raw yield would rank maximum assignment risk highest")

        # Missing delta must not be fabricated from a guessed sigma.
        unknown = contract("C", 100.0, bid=1.00, ask=1.04, delta=None)
        neutral = (1.00 / 100.0) * (1 - config.DELTA_TARGET) / 4
        check("null delta falls back to the band target",
              abs(expected_yield(unknown, 4) - neutral) < 1e-9)
        check("no bid yields nothing rather than dividing by zero",
              expected_yield(contract("D", 100.0, bid=None), 4) == 0.0)
        w = config.AGE_WEIGHT
        check("reward is additive in pwt",
              abs(pwt(3, 0.5, 1.0, 0.3) - (w * 3 - 0.5 + 1.0 + 0.3)) < 1e-9)
        check("age is weighted, not raw",
              abs(pwt(10, 0.0, 0.0, 0.0) - w * 10) < 1e-9,
              "unweighted age outranks eleven wins of consumed capital")

        # The reward must rank the EDGE when we have it, not the premium. A
        # live scan found the highest-yielding contract in the set was the one
        # whose underlying realised more vol than its option priced.
        st5 = State()
        st5.begin_cycle(datetime.now(), 100_000.0)
        rich_prem = contract("AAA", 100.0, bid=2.00, ask=2.04)   # pays most
        real_edge = contract("BBB", 100.0, bid=0.50, ask=0.54)   # pays least
        for c in (rich_prem, real_edge):
            st5.mark_qualified(c.underlying)
        w2, s5 = select([(rich_prem, 0.5), (real_edge, 0.5)], st5, 100_000.0,
                        date(2026, 8, 31),
                        edge={"AAA": -0.40, "BBB": +0.12})
        check("reward follows VRP, not premium",
              w2.contract.underlying == "BBB",
              f"picked {w2.contract.underlying}: ranking the payout again")
        by5 = {s.contract.underlying: s for s in s5}
        check("negative-VRP name gets the lower reward",
              by5["AAA"].reward < by5["BBB"].reward)

        # Crowding: ubt stops us doubling into one TICKER and nothing more, so
        # four semiconductor names read as diversification while being one bet.
        st7 = State()
        st7.begin_cycle(datetime.now(), 100_000.0)
        twin = contract("TWIN", 100.0, bid=1.00, ask=1.04)
        indep = contract("INDY", 100.0, bid=1.00, ask=1.04)
        for c in (twin, indep):
            st7.mark_qualified(c.underlying)
        w7, s7 = select([(twin, 0.5), (indep, 0.5)], st7, 100_000.0,
                        date(2026, 8, 31),
                        crowd={"TWIN": 0.95, "INDY": 0.05})
        check("a name correlated with the book loses to one that is not",
              w7.contract.underlying == "INDY", f"picked {w7.contract.underlying}")
        by7 = {s.contract.underlying: s for s in s7}
        check("crowding scales with correlation",
              by7["TWIN"].crowding > by7["INDY"].crowding)
        check("crowding is bounded by mu",
              by7["TWIN"].crowding <= config.CROWDING_MU + 1e-9)

        # A hedge is not a crime. Negative correlation must not be rewarded
        # OR punished -- it clamps to zero.
        _, s8 = select([(twin, 0.5), (indep, 0.5)], st7, 100_000.0,
                       date(2026, 8, 31), crowd={"TWIN": -0.8, "INDY": 0.0})
        by8 = {s.contract.underlying: s for s in s8}
        check("negative correlation is not penalised", by8["TWIN"].crowding == 0.0)

        # Unmeasured correlation must not be read as "uncorrelated".
        _, s9 = select([(twin, 0.5), (indep, 0.5)], st7, 100_000.0,
                       date(2026, 8, 31), crowd={"INDY": 0.9})
        by9 = {s.contract.underlying: s for s in s9}
        check("unmeasured pair carries no crowding penalty",
              by9["TWIN"].crowding == 0.0)
        check("but a measured one does", by9["INDY"].crowding > 0.0)

        # Unmeasurable edge must be neutral -- never best, never worst.
        _, s6 = select([(rich_prem, 0.5), (real_edge, 0.5)], st5, 100_000.0,
                       date(2026, 8, 31), edge={"BBB": +0.12})
        by6 = {s.contract.underlying: s for s in s6}
        check("missing edge ranks neutral",
              abs(by6["AAA"].reward - config.REWARD_LAMBDA * 0.5) < 1e-9,
              f"got {by6['AAA'].reward}")

        # Rank spacing is lambda/(n-1), so the term is strongest in a
        # two-horse race -- which is what a late session looks like once
        # names get gated out. Lambda must hold rotation at n=2, the case
        # tuning on a larger fixture silently breaks.
        config.REWARD_LAMBDA = original
        st4 = State()
        st4.begin_cycle(datetime.now(), 100_000.0)
        a = contract("NVDA", 175.0)
        b = contract("IWM", 235.0)
        for c in (a, b):
            st4.mark_qualified(c.underlying)
        w = select([(a, 0.5), (b, 0.9)], st4, 100_000.0, date(2026, 8, 31))[0]
        st4.charge_capital(w.contract.underlying, w.contract.collateral,
                           100_000.0, 4)
        w2 = select([(a, 0.5), (b, 0.9)], st4, 100_000.0, date(2026, 8, 31))[0]
        check("shipped lambda still rotates in a two-horse race",
              w2.contract.underlying != w.contract.underlying,
              f"{w.contract.underlying} held the seat after paying ubt; "
              f"REWARD_LAMBDA={original} is too large for n=2")
    finally:
        config.REWARD_LAMBDA = original


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

    # Derived from config, not hardcoded. A literal here silently stopped
    # testing anything the moment PORTFOLIO_CAP moved from 0.60 to 0.85: the
    # old 58,000 fixture no longer breached, so the gate "passed" its own test
    # by not firing.
    cap = config.PORTFOLIO_CAP * base["equity"]
    coll = contract().collateral
    g = gates.evaluate(contract(), **{**base, "deployed_collateral": cap - coll / 2})
    check("g6 rejects portfolio over-deployment",
          not g.passed and "portfolio_cap" in g.reason,
          f"cap {cap:,.0f}, this fill would reach {cap + coll / 2:,.0f}")

    # And the other direction: deployment well inside the cap must be allowed,
    # or raising the cap buys nothing.
    g = gates.evaluate(contract(), **{**base, "deployed_collateral": cap - coll * 2})
    check("g6 allows a fill that stays inside the cap", g.passed, g.reason)

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
    check("the BINDING reason leads", r.reasoning.startswith("CRITIC OVERRODE"),
          "logs truncate; a veto that opens with the approval reads as a bug")

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
    for fn in (test_occ, test_chain, test_signal, test_allocation, test_reward, test_gates,
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
