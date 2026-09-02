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
from src import allocation, exits, gates, ledger, signal as signal_mod
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
        self._consecutive_failures = 0

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

        # Anything that left the book without us buying it back: expiry,
        # assignment, or a manual close. Recorded from the LAST mark we saw,
        # and labelled an estimate, because there is no exit fill to measure
        # against. Agent-initiated closes below record the real number.
        for sym, prior in (delta.closed_positions or {}).items():
            if not (prior.is_option and prior.qty < 0):
                continue
            occ = parse_occ(sym)
            if occ is None:
                continue
            ledger.record_close(
                symbol=sym, ticker=occ.root, strike=occ.strike,
                expiry=occ.expiry, qty=int(abs(prior.qty)),
                closed_by=ledger.VANISHED, cycle=self.state.cycle,
                last_unrealized_pl=prior.unrealized_pl,
                dte=exits.dte_from_expiry(occ.expiry, now.date()),
                note=("assigned" if occ.root in delta.assigned
                      else "expired or closed outside the agent"))

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

        # Rebuild the allocator's memory from positions the broker reports, if
        # the state file did not survive.
        #
        # ubt is what stops a ticker winning twice in a row, and it lived only
        # in state.json. Lose that file -- a crash mid-write, a stray delete,
        # a restart onto a clean checkout -- and the agent forgets which names
        # already consumed capital while still holding them, so it ranks them
        # as though they were fresh. The positions were in the account the
        # whole time; there is no reason to depend on a local file for
        # something the broker can be asked.
        #
        # Only seeds what is MISSING, so a healthy state file always wins: the
        # real ubt accumulates across cycles and is more accurate than this
        # one-shot reconstruction from current holdings.
        if positions and acct.equity > 0:
            seeded = []
            for p in positions:
                if not (p.is_option and p.qty < 0):
                    continue
                occ = parse_occ(p.symbol)
                if occ is None or self.state.ubt(occ.root) > 0:
                    continue
                y, mo, d = (int(x) for x in occ.expiry.split("-"))
                days = max((date(y, mo, d) - now.date()).days, 1)
                self.state.charge_capital(
                    occ.root, abs(p.qty) * occ.collateral, acct.equity, days)
                seeded.append(occ.root)
            if seeded:
                self.log.note(f"  state rebuilt from broker positions: "
                              f"{', '.join(seeded)} (ubt was missing)")

        # The two risk controls that lived ONLY on disk.
        #
        # orders_today caps entries at MAX_ORDERS_PER_DAY, and day_high_water
        # is what the drawdown breaker measures against. Losing state.json
        # reset both: the cap started over, and the breaker's reference became
        # whatever equity happened to be at that moment -- so after a drop it
        # would measure the drawdown from the bottom and fire late, exactly
        # when it is most needed. A control that silently weakens on restart is
        # worse than one that is absent, because the log still claims it.
        #
        # Both are answerable from the broker, so neither needs to depend on a
        # local file surviving.
        if self.state.orders_today == 0:
            counted = await self._orders_placed_today(now)
            if counted:
                self.state.orders_today = counted
                self.log.note(f"  daily order count rebuilt from broker: {counted}")

        # last_equity is the previous session's close. If the high-water mark
        # was lost we cannot recover the intraday peak, but starting from
        # yesterday's close is the conservative floor -- it can only make the
        # breaker fire sooner, never later.
        prior = float(getattr(acct, "last_equity", 0) or 0)
        if prior > self.state.day_high_water:
            self.state.day_high_water = prior
            record["high_water_rebuilt"] = prior
            # Recompute against the mark we just corrected. `drawdown` was
            # measured near the top of this method, while day_high_water was
            # still whatever begin_cycle() reset it to -- on the first cycle of
            # a session that is current equity, which makes the drawdown zero by
            # construction. Leaving the stale figure lets the gate below test
            # 0.0 on exactly the cycle this rebuild exists to protect: a session
            # that opens BELOW the previous close.
            #
            # Seen live, cycle 10 on 2 Sep -- drawdown logged as 0.0 alongside
            # high_water_rebuilt 99,805.53 at equity 99,627.53, a true drawdown
            # of 0.18% reported as none. Harmless at that size. On a gap-down
            # open the breaker would sit out the first cycle of the day and fire
            # fifteen minutes late, which is the one morning it matters.
            record["drawdown"] = self.state.drawdown(acct.equity)

        # Gate 5. Halting entries is not enough -- also reduce exposure.
        if record["drawdown"] >= config.DRAWDOWN_LIMIT and not self.state.breaker_tripped:
            self.state.trip_breaker(f"drawdown {record['drawdown']:.2%}")
            self.log.note(f"  !! CIRCUIT BREAKER {self.state.breaker_reason}")
            closed = await self._close_largest(positions)
            record["breaker_action"] = closed

        if not acct.tradable:
            return await self._finish(record, "account not tradable (level/blocked/equity)")

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

        # 4a. Manage what we already hold.
        #
        # Placed HERE, above every early return below it, because each of those
        # returns skips position management while claiming to do something else.
        # "past entry cutoff; managing only" managed nothing at all -- it
        # returned immediately -- so between the Thursday cutoff and the mark
        # the agent sat inert on a book of 1-DTE short puts, which is the exact
        # window where gamma makes management matter most. "no ticker cleared
        # the signal floor" had the same effect for a different reason: a quiet
        # market silenced the exits too.
        closed = await self._manage_book(positions, chains, record, now.date())
        if closed:
            # Entries wait a cycle. The gates below were about to be evaluated
            # against a deployed-capital figure that this close just changed,
            # and re-deriving it mid-cycle is more moving parts than the fifteen
            # minutes are worth.
            return await self._finish(
                record, f"closed {closed} position(s); entries resume next cycle")

        if now > config.ENTRY_CUTOFF:
            return await self._finish(record, "past entry cutoff; managing only")

        if not eligible:
            return await self._finish(record, "no ticker cleared the signal floor")

        # 4. Gates -- before the model sees anything.
        held = {p.symbol for p in positions if p.is_option}
        runnable: list[tuple[Contract, float]] = []
        cap_blocked: list[tuple[Contract, float]] = []
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
                    max(allocation.dte(contract, now.date()), 1))

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
            elif all(n == 6 for n, _name, _why in g.failures):
                # Rejected ONLY because the book is full: not a bad trade, an
                # unaffordable one. Everything else about it passed, which is
                # what makes rotation a legitimate question rather than a way
                # of talking ourselves past a gate.
                cap_blocked.append((contract, s.score))
        record["gate_results"] = gate_rows

        # 4c. Rotation: the answer to "I want to trade and I am at the cap".
        #
        # Without it the agent logs `0/N candidates runnable` for the rest of
        # the week once deployment reaches PORTFOLIO_CAP -- which is exactly
        # what happened from Tuesday afternoon on. PWT already knew how to rank
        # a contract; it had simply never been pointed at the book. Scoring
        # holdings on the same index turns "is this candidate good?" into the
        # question that matters when capital is scarce: "is it better than the
        # worst thing I already own, by more than switching costs?"
        if cap_blocked and not self.state.breaker_tripped:
            freed = await self._maybe_rotate(positions, cap_blocked, signals,
                                             raw, record, now, acct.equity)
            if freed:
                return await self._finish(
                    record, f"rotated out of {freed}; entry resumes next cycle")

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
                # A candidate on an underlying we ALREADY hold is the most
                # crowded thing available -- a second HOOD put moves with the
                # first one exactly, not approximately. This used to `continue`
                # here, exempting it from the penalty entirely on the reasoning
                # that a name should not be correlated against itself. That is
                # backwards, and it showed: the agent bought HOOD 99 while
                # holding HOOD 100 and put 58% of the book into one name.
                #
                # Gate 2 only blocks the identical CONTRACT, so different
                # strikes on one underlying are legitimately allowed -- they
                # are different risks. But they should have to outrank a
                # genuinely new name to get funded, not skip the queue.
                if cand in held_roots:
                    crowd[cand] = 1.0
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
                                           now.date(), edge=edge, crowd=crowd)
        record["runnable_table"] = [s.row() for s in scored]
        record["dte"] = allocation.dte(winner.contract, now.date())

        # 6. The model. One candidate, no tools, veto or shrink only.
        #
        # Logged as well as sent, because a verdict is only auditable next to
        # what the model was actually shown. Without this the record proves the
        # model said proceed and not what it had to go on.
        book_context = self._book_context(positions, signals, raw, now.date())
        record["book"] = book_context

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
                          "open_positions": len(held),
                          "drawdown": record["drawdown"]},
            # What we already hold, and how close each of it is to trouble.
            #
            # Until now the model saw `open_positions: 5` -- a count -- and
            # nothing else about the book. That is the reason it approved every
            # one of its first 21 live decisions at full size: given a single
            # pre-vetted candidate and no portfolio context, it had no
            # information the gates did not already have, so there was nothing
            # for it to disagree with.
            #
            # These are the numbers the gates compute per-name and never
            # combine. Four positions each individually inside their limits, all
            # drifting the same way on the same sector, is a book-level fact no
            # single gate can see and the one thing a judgment layer is
            # genuinely better at than a threshold.
            "book": book_context,
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

        # What would have happened with no model in the path.
        #
        # Everything ahead of this point is deterministic: the gates admitted
        # the candidate and the allocator selected it, so the no-model outcome
        # is precisely "trade it, at full size". Recording that alongside the
        # verdict turns the model's contribution into a number we can count
        # instead of an assumption we restate.
        #
        # It is written on EVERY decision, including the ones where nothing
        # changed, because "the model agreed" is only evidence if the cycles
        # where it disagreed were counted the same way. Across the first 21
        # live decisions this layer approved every candidate at full size and
        # altered nothing -- which is a fact about the seat it was sitting in,
        # not about the model, and it took counting to see it.
        record["counterfactual"] = {
            "without_model": {"action": "proceed", "size_multiplier": 1.0},
            "with_model": {"action": verdict.action,
                           "size_multiplier": verdict.size_multiplier},
            "changed": (not verdict.approved) or verdict.size_multiplier != 1.0,
            "changed_by": ("critic" if second is not None
                           and record.get("critique", {}).get("action")
                           != record.get("first_pass", {}).get("action")
                           else "primary"),
        }

        if not verdict.approved:
            return await self._finish(record, f"LLM {verdict.action}: {verdict.reasoning[:160]}")

        # 8. Execution.
        qty = self._size_for(winner, acct.equity, verdict.size_multiplier, deployed)
        record["sizing"] = {
            "edge_rank": round(winner.reward / config.REWARD_LAMBDA, 3)
            if config.REWARD_LAMBDA else None,
            "model_multiplier": verdict.size_multiplier,
            "qty": qty,
        }
        if not self.place_orders:
            return await self._finish(record, "dry run: order not submitted")

        if self.state.orders_today >= config.MAX_ORDERS_PER_DAY:
            self.state.trip_breaker(
                f"order cap {config.MAX_ORDERS_PER_DAY} reached this session")
            return self._finish(
                record,
                f"daily order cap reached ({self.state.orders_today}); "
                "something is looping -- entries halted")

        # A resting sell on this exact contract means we already asked for this
        # trade and the market has not taken it yet. Sending another is not a
        # second decision, it is the same decision twice -- and gate 2 cannot
        # see it, because a working order is not a position.
        if winner.contract.symbol in await self._working_orders("sell"):
            return await self._finish(
                record,
                f"order already working on {winner.contract.symbol}; "
                "not duplicating it")

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

    async def _orders_placed_today(self, now) -> int:
        """How many of OUR orders the broker has seen today.

        The daily cap is a risk control and it lived only in state.json, so a
        restart handed the agent a fresh budget. Counted from the broker
        instead, filtered to our own client_order_id prefix so a manual order
        placed in the Alpaca UI does not consume the agent's allowance.
        """
        try:
            orders = await self.mcp.call("orders", status="all", limit=200)
        except Exception:  # noqa: BLE001
            return 0
        if not isinstance(orders, list):
            return 0
        today = now.date().isoformat()
        n = 0
        for o in orders:
            coid = str(o.get("client_order_id") or "")
            created = str(o.get("created_at") or "")
            if coid.startswith("aw-c") and created[:10] == today:
                # Reprices share a cycle and are not new entries.
                if not coid.rstrip("0123456789").endswith("-r"):
                    n += 1
        return n

    def _book_context(self, positions, signals, raw: dict, today) -> dict:
        """Compact book-level risk for the model's payload. Pure, no calls.

        Every number here was already computed this cycle: spot from the signal
        layer's own quote, rv_iv from the signal, strike and expiry off the OCC
        symbol, P&L off the position. The model was simply never shown any of
        it.

        `worst_cushion` is the summary line. A book can satisfy every
        per-position gate and still be one bad morning from all of it going ITM
        together, which is the failure mode that actually ends a short-put book
        in four sessions.
        """
        by_ticker = {s.ticker: s for s in signals}
        rows: list[dict] = []
        for p in positions:
            if not (p.is_option and p.qty < 0):
                continue
            occ = parse_occ(p.symbol)
            if occ is None:
                continue
            spot = (raw.get(occ.root) or {}).get("spot")
            sig = by_ticker.get(occ.root)
            rows.append({
                "ticker": occ.root,
                "strike": occ.strike,
                "dte": exits.dte_from_expiry(occ.expiry, today),
                # None, not 0.0: an unquoted name is unknown, not safe. The
                # same convention the gates use for a null delta.
                "cushion_pct": (round(exits.cushion(occ.strike, spot) * 100, 2)
                                if spot else None),
                "rv_iv": round(sig.rv_iv, 3) if sig and sig.rv_iv is not None else None,
                "unrealized_pl": p.unrealized_pl,
                # Already firing an exit rule, so the model knows this position
                # is on its way out rather than treating it as settled exposure.
                "exit_streak": self.state.exit_streak.get(p.symbol, 0),
            })

        cushions = [r["cushion_pct"] for r in rows if r["cushion_pct"] is not None]
        return {
            "positions": rows,
            "worst_cushion_pct": min(cushions) if cushions else None,
            "unquoted": sum(1 for r in rows if r["cushion_pct"] is None),
            # Names whose realised vol has caught or passed implied: we are no
            # longer being paid for the risk we are carrying on them.
            "edge_inverted": [r["ticker"] for r in rows
                              if r["rv_iv"] is not None and r["rv_iv"] >= 1.0],
        }

    async def _maybe_rotate(self, positions, cap_blocked, signals, raw, record,
                            now, equity) -> str | None:
        """Free capital by closing the worst holding, if a candidate earns it.

        At most ONE per cycle. The daily order cap bounds churn, but rotating
        several holdings on a single snapshot would act on far more conviction
        than one cycle of numbers supports.

        Four things must all hold, and each removes a different way this could
        be a bad trade:

        1. The candidate beats the weakest holding on PWT by more than
           ROTATION_MARGIN. Switching pays the spread twice, so a candidate
           that merely edges ahead is a worse trade than doing nothing.
        2. Leaving is CHEAP in extrinsic terms -- the same test that stops the
           exit layer overpaying. Rotation is still an exit, and one that
           surrenders more time value than the position ever earned destroys
           value however attractive the replacement looks.
        3. The freed collateral actually funds the candidate. Closing a $5,250
           DRAM put does not pay for a $21,500 NVDA one, and rotating into
           something still unaffordable is a realised loss with extra steps.
        4. Nothing is already working on that contract, and the daily order
           cap has room.
        """
        edge = {x.ticker: x.iv - x.realized_vol for x in signals
                if x.iv is not None and x.realized_vol is not None} or None

        book: list[tuple[Contract, float]] = []
        meta: dict[str, Any] = {}
        for pos in positions:
            if not (pos.is_option and pos.qty < 0):
                continue
            occ = parse_occ(pos.symbol)
            if occ is None:
                continue
            c = Contract(symbol=pos.symbol, underlying=occ.root,
                         strike=occ.strike, expiry=occ.expiry)
            book.append((c, 0.0))
            meta[pos.symbol] = (pos, (raw.get(occ.root) or {}).get("spot"))
        if not book:
            return None

        held_scored = allocation.score_book(book, self.state, equity,
                                            now.date(), edge=edge)
        cand_scored = allocation.select(cap_blocked, self.state, equity,
                                        now.date(), edge=edge)[1]
        record["rotation"] = {"book": [x.row() for x in held_scored],
                              "against": [x.row() for x in cand_scored],
                              "margin": config.ROTATION_MARGIN}

        pair = allocation.rotation(held_scored, cand_scored,
                                   config.ROTATION_MARGIN)
        if pair is None:
            record["rotation"]["outcome"] = "held: nothing cleared the margin"
            return None

        weakest, best = pair
        pos, spot = meta[weakest.contract.symbol]
        qty = int(abs(pos.qty))

        credit = exits.credit_received(pos.market_value, pos.unrealized_pl)
        intr = exits.intrinsic_value(weakest.contract.strike, spot, qty)
        mark = abs(pos.market_value) if pos.market_value is not None else None
        extr = exits.extrinsic_value(mark, intr)
        if (intr is not None and intr > 0 and extr is not None and credit
                and extr > config.EXIT_MAX_EXTRINSIC_GIVEUP * credit):
            record["rotation"]["outcome"] = (
                f"held {weakest.contract.underlying}: leaving surrenders "
                f"{extr:.0f} of time value on a {credit:.0f} credit")
            self.log.note("  rotation declined: leaving "
                          f"{weakest.contract.symbol} costs more time value "
                          "than it ever earned")
            return None

        freed = weakest.contract.collateral * qty
        if freed < best.contract.collateral:
            record["rotation"]["outcome"] = (
                f"held: freeing {freed:,.0f} does not fund "
                f"{best.contract.underlying} at {best.contract.collateral:,.0f}")
            return None
        if weakest.contract.symbol in await self._working_orders("buy"):
            record["rotation"]["outcome"] = "held: a close is already working"
            return None
        if self.state.orders_today >= config.MAX_ORDERS_PER_DAY:
            record["rotation"]["outcome"] = "held: daily order cap reached"
            return None

        self.log.note(
            f"  ROTATE {weakest.contract.underlying} (pwt {weakest.pwt:+.3f}) "
            f"-> {best.contract.underlying} (pwt {best.pwt:+.3f}), "
            f"frees {freed:,.0f}")
        self.state.orders_today += 1
        f = await self.exec.buy_to_close(weakest.contract.symbol, qty,
                                         self.state.cycle)
        record["rotation"]["outcome"] = {
            "closed": weakest.contract.symbol, "for": best.contract.symbol,
            "status": f.status, "filled": f.filled_qty,
            "pwt_gap": round(best.pwt - weakest.pwt, 4)}
        if not f.filled_qty:
            return None
        self.state.forget_position(weakest.contract.symbol)
        ledger.record_close(
            symbol=weakest.contract.symbol, ticker=weakest.contract.underlying,
            strike=weakest.contract.strike, expiry=weakest.contract.expiry,
            qty=qty, closed_by=ledger.CLOSED_BY_ROTATION,
            rule=f"rotated_for_{best.contract.underlying}",
            cycle=self.state.cycle, credit=credit,
            exit_cost=(f.fill_price * 100.0 * qty
                       if f.fill_price is not None else None),
            spot=spot,
            dte=exits.dte_from_expiry(weakest.contract.expiry, now.date()),
            note=f"pwt gap {best.pwt - weakest.pwt:+.4f}")
        return weakest.contract.symbol

    def _size_for(self, winner, equity: float, model_multiplier: float,
                  deployed: float) -> int:
        """Contracts to sell: conviction first, then the model, then the caps.

        Conviction is the candidate's rank on measured variance risk premium --
        the allocator's own `reward` term, read off the quote rather than
        forecast. More edge per contract is the only honest argument for more
        contracts.

        The model then scales DOWN and can never scale up: the safety story is
        that a confused model costs a trade we skipped, never a trade we
        oversized. At a fixed one contract this lever did nothing, so a SHRINK
        verdict was silently discarded twice on 2 Sep.
        """
        rank = (winner.reward / config.REWARD_LAMBDA) if config.REWARD_LAMBDA else 0.5
        if rank >= config.SIZE_EDGE_STRONG:
            base = config.MAX_CONTRACTS_PER_ORDER
        elif rank >= config.SIZE_EDGE_FLOOR:
            base = max(config.CONTRACTS_PER_ORDER, config.MAX_CONTRACTS_PER_ORDER - 1)
        else:
            base = config.CONTRACTS_PER_ORDER
        qty = max(config.CONTRACTS_PER_ORDER, int(base * model_multiplier))

        # Both caps bind last, and BOTH matter.
        #
        # Gate 6 validated the portfolio cap against ONE contract's collateral,
        # because one contract is all it was shown. Sizing above one silently
        # invalidates that check: three contracts add three times the collateral
        # the gate approved, and the 85% limit is breached by a path that never
        # re-examined it. A limit a later step can walk through is not a limit --
        # the same shape as the defect that let unparseable symbols under-count
        # deployment.
        collateral = winner.contract.collateral
        if collateral > 0 and equity > 0:
            by_position = int((config.PER_POSITION_CAP * equity) // collateral)
            room = config.PORTFOLIO_CAP * equity - deployed
            by_portfolio = int(room // collateral) if room > 0 else 0
            qty = min(qty, by_position, by_portfolio)

        # Never below one: gate 6 has already proved a single contract fits, so
        # a floor of one cannot breach a cap that was just checked.
        return max(1, qty)

    async def _working_orders(self, side: str) -> set[str]:
        """Contracts with one of OUR orders of `side` still live at the broker.

        Both sides need this and for the same reason: client_order_id embeds the
        CYCLE, so an order that does not fill inside ORDER_TIMEOUT_SECONDS rests
        at the broker while the next cycle mints a different id that
        existing_order() cannot match. Nothing else notices -- gate 2 checks
        POSITIONS, and a resting order is not a position yet.

        Seen live on 2 Sep: a HOOD 101 sell repriced twice, never filled, and
        sat at 0.50 while the market moved to 0.42/0.49. Had the next cycle
        selected HOOD again it would have sent a second sell, and two fills
        would have left the book short twice what the agent sized for, with the
        position and portfolio caps both computed for one.

        Fails to the SAFE side: if the call errors we return a wildcard, which
        suppresses the action for that cycle. Skipping costs one cycle of delay;
        duplicating sends an order we never decided to send.
        """
        try:
            orders = await self.mcp.call("orders", status="open", limit=100)
        except Exception:  # noqa: BLE001
            return {"*"}
        if not isinstance(orders, list):
            return {"*"}
        out: set[str] = set()
        for o in orders:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            sym = str(o.get("symbol") or "")
            if not sym or not coid.startswith("aw-c"):
                continue  # not ours; a hand-placed order is not our business
            if str(o.get("side") or "").lower() == side:
                out.add(sym)
        return out

    async def _manage_book(self, positions, chains, record: dict, today) -> int:
        """Evaluate exit rules on every short we hold. Returns how many closed.

        No model is consulted. Entries fail closed on a model timeout, which
        costs a trade and nothing worse; exits inverted would fail by leaving a
        position open, so a safety control whose correctness depends on a
        network call is the wrong shape. Everything here is arithmetic on
        numbers this cycle already produced.
        """
        if not config.EXITS_ENABLED:
            return 0

        shorts = [p for p in positions if p.is_option and p.qty < 0]
        if not shorts:
            self.state.exit_streak.clear()
            return 0

        # Contracts that already have a close working at the broker.
        #
        # buy_to_close derives its client_order_id from the CYCLE number, so a
        # close that does not fill inside ORDER_TIMEOUT_SECONDS is left resting
        # and the next cycle mints a DIFFERENT id -- existing_order() cannot see
        # it, and we would send a second buy on the same contract. Stack a few
        # across cycles and the agent buys back more contracts than it is short,
        # turning a closed put into a long one. Nothing downstream catches that:
        # the position cap governs opening, and gate 2 governs entries.
        working = await self._working_orders("buy")

        rows: list[dict] = []
        closed = 0
        for p in shorts:
            occ = parse_occ(p.symbol)
            if occ is None:
                # Handled by the unparseable-symbol path above, which has
                # already halted entries. Nothing useful to say about a
                # position whose strike we cannot read.
                continue

            # fresh=True for the same reason _close_largest uses it: this check
            # only matters in a fast market, which is when a cached quote is
            # most wrong.
            spot = await self.data.spot(occ.root, fresh=True)

            # Delta for the contract we HOLD, not the strike we would open
            # today. Sourced from the chain this cycle already fetched; None
            # when the chain failed, which evaluate() treats as absence of
            # evidence rather than evidence of safety.
            delta = None
            for c in chains.get(occ.root, ()):
                if c.symbol == p.symbol:
                    delta = c.delta
                    break

            signal = exits.evaluate(
                symbol=p.symbol, ticker=occ.root, strike=occ.strike, spot=spot,
                delta=delta, market_value=p.market_value,
                unrealized_pl=p.unrealized_pl,
                dte=exits.dte_from_expiry(occ.expiry, today),
                qty=int(abs(p.qty)),
            )
            streak = self.state.note_exit_trigger(p.symbol, signal is not None)
            if signal is None:
                continue

            row = signal.row() | {"streak": streak}
            if not exits.confirmed(signal, streak):
                row["action"] = "watching"
                rows.append(row)
                self.log.note(f"  ~ {p.symbol} {signal.rule}: {signal.why} "
                              f"(cycle {streak}/{config.EXIT_PERSIST_CYCLES})")
                continue

            # Already working at the broker: leave it alone rather than send a
            # duplicate the position cap would never catch.
            if "*" in working or p.symbol in working:
                row["action"] = "close already working"
                rows.append(row)
                self.log.note(f"  ~ {p.symbol} close already resting at the broker")
                continue

            # Exits count against the daily order cap too. The cap is a
            # this-is-looping detector, not an entry budget -- an exit rule
            # stuck in a fire/refill loop is exactly the runaway it exists to
            # stop, and counting only entries left that half unguarded.
            if self.state.orders_today >= config.MAX_ORDERS_PER_DAY:
                self.state.trip_breaker(
                    f"order cap {config.MAX_ORDERS_PER_DAY} reached this session")
                row["action"] = "blocked by daily order cap"
                rows.append(row)
                self.log.note(f"  !! {p.symbol} exit blocked: daily order cap "
                              f"({self.state.orders_today}) -- something is looping")
                break

            self.state.orders_today += 1
            f = await self.exec.buy_to_close(p.symbol, int(abs(p.qty)),
                                             self.state.cycle)
            row["action"] = "closed"
            row["status"] = f.status
            row["filled"] = f.filled_qty
            rows.append(row)
            self.log.note(f"  EXIT {p.symbol} {signal.rule}: {signal.why} "
                          f"-> {f.status} {f.filled_qty}/{abs(int(p.qty))}")
            if f.filled_qty:
                self.state.forget_position(p.symbol)
                closed += 1
                # Both legs known here -- what we were paid and what we paid to
                # get out -- so this is a MEASURED outcome, not an estimate.
                ledger.record_close(
                    symbol=p.symbol, ticker=occ.root, strike=occ.strike,
                    expiry=occ.expiry, qty=int(abs(p.qty)),
                    closed_by=ledger.CLOSED_BY_RULE, rule=signal.rule,
                    cycle=self.state.cycle,
                    credit=exits.credit_received(p.market_value, p.unrealized_pl),
                    exit_cost=(f.fill_price * 100.0 * abs(p.qty)
                               if f.fill_price is not None else None),
                    spot=spot,
                    dte=exits.dte_from_expiry(occ.expiry, today),
                    note=signal.why)

        if rows:
            record["exits"] = rows
        return closed

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
        # A breaker close is still a closed trade. Recording it matters more
        # than the routine ones, not less: it is the only sample of what the
        # circuit breaker actually costs, and without it the ledger would show
        # a book that shrank for no recorded reason.
        occ_t = parse_occ(target.symbol)
        if occ_t is not None and f.filled_qty:
            ledger.record_close(
                symbol=target.symbol, ticker=occ_t.root, strike=occ_t.strike,
                expiry=occ_t.expiry, qty=int(abs(target.qty)),
                closed_by=ledger.CLOSED_BY_BREAKER, rule="drawdown_breaker",
                cycle=self.state.cycle,
                credit=exits.credit_received(target.market_value,
                                             target.unrealized_pl),
                exit_cost=(f.fill_price * 100.0 * abs(target.qty)
                           if f.fill_price is not None else None),
                note=f"ranked by {basis}")
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
            # Human-initiated, but still an outcome. A ledger that only records
            # the agent's own closes would report a partial history and quietly
            # flatter whichever rules happened to run.
            occ_p = parse_occ(p.symbol)
            if occ_p is not None and f.filled_qty:
                ledger.record_close(
                    symbol=p.symbol, ticker=occ_p.root, strike=occ_p.strike,
                    expiry=occ_p.expiry, qty=int(abs(p.qty)),
                    closed_by=ledger.CLOSED_BY_HUMAN, rule="manual_flatten",
                    cycle=self.state.cycle,
                    credit=exits.credit_received(p.market_value, p.unrealized_pl),
                    exit_cost=(f.fill_price * 100.0 * abs(p.qty)
                               if f.fill_price is not None else None))
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
                self._consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                # A cycle must never kill the run. Log and try again next tick.
                self._consecutive_failures += 1
                self.log.cycle({"cycle": self.state.cycle,
                                "error": f"{type(exc).__name__}: {exc}",
                                "consecutive_failures": self._consecutive_failures,
                                "no_trade_reason": "cycle raised"})

                # Catching every cycle error was half a solution. If the
                # network drops, or the MCP subprocess dies, the connection is
                # opened OUTSIDE this loop -- so every later call fails and
                # nothing ever reconnects. The agent then logs "cycle raised"
                # every fifteen minutes for the rest of the week, alive and
                # doing nothing, and the supervisor cannot help because it only
                # restarts a process that EXITS.
                #
                # A zombie that looks healthy in the log is the worst outcome
                # available, so after enough consecutive failures we exit
                # deliberately and let the supervisor rebuild the connection
                # from scratch. Recovery is safe: positions, deployment, ubt
                # and the daily order count all come back from the broker.
                if self._consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
                    self.log.note(
                        f"\n{self._consecutive_failures} cycles failed in a row "
                        f"({type(exc).__name__}). The connection is not coming "
                        f"back on its own -- exiting so the supervisor can "
                        f"restart with a fresh one.")
                    return
            # A dead MCP connection does not raise -- every caller catches
            # and degrades, so the cycle completes looking healthy while
            # deciding on nothing. The timeout counter is the only evidence,
            # and this is the same deliberate exit the failure counter uses:
            # the connection was opened outside this loop and cannot be rebuilt
            # from inside it.
            timeouts = getattr(self.mcp, "consecutive_timeouts", 0)
            if timeouts >= config.MCP_MAX_CONSECUTIVE_TIMEOUTS:
                self.log.note(
                    f"\n{timeouts} consecutive MCP timeouts. The connection is "
                    f"wedged, not slow -- exiting so the supervisor can restart "
                    f"with a fresh one.")
                return

            await asyncio.sleep(config.CYCLE_SECONDS)
