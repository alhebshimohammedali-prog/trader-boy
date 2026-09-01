"""The cycle loop. Runs layers 1-10 in order, every CYCLE_SECONDS.

Order matters and is not the build order: state is re-read from Alpaca before
anything else decides, gates run before the model sees a candidate, and
reconciliation runs after execution against the broker's account of events
rather than ours.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import config
from src import allocation, gates, signal as signal_mod
from src.data import Contract, Data, parse_occ
from src.decide import combine, critique, decide, narrate
from src.execution import Executor
from src.logbook import Logbook
from src.mcp_client import AlpacaMCP
from src.reconcile import Reconciler
from src.state import State


def now_et() -> datetime:
    return datetime.now(config.ET)


def _minutes_to_next_macro(now: datetime) -> int | None:
    upcoming = [w for w, _n in config.MACRO_EVENTS if w > now]
    if not upcoming:
        return None
    return int((min(upcoming) - now).total_seconds() / 60)


def market_open(now: datetime) -> bool:
    if now.date().isoformat() not in config.TRADING_DAYS:
        return False
    return config.SESSION_OPEN <= now.time() <= config.LAST_CYCLE


def pick_contract(chain: list[Contract], spot: float | None) -> tuple[Contract | None, str]:
    """Delta is primary; percentage moneyness is the fallback.

    Black-Scholes is deliberately NOT the fallback: it needs the implied vol
    Alpaca failed to solve, which is exactly why delta was null. Computing a
    delta from a guessed sigma would be fabricating the number we claim to
    lack. Percentage moneyness needs no inputs we do not have.
    """
    puts = [c for c in chain if c.bid and c.bid > 0]
    if not puts:
        return None, "no contract with a live bid"

    with_delta = [c for c in puts if c.delta is not None]
    banded = [c for c in with_delta if config.DELTA_MIN <= abs(c.delta) <= config.DELTA_MAX]
    if banded:
        # Low end of the band, not the middle -- see config.DELTA_TARGET.
        best = min(banded, key=lambda c: abs(abs(c.delta) - config.DELTA_TARGET))
        return best, f"delta {best.delta:.3f} (target {config.DELTA_TARGET})"

    if spot:
        lo = spot * (1 - config.MONEYNESS_MAX)
        hi = spot * (1 - config.MONEYNESS_MIN)
        window = [c for c in puts if lo <= c.strike <= hi]
        # The fallback exists for contracts whose delta the feed could not
        # solve. It must not become a way around the delta band for contracts
        # whose delta is known and out of it. A universe scan surfaced this:
        # AAL had deltas on every strike, none inside 0.16-0.30, and the
        # moneyness window happily returned a 0.40-delta put -- more than twice
        # the assignment risk the band exists to cap, selected by a path meant
        # to handle MISSING data rather than unwelcome data.
        window = [c for c in window if c.delta is None
                  or config.DELTA_MIN <= abs(c.delta) <= config.DELTA_MAX]
        if window:
            target = spot * (1 - config.MONEYNESS_TARGET)
            best = min(window, key=lambda c: abs(c.strike - target))
            otm = (spot - best.strike) / spot
            return best, f"moneyness {otm:.2%} OTM (delta unavailable)"

    return None, "no contract in delta band or moneyness window"


class Agent:
    def __init__(self, mcp: AlpacaMCP, log: Logbook, *, place_orders: bool = True,
                 rescan=None):
        self.mcp = mcp
        self.data = Data(mcp)
        self.exec = Executor(mcp)
        self.recon = Reconciler(self.data)
        self.log = log
        self.state = State.load()
        self.place_orders = place_orders
        # Optional async callable that rebuilds the universe. None means the
        # universe is fixed, which is what --no-scan and the offline fixtures
        # want.
        self.rescan = rescan
        self._scanned_at = None

    # -------------------------------------------------------------- cycle --

    async def run_cycle(self) -> dict:
        now = now_et()
        record: dict[str, Any] = {"cycle": None, "account": self.mcp.account}

        # 1-2. Broker truth first. Never decide from disk.
        acct = await self.data.account()
        self.state.begin_cycle(now, acct.equity)
        record["cycle"] = self.state.cycle
        record["equity"] = acct.equity
        record["buying_power"] = acct.buying_power

        # 9 (early). Diff positions to detect assignment; activities lag a day.
        delta, positions = await self.recon.diff_positions()
        record["reconciliation"] = {"summary": delta.summary(),
                                    "assigned": delta.assigned}
        if delta.assigned:
            self.log.note(f"  !! assignment detected on {', '.join(delta.assigned)}")

        deployed = 0.0
        unparsed: list[str] = []
        for p in positions:
            if not (p.is_option and p.qty < 0):
                continue
            occ = parse_occ(p.symbol)
            if occ is None:
                unparsed.append(p.symbol)
                continue
            deployed += abs(p.qty) * occ.collateral

        if unparsed:
            # Cannot price these into the portfolio cap, so gate 6 would be
            # enforcing against an understated number. Halt entries rather than
            # deploy against a figure we know is wrong.
            record["unparsed_positions"] = unparsed
            self.state.trip_breaker(f"unparseable position symbols: {unparsed}")
            self.log.note(f"  !! cannot parse {len(unparsed)} position symbol(s); "
                          f"entries halted rather than mis-cap the portfolio")

        record["deployed_pct"] = deployed / acct.equity if acct.equity else 0.0
        record["drawdown"] = self.state.drawdown(acct.equity)

        # Gate 5. Halting entries is not enough -- also reduce exposure.
        if record["drawdown"] >= config.DRAWDOWN_LIMIT and not self.state.breaker_tripped:
            self.state.trip_breaker(f"drawdown {record['drawdown']:.2%}")
            self.log.note(f"  !! CIRCUIT BREAKER {self.state.breaker_reason}")
            closed = await self._close_largest(positions)
            record["breaker_action"] = closed

        if not acct.tradable:
            return await self._finish(record, "account not tradable (level/blocked/equity)")
        if now > config.ENTRY_CUTOFF:
            return await self._finish(record, "past entry cutoff; managing only")

        # 3. Signal.
        raw: dict[str, dict] = {}
        chains: dict[str, list[Contract]] = {}
        for ticker in config.UNIVERSE_CANDIDATES:
            if ticker in config.EARNINGS_EXCLUDED:
                continue
            try:
                chain = await self.data.put_chain(ticker, config.TARGET_EXPIRY)
            except Exception as exc:  # noqa: BLE001
                self.log.note(f"  chain error {ticker}: {str(exc)[:80]}")
                continue
            if not chain:
                continue
            chains[ticker] = chain
            spot = await self.data.spot(ticker)
            atm = min(chain, key=lambda c: abs(c.strike - (spot or c.strike)))
            raw[ticker] = {"iv": atm.iv, "bars": await self.data.bars(ticker, 40),
                           "spot": spot}

        signals = signal_mod.build(raw, self.state.iv_history)
        signal_mod.record_iv(self.state.iv_history, signals)
        eligible = signal_mod.eligible(signals)
        record["signals"] = [s.row() for s in signals]
        record["eligible"] = [s.ticker for s in eligible]

        if not eligible:
            return await self._finish(record, "no ticker cleared the signal floor")

        # 4. Gates -- before the model sees anything.
        held = {p.symbol for p in positions if p.is_option}
        runnable: list[tuple[Contract, float]] = []
        gate_rows: list[dict] = []
        for s in eligible:
            spot = raw[s.ticker]["spot"]
            contract, how = pick_contract(chains[s.ticker], spot)
            if contract is None:
                gate_rows.append({"ticker": s.ticker, "passed": False, "reason": how})
                continue
            await self.data.refresh_quotes([contract])

            # What this underlying has actually done at this distance, over
            # this holding period. Reuses the bars the signal layer already
            # fetched, so it costs no extra call. None when spot is unknown or
            # the strike is not OTM -- an ITM strike is gate 4's business, and
            # a negative moneyness would make the rate meaningless.
            hist = None
            if spot and spot > 0 and contract.strike < spot:
                bars = (raw.get(s.ticker) or {}).get("bars") or []
                hist = signal_mod.empirical_itm_rate(
                    bars, (spot - contract.strike) / spot,
                    max(allocation.dte(contract, date.today()), 1))

            g = gates.evaluate(
                contract, now=now, equity=acct.equity, spot=spot,
                deployed_collateral=deployed,
                breaker_tripped=self.state.breaker_tripped, held_symbols=held,
                empirical_itm=hist,
            )
            gate_rows.append({"ticker": s.ticker, "symbol": contract.symbol,
                              "passed": g.passed, "reason": g.reason, "strike_via": how})
            if g.passed:
                self.state.mark_qualified(s.ticker)
                runnable.append((contract, s.score))
        record["gate_results"] = gate_rows

        if not runnable:
            return await self._finish(record, "no candidate passed all gates")

        # 5. Allocation.
        # Variance risk premium per ticker, from the signal layer's own
        # measurements. This is what the allocator's reward term ranks on.
        edge = {s.ticker: s.iv - s.realized_vol
                for s in signals
                if s.iv is not None and s.realized_vol is not None}

        # Correlation of each candidate against the underlyings already held.
        # Uses the same bars the signal layer fetched this cycle, so this costs
        # no extra calls. Held names whose bars we do not have contribute
        # nothing rather than a assumed zero.
        held_roots = {parse_occ(p.symbol).root for p in positions
                      if p.is_option and parse_occ(p.symbol)}
        crowd: dict[str, float] = {}
        if held_roots:
            rets = {t: signal_mod.log_returns(v.get("bars") or [])
                    for t, v in raw.items()}
            for cand in rets:
                if cand in held_roots:
                    continue
                cs = [signal_mod.correlation(rets[cand], rets[h])
                      for h in held_roots if h in rets]
                cs = [c for c in cs if c is not None]
                if cs:
                    # Worst case, not average: the risk that matters is the
                    # single holding this would move with, and averaging that
                    # against uncorrelated names hides it.
                    crowd[cand] = max(cs)

        winner, scored = allocation.select(runnable, self.state, acct.equity,
                                           date.today(), edge=edge, crowd=crowd)
        record["runnable_table"] = [s.row() for s in scored]
        record["dte"] = allocation.dte(winner.contract, date.today())

        # 6. The model. One candidate, no tools, veto or shrink only.
        candidate = {
            "ticker": winner.contract.underlying,
            "contract": {
                "symbol": winner.contract.symbol, "strike": winner.contract.strike,
                "expiry": winner.contract.expiry, "dte": record["dte"],
                "bid": winner.contract.bid, "ask": winner.contract.ask,
                "open_interest": winner.contract.open_interest,
                "delta": winner.contract.delta, "iv": winner.contract.iv,
            },
            "signal": next((s.row() for s in signals if s.ticker == winner.contract.underlying), {}),
            "allocation": winner.row(),
            "sizing": {"collateral": winner.contract.collateral,
                       "pct_equity": winner.contract.collateral / acct.equity},
            "portfolio": {"equity": acct.equity, "deployed_pct": record["deployed_pct"],
                          "open_positions": len(held)},
            # Only decision-relevant time deltas -- deliberately NOT the wall
            # clock or the cycle number. Live-agent research documents "cadence
            # trading", where a model reads its own polling interval as a
            # signal. The prompt forbids it, but not handing over the raw
            # timestamp removes the temptation structurally, which is the same
            # reasoning as putting the gates ahead of the model.
            "clock": {
                "hours_to_mark": round(
                    (config.MARK_AT - now).total_seconds() / 3600, 1),
                "hours_to_entry_cutoff": round(
                    (config.ENTRY_CUTOFF - now).total_seconds() / 3600, 1),
                "minutes_to_next_macro_event": _minutes_to_next_macro(now),
            },
        }
        verdict = await decide(candidate)
        record["first_pass"] = verdict.row()

        # Second pass argues against it. Veto/shrink only; combine() keeps
        # whichever verdict is more conservative, never the looser one.
        second = await critique(candidate, verdict)
        if second is not None:
            record["critique"] = second.row()
            verdict = combine(verdict, second)
        record["decision"] = verdict.row()

        if not verdict.approved:
            return await self._finish(record, f"LLM {verdict.action}: {verdict.reasoning[:160]}")

        # 8. Execution.
        qty = max(1, int(config.CONTRACTS_PER_ORDER * verdict.size_multiplier))
        if not self.place_orders:
            return await self._finish(record, "dry run: order not submitted")

        if self.state.orders_today >= config.MAX_ORDERS_PER_DAY:
            self.state.trip_breaker(
                f"order cap {config.MAX_ORDERS_PER_DAY} reached this session")
            return self._finish(
                record,
                f"daily order cap reached ({self.state.orders_today}); "
                "something is looping -- entries halted")

        self.state.orders_today += 1
        fill = await self.exec.sell_put(winner.contract, self.state.cycle, qty)
        record["fill"] = {**fill.__dict__, "slippage": fill.slippage}

        # 9. Reconcile the fill against Alpaca, not against our own record.
        check = await self.recon.verify_fill(fill, self.exec)
        record["reconciliation"]["fill_check"] = check.__dict__

        if check.broker_filled > 0:
            self.state.charge_capital(
                winner.contract.underlying, winner.contract.collateral,
                acct.equity, record["dte"],
            )
        return await self._finish(record, None)

    # ------------------------------------------------------------- helpers --

    async def _close_largest(self, positions) -> dict:
        """Circuit breaker must reduce exposure, not merely stop adding to it.

        Close the highest-DELTA short, not the highest market value. For short
        puts those are different positions and only the first one matters: a
        deep-ITM put is the one whose delta is approaching 1.0, whose gamma is
        about to hurt, and which is the actual early-assignment candidate. A
        far-OTM put can carry a larger notional and be nearly inert.

        Alpaca does not return Greeks on positions, so we rank by moneyness
        ((strike - spot) / spot), which is monotone in |delta| for puts at a
        common expiry. If spot is unavailable we fall back to market value and
        say so in the log rather than silently ranking on the wrong axis.
        """
        shorts = [p for p in positions if p.is_option and p.qty < 0]
        if not shorts:
            return {"closed": None, "note": "no short options to close"}

        ranked: list[tuple[float, Any, str]] = []
        for p in shorts:
            occ = parse_occ(p.symbol)
            root = (occ.root if occ else None) or p.underlying
            # fresh=True: never rank danger on a cached price. The breaker only
            # fires in a fast market, which is when a stale quote is wrongest.
            spot = await self.data.spot(root, fresh=True) if root else None
            if occ and spot:
                ranked.append(((occ.strike - spot) / spot, p, "moneyness"))
            else:
                # Fallback keeps the breaker functional but is a weaker proxy.
                ranked.append((-1e9 + abs(p.market_value or 0.0), p, "market_value"))

        moneyness, target, basis = max(ranked, key=lambda t: t[0])
        f = await self.exec.buy_to_close(target.symbol, int(abs(target.qty)),
                                         self.state.cycle)
        return {
            "closed": target.symbol,
            "ranked_by": basis,
            "moneyness": round(moneyness, 4) if basis == "moneyness" else None,
            "status": f.status,
            "filled": f.filled_qty,
        }

    @staticmethod
    def _summary(record: dict) -> dict:
        """Compact view for the narrator. The full record carries big tables
        the model does not need and would pad the prompt with."""
        gates = record.get("gate_results") or []
        table = record.get("runnable_table") or []
        fill = record.get("fill") or {}
        return {
            "cycle": record.get("cycle"),
            "equity": record.get("equity"),
            "deployed_pct": record.get("deployed_pct"),
            "drawdown": record.get("drawdown"),
            "candidates_scored": len(record.get("signals") or []),
            "eligible": record.get("eligible"),
            "gate_failures": [
                {"ticker": g.get("ticker"), "reason": g.get("reason")}
                for g in gates if not g.get("passed")
            ][:8],
            "runnable_count": len(table),
            "selected": next((r.get("ticker") for r in table if r.get("selected")), None),
            "decision": record.get("decision"),
            "critique": record.get("critique"),
            "order": {"status": fill.get("status"),
                      "filled": fill.get("filled_qty"),
                      "price": fill.get("fill_price")} if fill else None,
            "no_trade_reason": record.get("no_trade_reason"),
            "breaker_tripped": bool(record.get("breaker_action")),
        }

    async def flatten(self) -> dict:
        """Emergency exit: close every short option position, now.

        Deliberately NOT part of any automatic path. Holding to the mark is
        correct almost always -- unrealised P&L counts and closing pays the
        spread -- so an agent that can flatten itself will eventually flatten
        itself for a bad reason. This exists for the human, via `--flatten`,
        for the case where something is wrong that the breaker did not catch.
        """
        positions = await self.data.positions()
        shorts = [p for p in positions if p.is_option and p.qty < 0]
        if not shorts:
            self.log.note("flatten: no short option positions open")
            return {"closed": [], "note": "nothing to close"}

        self.log.note(f"flatten: closing {len(shorts)} position(s)")
        results = []
        for p in shorts:
            f = await self.exec.buy_to_close(p.symbol, int(abs(p.qty)),
                                             self.state.cycle)
            self.log.note(f"  {p.symbol}: {f.status} filled {f.filled_qty}")
            results.append({"symbol": p.symbol, "status": f.status,
                            "filled": f.filled_qty})
        self.state.trip_breaker("manual flatten")
        self.state.save()
        return {"closed": results}

    async def _finish(self, record: dict, no_trade: str | None) -> dict:
        if no_trade:
            record["no_trade_reason"] = no_trade
        record["narrative"] = await narrate(self._summary(record))
        self.state.save()
        self.log.cycle(record)
        return record

    # ---------------------------------------------------------------- run --

    async def _maybe_rescan(self, now) -> None:
        """Rebuild the universe when the old one was chosen under conditions
        that no longer hold.

        The scan used to run once, at startup. Start the agent before the open
        and it scanned on after-hours quotes -- where spreads run 25-30% and
        almost everything fails the liquidity gate -- then traded that thin,
        badly-priced universe for the rest of the week without ever looking
        again. The market it picked from was not the market it was trading in.

        So: rescan on the first cycle of every session, and periodically
        through the day, because which names are worth selling puts on is not
        a fact you establish once.
        """
        if self.rescan is None:
            return
        stale = (self._scanned_at is None
                 or (now - self._scanned_at).total_seconds()
                 > config.SCAN_REFRESH_SECONDS
                 or self._scanned_at.date() != now.date())
        if stale:
            await self.rescan()
            self._scanned_at = now

    async def run_forever(self) -> None:
        while True:
            now = now_et()
            if not market_open(now):
                if now > config.MARK_AT:
                    self.log.note("past the mark; nothing further counts. stopping.")
                    return
                self.log.note(f"{now:%Y-%m-%d %H:%M} ET - market closed, waiting")
                await asyncio.sleep(60)
                continue
            try:
                # Only ever while the market is open, so the quotes it ranks on
                # are the quotes it would trade at.
                await self._maybe_rescan(now)
                await self.run_cycle()
            except Exception as exc:  # noqa: BLE001
                # A cycle must never kill the run. Log and try again next tick.
                self.log.cycle({"cycle": self.state.cycle,
                                "error": f"{type(exc).__name__}: {exc}",
                                "no_trade_reason": "cycle raised"})
            await asyncio.sleep(config.CYCLE_SECONDS)
