"""Pick the tradeable universe from the live market.

Shared by tools/scan.py (inspect it) and run.py (use it at startup), so the
list the agent trades is the same list you reviewed, produced by one code path
rather than two that drift.

The funnel is ordered cheapest-filter-first:

  screener          most-active, then movers for breadth. Liquidity is the
                    precondition for everything downstream
  batch quote       one call per 25 names, so price filtering is nearly free
  collateral cap    strike x 100 must fit PER_POSITION_CAP x equity, which
                    removes most of the market on price alone
  option chain      the expensive call, and only for survivors
  THE REAL GATES    gates.evaluate on the actual target contract
  vol ceiling       realised vol measured from bars, not a list of tickers
  rank              variance risk premium: implied vol minus realised

Running the real gates is the part that matters. An earlier version ranked on
yield alone and returned names whose target contract the agent would reject on
sight -- PATH quoted 0.15 wide on a 0.22 bid, a 51% spread against a 20% limit,
ranked second. A universe of contracts that fail gate 3 is a zero-trade week
wearing the costume of a market scan.
"""

from __future__ import annotations

from datetime import date

import config
from src import gates
from src.allocation import expected_yield
from src.data import Data, by_symbol
from src.signal import realized_vol

# Below this the premium is rounding error against the spread. Above it the
# collateral cap does the work. Both are about tradeability, not a view.
MIN_PRICE = 12.0

# Alpaca's screener rejects top > 100 with a 400, and the failure does not
# degrade gracefully: retrying without the parameter returns the ten-name
# default, which reads as a thin market rather than a bad argument.
SCREENER_MAX = 100

# Ranking on yield ranks VOLATILITY, because volatility is what pays premium.
# Unfiltered, this scan put SOXL, SOXS, TQQQ and SQQQ in the top ten -- 3x
# leveraged funds including a long/short pair on one underlying, which it
# proposed selling puts on simultaneously. A 3x fund gapping 25% is a Tuesday,
# and no delta adjustment prices it: delta describes the contract, not the
# leverage of the thing underneath.
LEVERAGED = {
    "SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS", "TNA", "TZA", "LABU", "LABD",
    "FAS", "FAZ", "YINN", "YANG", "NUGT", "DUST", "JNUG", "JDST", "UVXY", "SVXY",
    "VIXY", "UPRO", "SPXU", "UDOW", "SDOW", "TMF", "TMV", "BOIL", "KOLD", "ERX",
    "ERY", "DRIP", "GUSH", "WEBL", "WEBS", "TECL", "TECS", "CURE", "NAIL", "MSTU",
    "MSTZ", "TSLL", "TSLQ", "NVDL", "NVD", "CONL", "AMDL", "BITX", "ETHU", "BITU",
}

# Crypto exposure, whether or not it is wrapped as a fund. The ETF tickers were
# the obvious half; the scan then returned CIFR, BMNR and MSTR, which are a
# miner, a treasury company and a bitcoin proxy -- equities by listing and
# crypto by risk, carrying weekend gaps the option market cannot hedge.
CRYPTO = {
    "IBIT", "ETHA", "FBTC", "GBTC", "ARKB", "BITB", "ETHE", "BITO", "BTF",
    "MSTR", "MARA", "RIOT", "CLSK", "HUT", "CORZ", "CIFR", "BMNR", "WULF",
    "HIVE", "BITF", "IREN", "GLXY", "COIN", "BTBT", "SDIG", "CAN", "BTDR",
}

EXCLUDED = LEVERAGED | CRYPTO


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


async def build_pool(mcp, n: int, note=print) -> list[str]:
    """Union the screener endpoints, most-liquid first."""
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
            note(f"  {op} unavailable: {str(exc)[:90]}")
    return out[:n]


async def scan(mcp, equity: float, now, pool_size: int = 100,
               note=print) -> tuple[list[dict], list[tuple[str, str]]]:
    """Returns (ranked rows, rejections). Never raises: a scan that cannot
    complete must degrade to an empty result so the caller can fall back,
    rather than taking the session down with it."""
    try:
        max_strike = (config.PER_POSITION_CAP * equity) / 100.0

        pool = await build_pool(mcp, pool_size, note)
        if len(pool) < config.UNIVERSE_MIN_NAMES:
            pool = list(dict.fromkeys(pool + list(config.UNIVERSE_CANDIDATES)))
            note("  screener thin; topped up from the configured list")

        spots: dict[str, float] = {}
        for i in range(0, len(pool), 25):
            chunk = pool[i:i + 25]
            try:
                q = await mcp.call("stock_quote", symbols=",".join(chunk))
            except Exception as exc:  # noqa: BLE001
                note(f"  quote batch failed: {str(exc)[:90]}")
                continue
            for sym in chunk:
                row = by_symbol(q, "quotes", sym) or {}
                px = next((float(v) for v in (row.get("ap"), row.get("bp"),
                                              row.get("ask_price"),
                                              row.get("bid_price")) if v), 0.0)
                if px > 0:
                    spots[sym] = px

        # Both filters, because they catch different things and neither is
        # sufficient. The realised-vol ceiling below is the general mechanism
        # and needs no maintenance, but it is backward-looking: it cannot see
        # that SQQQ is structurally 3x leveraged, or that BMNR holds an asset
        # trading all weekend while the option market is shut. Measured on
        # trailing bars both looked calm -- 0.56 and 0.63, comfortably under
        # the ceiling -- and VRP ranked BMNR first of everything.
        affordable = [s for s, px in spots.items()
                      if MIN_PRICE <= px <= max_strike and s not in EXCLUDED]
        note(f"  pool {len(pool)} -> priced {len(spots)} -> affordable "
             f"{len(affordable)} (max strike ${max_strike:,.0f})")

        y, m, d = (int(x) for x in config.TARGET_EXPIRY.split("-"))
        dte = max((date(y, m, d) - now.date()).days, 1)

        from src.agent import pick_contract

        data = Data(mcp)
        rows: list[dict] = []
        rejected: list[tuple[str, str]] = []
        for sym in affordable:
            try:
                chain = await data.put_chain(sym, config.TARGET_EXPIRY)
            except Exception:  # noqa: BLE001
                continue
            if not chain:
                continue
            contract, why = pick_contract(chain, spots[sym])
            if contract is None:
                rejected.append((sym, why))
                continue
            g = gates.evaluate(contract, now=now, equity=equity, spot=spots[sym],
                               deployed_collateral=0.0, breaker_tripped=False,
                               held_symbols=set())
            if not g.passed:
                rejected.append((sym, g.reason or "gate"))
                continue

            # Measure the volatility rather than recognising the ticker. A
            # denylist of leveraged and crypto names is a list someone typed:
            # it goes stale the day a new 3x fund lists, and it never catches
            # the ordinary equity that happens to be realising 120%. What
            # actually disqualifies those names is the volatility itself, and
            # that is a number we can read off the tape.
            rv_check = None
            try:
                rv_check = realized_vol(await data.bars(sym, 40),
                                        config.RV_LOOKBACK_DAYS)
            except Exception as exc:  # noqa: BLE001
                note(f"  {sym}: no realised vol ({type(exc).__name__})")
            if rv_check is not None and rv_check > config.MAX_REALIZED_VOL:
                rejected.append(
                    (sym, f"realised vol {rv_check:.0%} over the "
                          f"{config.MAX_REALIZED_VOL:.0%} ceiling"))
                continue
            # Realised vol from the same bars the signal layer uses. This is
            # the measurement that replaces a denylist: a 3x fund or a bitcoin
            # miner does not need to be recognised by NAME, it announces
            # itself by realising three times the volatility of anything else.
            rv = rv_check

            iv = contract.iv
            # The strategy's entire claimed edge, per name and measurable:
            # implied vol richer than what the underlying is actually
            # realising. Premium yield alone just ranks volatility, and
            # volatility is what a short-put book dies of.
            vrp = (iv - rv) if (iv is not None and rv is not None) else None

            rows.append({
                "ticker": sym, "spot": round(spots[sym], 2),
                "strike": contract.strike,
                "collateral": round(contract.collateral, 0),
                "pct_equity": contract.collateral / equity,
                "bid": contract.bid,
                "spread": round((contract.ask or 0) - (contract.bid or 0), 3),
                "oi": contract.open_interest, "delta": contract.delta,
                "iv": iv, "rv": rv, "vrp": vrp,
                "yield": expected_yield(contract, dte), "why": why,
            })

        # Rank on the variance risk premium, not on premium yield.
        #
        # Measured live, this is not a refinement, it is a sign flip. Yield
        # ranked CIFR first at 0.00283 -- and CIFR's implied vol was 1.06
        # against realised 1.20, so that premium is high because the stock is
        # moving MORE than the option price assumes. PLTR was realising double
        # its implied (0.51 vs 1.01). A yield ranking systematically finds the
        # names where selling volatility is underpriced, which is the opposite
        # of the edge this strategy claims.
        #
        # Only 5 of 15 names that cleared the gates had positive VRP at all.
        # Ranking on it puts those first instead of last.
        #
        # Yield still breaks ties and still ranks names whose vol we could not
        # measure, since between two contracts with the same edge the one
        # paying more per capital-day is strictly better.
        rows.sort(key=lambda r: (r["vrp"] if r["vrp"] is not None else -9.0,
                                 r["yield"]), reverse=True)
        return rows, rejected
    except Exception as exc:  # noqa: BLE001
        note(f"  scan failed: {type(exc).__name__}: {exc}")
        return [], []
