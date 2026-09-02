"""Layer 9: reconciliation.

Two platform facts shape this file (§7):

1. Non-trade activities sync NEXT DAY on paper. An assignment, exercise or
   expiry that happened today will not appear in the activities feed until
   tomorrow -- and tomorrow is often after the mark. So we detect assignment by
   DIFFING POSITIONS between cycles, never by reading activities. There is
   also no websocket for assignments at all; REST polling is the only channel.

2. Paper randomly partial-fills roughly 10% of eligible orders. Reconciliation
   must account for the remainder without re-sending. (At one contract per
   order a partial is arithmetically impossible, but the code stays honest so
   raising CONTRACTS_PER_ORDER does not silently introduce a double-order bug.)

The rule everywhere: believe Alpaca, not our own record of what we intended.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data import Data, Position
from src.execution import Fill


@dataclass
class PositionDelta:
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    resized: list[tuple[str, float, float]] = field(default_factory=list)
    assigned: list[str] = field(default_factory=list)
    # The prior Position objects for everything in `closed`. Kept because once
    # a position leaves the book the broker will not tell us what it was worth,
    # and the ledger needs its last mark to record an outcome at all.
    closed_positions: dict = field(default_factory=dict)

    @property
    def quiet(self) -> bool:
        return not (self.opened or self.closed or self.resized or self.assigned)

    def summary(self) -> str:
        if self.quiet:
            return "no position change"
        bits = []
        if self.opened:
            bits.append(f"opened {len(self.opened)}")
        if self.closed:
            bits.append(f"closed {len(self.closed)}")
        if self.resized:
            bits.append(f"resized {len(self.resized)}")
        if self.assigned:
            bits.append(f"ASSIGNED {','.join(self.assigned)}")
        return "; ".join(bits)


@dataclass
class FillCheck:
    """What we believe vs what Alpaca says. Divergence is the interesting case."""
    client_order_id: str
    believed_filled: int
    broker_filled: int
    broker_status: str
    diverged: bool
    note: str = ""


def _underlying_of(symbol: str) -> str:
    """OCC symbols are ROOT + YYMMDD + C/P + strike. The root is the leading
    alphabetic run."""
    root = []
    for ch in symbol:
        if ch.isalpha():
            root.append(ch)
        else:
            break
    return "".join(root)


class Reconciler:
    def __init__(self, data: Data):
        self.data = data
        self._prev: dict[str, Position] = {}
        self._seeded = False

    def seed(self, positions: list[Position]) -> None:
        """Establish a baseline without reporting the whole book as 'opened'."""
        self._prev = {p.symbol: p for p in positions}
        self._seeded = True

    async def diff_positions(self) -> tuple[PositionDelta, list[Position]]:
        current = await self.data.positions()
        by_symbol = {p.symbol: p for p in current}
        delta = PositionDelta()

        if not self._seeded:
            self.seed(current)
            return delta, current

        prev_syms, cur_syms = set(self._prev), set(by_symbol)

        delta.opened = sorted(cur_syms - prev_syms)
        delta.closed = sorted(prev_syms - cur_syms)
        delta.closed_positions = {s: self._prev[s] for s in delta.closed}
        for sym in sorted(prev_syms & cur_syms):
            before, after = self._prev[sym].qty, by_symbol[sym].qty
            if before != after:
                delta.resized.append((sym, before, after))

        # Assignment signature: a short option leaves the book in the same
        # cycle an equity position in its underlying appears or grows.
        vanished_puts = [
            s for s in delta.closed
            if self._prev[s].is_option and self._prev[s].qty < 0
        ]
        for opt in vanished_puts:
            root = _underlying_of(opt)
            grew = any(
                (not by_symbol[s].is_option)
                and s == root
                and by_symbol[s].qty > (self._prev.get(s).qty if s in self._prev else 0)
                for s in cur_syms
            )
            if grew:
                delta.assigned.append(root)

        self._prev = by_symbol
        return delta, current

    async def verify_fill(self, fill: Fill, executor) -> FillCheck:
        """Re-read the order from Alpaca and compare against what we recorded."""
        order = await executor.existing_order(fill.client_order_id)
        if order is None:
            return FillCheck(
                fill.client_order_id, fill.filled_qty, 0, "not_found",
                diverged=fill.filled_qty > 0,
                note="we recorded a fill Alpaca has no record of -- trust Alpaca",
            )

        from src.data import fnum, pick  # local import: avoids a cycle

        broker_filled = int(fnum(order, "filled_qty", "filled_quantity", default=0) or 0)
        status = str(pick(order, "status", default="unknown")).lower()
        diverged = broker_filled != fill.filled_qty

        note = ""
        if diverged:
            note = f"believed {fill.filled_qty}, broker says {broker_filled}"
        elif 0 < broker_filled < fill.requested_qty:
            note = (
                f"partial: {broker_filled}/{fill.requested_qty} -- "
                "remainder NOT re-sent this cycle"
            )

        return FillCheck(
            fill.client_order_id, fill.filled_qty, broker_filled, status,
            diverged=diverged, note=note,
        )
