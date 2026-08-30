"""Layer 1: data. Normalises whatever alpaca-mcp-server returns into typed
records the rest of the agent can rely on.

Field names drift between MCP versions and between endpoints, so every read
goes through `pick()` with a list of aliases rather than a hardcoded key.
A missing field becomes None and is handled downstream -- it never becomes a
KeyError at 10:15 on Monday.

Data-source note (§7): on the free tier the LATEST option/stock quote is
real-time; only historical bars and trades carry the 15-minute delay. So
`spot()` and chain quotes drive pricing, while `bars()` -- delayed, and fine
for the purpose -- drives realised vol and momentum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from src.mcp_client import AlpacaMCP


def pick(d: Any, *names: str, default=None):
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def fnum(d: Any, *names: str, default=None) -> float | None:
    v = pick(d, *names, default=None)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def by_symbol(payload: Any, container: str, ticker: str) -> Any:
    """Pull one symbol out of Alpaca's `{container: {SYMBOL: payload}}` shape.

    Market-data endpoints nest twice: `{"quotes": {"AAPL": {...}}}` and
    `{"bars": {"AAPL": [...]}}`. Tolerates the container being absent, in case
    a future server version flattens it.
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get(container)
    if isinstance(inner, dict):
        if ticker in inner:
            return inner[ticker]
        # Single-symbol request that came back unkeyed.
        vals = list(inner.values())
        return vals[0] if len(vals) == 1 else None
    if ticker in payload:
        return payload[ticker]
    return None


def as_list(payload: Any, *keys: str) -> list[dict]:
    """MCP may return a bare list or wrap it under one of several keys."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # Snapshot endpoints sometimes key by symbol.
        vals = list(payload.values())
        if vals and all(isinstance(v, dict) for v in vals):
            out = []
            for k, v in payload.items():
                v = dict(v)
                v.setdefault("symbol", k)
                out.append(v)
            return out
    return []


# --- records -----------------------------------------------------------------


@dataclass
class OCC:
    """A parsed OCC option symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits).

    Parsed strictly, because the alternative is worse than an error. The old
    `int(symbol[-8:]) / 1000` returned 0.0 for anything unexpected, which made
    deployed collateral under-count, which silently disabled the portfolio cap.
    A risk limit that fails open without saying so is the worst failure mode in
    the system, so this returns None and forces the caller to handle it.
    """

    root: str
    expiry: str  # ISO yyyy-mm-dd
    right: str  # "C" | "P"
    strike: float

    @property
    def collateral(self) -> float:
        return self.strike * 100


def parse_occ(symbol: str) -> OCC | None:
    s = (symbol or "").strip().upper()
    if len(s) < 16:  # 1 root char + 6 date + 1 right + 8 strike
        return None
    strike_part, right, date_part, root = s[-8:], s[-9], s[-15:-9], s[:-15]
    if not (strike_part.isdigit() and date_part.isdigit() and right in "CP"):
        return None
    if not root or not root.isalpha():
        return None
    try:
        yy, mm, dd = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:6])
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return None
        strike = int(strike_part) / 1000.0
    except ValueError:
        return None
    if strike <= 0:
        return None
    return OCC(root=root, expiry=f"20{yy:02d}-{mm:02d}-{dd:02d}",
               right=right, strike=strike)


@dataclass
class Account:
    equity: float
    buying_power: float
    options_level: int | None
    status: str | None
    trading_blocked: bool

    @property
    def tradable(self) -> bool:
        return (
            not self.trading_blocked
            and (self.options_level is None or self.options_level >= 1)
            and self.equity > 0
        )


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float | None
    unrealized_pl: float | None
    asset_class: str | None
    underlying: str | None = None

    @property
    def is_option(self) -> bool:
        cls = (self.asset_class or "").lower()
        return "option" in cls or len(self.symbol) > 10


@dataclass
class Contract:
    symbol: str
    underlying: str
    strike: float
    expiry: str
    bid: float | None = None
    ask: float | None = None
    open_interest: int | None = None
    delta: float | None = None
    iv: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_abs(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_rel(self) -> float | None:
        m, s = self.mid, self.spread_abs
        if not m or s is None or m <= 0:
            return None
        return s / m

    @property
    def collateral(self) -> float:
        """Cash-secured put collateral. The number gate 2 and gate 6 cap."""
        return self.strike * 100


# --- reads -------------------------------------------------------------------


class Data:
    def __init__(self, mcp: AlpacaMCP):
        self.mcp = mcp
        # Daily bars cannot change during a session, so refetching 40 of them
        # for 12 tickers every 15-minute cycle is ~300 wasted calls a day on a
        # free tier -- and every one is a chance to hit a rate limit mid-session
        # for data we already have. Keyed by (ticker, date).
        self._bars_cache: dict[tuple[str, int, str], list[dict]] = {}
        # Spot IS live and must stay fresh across cycles, but the same ticker
        # gets asked for more than once within a single cycle. Short TTL
        # dedupes inside a cycle without ever serving a stale price to the next.
        self._spot_cache: dict[str, tuple[float, float | None]] = {}
        self._spot_ttl = 60.0

    async def account(self) -> Account:
        raw = await self.mcp.call("account")
        lvl = pick(raw, "options_trading_level", "options_approved_level")
        try:
            lvl = int(lvl) if lvl is not None else None
        except (TypeError, ValueError):
            lvl = None
        blocked = pick(raw, "trading_blocked", "account_blocked", default=False)
        return Account(
            equity=fnum(raw, "equity", "portfolio_value", default=0.0) or 0.0,
            buying_power=fnum(raw, "buying_power", "options_buying_power", default=0.0)
            or 0.0,
            options_level=lvl,
            status=pick(raw, "status"),
            trading_blocked=bool(blocked) and str(blocked).lower() != "false",
        )

    async def positions(self) -> list[Position]:
        raw = await self.mcp.call("positions")
        out = []
        for p in as_list(raw, "positions"):
            sym = pick(p, "symbol", "asset_symbol") or ""
            out.append(
                Position(
                    symbol=sym,
                    qty=fnum(p, "qty", "quantity", default=0.0) or 0.0,
                    market_value=fnum(p, "market_value", "market_val"),
                    unrealized_pl=fnum(p, "unrealized_pl", "unrealized_pnl"),
                    asset_class=pick(p, "asset_class", "class"),
                    underlying=pick(p, "underlying_symbol", "underlying"),
                )
            )
        return out

    async def spot(self, ticker: str, fresh: bool = False) -> float | None:
        """Latest quote. Real-time even on the free tier.

        `fresh=True` bypasses the dedupe cache. Use it anywhere a decision
        turns on the price right now rather than the price this cycle opened
        with. The circuit breaker is the case that matters: it fires when the
        market is moving fast, which is exactly when a quote up to a minute
        old can rank the wrong position as the most dangerous one.
        """
        import time as _time

        hit = self._spot_cache.get(ticker)
        if hit and not fresh and (_time.monotonic() - hit[0]) < self._spot_ttl:
            return hit[1]
        try:
            raw = await self.mcp.call("stock_quote", symbols=ticker)
        except Exception:  # noqa: BLE001
            self._spot_cache[ticker] = (_time.monotonic(), None)
            return None
        q = by_symbol(raw, "quotes", ticker)
        if isinstance(q, dict) and isinstance(q.get("quote"), dict):
            q = q["quote"]
        if not isinstance(q, dict):
            self._spot_cache[ticker] = (_time.monotonic(), None)
            return None

        bid = fnum(q, "bp", "bid_price", "bid")
        ask = fnum(q, "ap", "ask_price", "ask")

        # Outside regular hours one side of the book is often 0. Averaging
        # against a zero ask halves the price, and treating the whole quote as
        # missing loses a perfectly good bid, so fall back to whichever side
        # is actually quoted.
        if bid and ask:
            px = (bid + ask) / 2
        elif bid or ask:
            px = bid or ask
        else:
            px = fnum(q, "last_price", "price", "close", "p")

        self._spot_cache[ticker] = (_time.monotonic(), px)
        return px

    async def bars(self, ticker: str, days: int) -> list[dict]:
        """Daily bars for realised vol / momentum. 15-min delay is irrelevant
        here -- never use these for a live price check.

        Cached for the calendar day: these are DAILY closes ending yesterday,
        so nothing about them changes between 09:31 and 15:45.
        """
        key = (ticker, days, date.today().isoformat())
        if key in self._bars_cache:
            return self._bars_cache[key]

        start = (date.today() - timedelta(days=days * 2 + 10)).isoformat()
        end = (date.today() - timedelta(days=1)).isoformat()
        try:
            raw = await self.mcp.call(
                "stock_bars",
                symbols=ticker,
                timeframe="1Day",
                start=start,
                end=end,
            )
        except Exception:  # noqa: BLE001
            return []
        # {"bars": {"AAPL": [ {...}, ... ]}}
        series = by_symbol(raw, "bars", ticker)
        bars = ([b for b in series if isinstance(b, dict)]
                if isinstance(series, list) else as_list(raw, "bars", "data"))
        out = bars[-days:] if days else bars
        if out:  # never cache an empty result -- that is a failure, not data
            self._bars_cache[key] = out
        return out

    @staticmethod
    def _chain_rows(raw: Any) -> list[dict]:
        """Flatten a chain response into rows, whichever shape it arrives in.

        The REST option-chain endpoint returns a DICT keyed by contract symbol
        under `snapshots`, not a list:

            {"snapshots": {"AAPL240426P00162500": {...}}, "next_page_token": ...}

        The MCP server may instead hand back a list under `option_contracts`.
        Accept both, and carry the symbol down from the dict key when that is
        where it lives.
        """
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
        if not isinstance(raw, dict):
            return []

        snaps = raw.get("snapshots")
        if isinstance(snaps, dict):
            rows = []
            for sym, body in snaps.items():
                if isinstance(body, dict):
                    r = dict(body)
                    r.setdefault("symbol", sym)
                    rows.append(r)
            return rows

        for key in ("option_contracts", "contracts", "snapshots", "data", "result"):
            v = raw.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]

        return as_list(raw)

    async def put_chain(self, ticker: str, expiry: str) -> list[Contract]:
        """Puts for one underlying/expiry, with Greeks where Alpaca solved them."""
        raw = await self.mcp.call(
            "option_chain",
            underlying_symbol=ticker,
            expiration_date=expiry,
            type="put",
        )

        out: list[Contract] = []
        for r in self._chain_rows(raw):
            sym = pick(r, "symbol", "contract_symbol", "option_symbol")
            if not sym:
                continue

            # Snapshots carry no strike or expiry: both are encoded in the OCC
            # symbol. Parse them out rather than expecting fields that the
            # endpoint does not return.
            parsed = parse_occ(sym)
            strike = fnum(r, "strike_price", "strike")
            if strike is None:
                strike = parsed.strike if parsed else None
            if strike is None:
                continue

            right = pick(r, "type", "option_type", "right")
            right = str(right).lower() if right else (parsed.right.lower() if parsed else "p")
            if not right.startswith("p"):
                continue

            # camelCase on the REST feed, snake_case via some MCP builds.
            quote = (r.get("latestQuote") or r.get("latest_quote")
                     or r.get("quote") or r)
            greeks = r.get("greeks") or {}

            oi = fnum(r, "open_interest", "openInterest", "oi")

            out.append(
                Contract(
                    symbol=sym,
                    underlying=ticker,
                    strike=strike,
                    expiry=pick(r, "expiration_date", "expirationDate", "expiry",
                                default=(parsed.expiry if parsed else expiry)),
                    bid=fnum(quote, "bp", "bid_price", "bidPrice", "bid"),
                    ask=fnum(quote, "ap", "ask_price", "askPrice", "ask"),
                    # None means "not reported", which is different from zero.
                    # The snapshot feed omits open interest entirely.
                    open_interest=int(oi) if oi is not None else None,
                    delta=fnum(greeks, "delta"),
                    iv=(fnum(r, "impliedVolatility", "implied_volatility", "iv")
                        or fnum(greeks, "iv")),
                )
            )
        return out

    async def refresh_quotes(self, contracts: Iterable[Contract]) -> None:
        """Top up bid/ask/Greeks from the snapshot endpoint for contracts whose
        chain entry came back thin. Mutates in place."""
        for c in contracts:
            if c.bid is not None and c.ask is not None and c.delta is not None:
                continue
            try:
                raw = await self.mcp.call("option_snapshot", symbol=c.symbol)
            except Exception:  # noqa: BLE001
                continue
            snap = raw.get(c.symbol) if isinstance(raw, dict) and c.symbol in raw else raw
            if not isinstance(snap, dict):
                continue
            quote = snap.get("latest_quote") or snap.get("quote") or snap
            greeks = snap.get("greeks") or {}
            c.bid = c.bid if c.bid is not None else fnum(quote, "bid_price", "bp", "bid")
            c.ask = c.ask if c.ask is not None else fnum(quote, "ask_price", "ap", "ask")
            c.delta = c.delta if c.delta is not None else fnum(greeks, "delta")
            c.iv = c.iv if c.iv is not None else fnum(snap, "implied_volatility", "iv")
