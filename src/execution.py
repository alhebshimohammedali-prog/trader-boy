"""Layer 8: execution. The piece that decides whether anything else matters.

Three platform facts drive every choice here (§7):

1. Paper only fills MARKETABLE orders. A sell limit at mid never fills. For a
   short put we must price AT or THROUGH the bid. This is the single most
   common cause of a competition week with zero trades.

2. There are no bracket/OTO orders for options, so every exit is agent-driven.

3. `client_order_id` is our idempotency key. On restart we ask Alpaca whether
   an order already exists under this cycle's ID BEFORE placing anything --
   Alpaca is the source of truth, our state file is a cache that must converge
   to it. Without this, a crash-restart double-orders.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import config
from src.data import Contract, fnum, pick
from src.mcp_client import AlpacaMCP

TERMINAL_OK = {"filled"}
TERMINAL_BAD = {"canceled", "cancelled", "expired", "rejected", "suspended", "stopped"}


@dataclass
class Fill:
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    status: str
    requested_qty: int
    filled_qty: int
    limit_price: float | None
    fill_price: float | None
    quote_mid: float | None
    attempts: int = 1
    dropped_args: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def slippage(self) -> float | None:
        """Fill vs the mid we priced from. Negative = we gave up edge.
        We quote from the indicative feed but fill against real NBBO, so
        this is logged, not asserted (§7)."""
        if self.fill_price is None or self.quote_mid is None:
            return None
        return self.fill_price - self.quote_mid


def client_order_id(cycle: int, contract: Contract) -> str:
    """Deterministic per (cycle, contract) so a restart reconstructs it and
    can ask Alpaca 'did I already send this?'. Max 128 chars."""
    return f"aw-c{cycle:04d}-{contract.underlying}-{int(contract.strike * 100)}p"[:128]


def sell_limit_price(contract: Contract) -> float | None:
    """At or through the bid. Never mid -- mid does not fill on paper."""
    if contract.bid is None or contract.bid <= 0:
        return None
    price = contract.bid - config.LIMIT_OFFSET_FROM_BID
    return max(round(price, 2), 0.01)


class Executor:
    def __init__(self, mcp: AlpacaMCP):
        self.mcp = mcp

    # -- idempotency ---------------------------------------------------------

    async def existing_order(self, coid: str) -> dict | None:
        """Ask Alpaca whether this client_order_id already exists."""
        if "order_by_client_id" not in self.mcp.resolved:
            return None
        try:
            raw = await self.mcp.call("order_by_client_id", client_order_id=coid)
        except Exception:  # noqa: BLE001  -- not-found surfaces as an error
            return None
        return raw if isinstance(raw, dict) and pick(raw, "id", "order_id") else None

    # -- placement -----------------------------------------------------------

    async def _submit(self, contract: Contract, qty: int, limit: float, coid: str):
        """Build args from the tool's own schema, not the REST shape.

        qty and limit_price go as STRINGS. The MCP tool validates them with
        pydantic and rejects numbers outright:

            2 validation errors for call[place_option_order]
            qty          Input should be a valid string  [input_value=1]
            limit_price  Input should be a valid string  [input_value=1482.4]

        This is not discoverable from the schema -- place_option_order declares
        no properties at all, so fit_args passes everything through untouched
        and nothing type-checks it on the way out. It was only found by sending
        a deliberately unfillable order against the live endpoint.

        Worse, the rejection is RETURNED rather than raised: the call comes
        back as a string of error text with no exception, so the executor would
        have carried on to poll for an order that was never created. Four days
        of cycles would have placed zero trades and logged nothing that looked
        like a failure.
        """
        # Canonical names only. We used to send aliases alongside them --
        # option_symbol beside symbol, quantity beside qty, order_type beside
        # type -- as insurance against a server that spelled them differently.
        # This tool rejects unknown keyword arguments outright, and because it
        # publishes no schema, fit_args had nothing to filter them against, so
        # the insurance was itself the failure.
        args, dropped = self.mcp.fit_args(
            "place_option_order",
            symbol=contract.symbol,
            side="sell",
            qty=str(qty),
            type="limit",
            time_in_force="day",
            limit_price=f"{limit:.2f}",
            client_order_id=coid,
            # Options-specific: selling to open is not the same as closing.
            position_intent="sell_to_open",
        )
        resp = await self.mcp.call("place_option_order", **args)

        # If a future server version renames or drops a field, strip exactly
        # what it named and retry once, rather than failing the session over a
        # keyword. Bounded to one retry so a genuine rejection still surfaces.
        if isinstance(resp, str) and "unexpected_keyword_argument" in resp:
            unexpected = {ln.strip() for ln in resp.splitlines()
                          if ln.strip() and ln.strip() in args}
            if unexpected:
                for k in unexpected:
                    args.pop(k, None)
                dropped = list(dropped) + sorted(unexpected)
                resp = await self.mcp.call("place_option_order", **args)

        # A validation failure arrives as plain text, not an exception. Turn it
        # into one so it cannot be mistaken for an order that exists.
        if isinstance(resp, str) and "validation error" in resp.lower():
            raise RuntimeError(f"order rejected by tool validation: {resp[:300]}")
        return resp, dropped

    async def _poll(self, coid: str, timeout: int) -> dict | None:
        """Poll for terminal order state with exponential backoff.

        A marketable order either fills near-instantly or is not going to.
        A fixed 3s interval was both too slow to notice the first case and
        too chatty for the second -- ~30 calls per order against a free-tier
        rate limit. Starting at 0.5s and backing off to 8s detects a fast fill
        six times sooner while roughly halving the call count.
        """
        deadline = time.monotonic() + timeout
        delay, last = 0.5, None
        while time.monotonic() < deadline:
            order = await self.existing_order(coid)
            if order:
                last = order
                status = str(pick(order, "status", default="")).lower()
                if status in TERMINAL_OK or status in TERMINAL_BAD:
                    return order
                if (fnum(order, "filled_qty", "filled_quantity", default=0) or 0) > 0:
                    return order
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 1.6, 8.0)
        return last

    async def _cancel(self, order: dict) -> None:
        oid = pick(order, "id", "order_id")
        if not oid or "cancel_order" not in self.mcp.resolved:
            return
        try:
            args, _ = self.mcp.fit_args("cancel_order", order_id=oid, id=oid)
            await self.mcp.call("cancel_order", **args)
        except Exception:  # noqa: BLE001
            pass

    async def sell_put(self, contract: Contract, cycle: int, qty: int | None = None) -> Fill:
        """Sell to open, priced to fill. Reprices through the bid on timeout."""
        self.mcp.assert_paper()
        qty = qty or config.CONTRACTS_PER_ORDER
        coid = client_order_id(cycle, contract)
        mid = contract.mid

        # Crash recovery: never send twice under the same intent.
        prior = await self.existing_order(coid)
        if prior:
            return self._to_fill(prior, coid, contract, qty, mid, attempts=0,
                                 note="pre-existing order; not resent")

        limit = sell_limit_price(contract)
        if limit is None:
            return Fill(coid, None, contract.symbol, "no_bid", qty, 0, None, None, mid,
                        attempts=0, note="bid is zero or missing -- unpriceable")

        dropped: list[str] = []
        order: dict | None = None
        for attempt in range(1, config.MAX_REPRICE_ATTEMPTS + 2):
            _, dropped = await self._submit(contract, qty, limit, coid)
            order = await self._poll(coid, config.ORDER_TIMEOUT_SECONDS)

            filled = fnum(order or {}, "filled_qty", "filled_quantity", default=0) or 0
            status = str(pick(order or {}, "status", default="unknown")).lower()
            if filled >= qty or status in TERMINAL_BAD:
                break
            if attempt > config.MAX_REPRICE_ATTEMPTS:
                break

            # Unfilled at the bid: step through it and try again under a new
            # id, so the idempotency check stays meaningful.
            await self._cancel(order or {})
            limit = max(round(limit - 0.01, 2), 0.01)
            coid = f"{client_order_id(cycle, contract)}-r{attempt}"[:128]

        f = self._to_fill(order or {}, coid, contract, qty, mid, attempts=attempt)
        f.dropped_args = dropped
        return f

    # -- normalisation -------------------------------------------------------

    @staticmethod
    def _to_fill(order: dict, coid: str, contract: Contract, qty: int,
                 mid: float | None, attempts: int, note: str = "") -> Fill:
        filled = int(fnum(order, "filled_qty", "filled_quantity", default=0) or 0)
        return Fill(
            client_order_id=coid,
            broker_order_id=pick(order, "id", "order_id"),
            symbol=contract.symbol,
            status=str(pick(order, "status", default="unknown")).lower(),
            requested_qty=qty,
            filled_qty=filled,
            limit_price=fnum(order, "limit_price"),
            fill_price=fnum(order, "filled_avg_price", "average_price", "avg_fill_price"),
            quote_mid=mid,
            attempts=attempts,
            note=note,
        )

    # -- exits (gate 4 / gate 5) --------------------------------------------

    async def buy_to_close(self, symbol: str, qty: int, cycle: int,
                           ask: float | None = None) -> Fill:
        """Force-close. Marketable means AT or THROUGH the ask when buying."""
        self.mcp.assert_paper()
        coid = f"aw-c{cycle:04d}-close-{symbol}"[:128]
        prior = await self.existing_order(coid)
        if prior:
            stub = Contract(symbol=symbol, underlying="", strike=0.0, expiry="")
            return self._to_fill(prior, coid, stub, qty, ask, attempts=0,
                                 note="pre-existing close order")

        args, dropped = self.mcp.fit_args(
            "place_option_order",
            symbol=symbol, option_symbol=symbol,
            side="buy", qty=qty, quantity=qty,
            type="market" if ask is None else "limit",
            order_type="market" if ask is None else "limit",
            limit_price=None if ask is None else round(ask + 0.01, 2),
            time_in_force="day",
            client_order_id=coid,
            position_intent="buy_to_close",
        )
        await self.mcp.call("place_option_order", **args)
        order = await self._poll(coid, config.ORDER_TIMEOUT_SECONDS)
        stub = Contract(symbol=symbol, underlying="", strike=0.0, expiry="")
        f = self._to_fill(order or {}, coid, stub, qty, ask, attempts=1)
        f.dropped_args = dropped
        return f
