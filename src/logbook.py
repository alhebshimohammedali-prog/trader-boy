"""Layer 10: logging.

Two sinks from one record:
  - runs/<ts>/cycles.jsonl  -- the auditable trace
  - stdout                  -- a readable table, which is what makes the demo
                               video free (record the terminal, not a UI)

Every cycle writes a record, including "no trade" ones with the reason. A log
that only contains trades cannot show that the agent declined for good reasons,
which is half of what "autonomy and robustness" means.

The runnable-candidate table is the load-bearing part: pwt/age/ubt/opbt for
every runnable candidate, selected AND rejected. Without it the allocation claim is
unsupported.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config


class Logbook:
    def __init__(self, run_dir: str | None = None, echo: bool = True):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(run_dir or Path(config.RUNS_DIR) / stamp)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "cycles.jsonl"
        # The readable transcript. cycles.jsonl is the auditable record and is
        # the right thing to replay, but nobody reads it to find out what
        # happened -- they read the table. Keeping only the terminal copy means
        # the session is lost to a scrollback limit, a closed window, or a
        # supervisor restart, which over four unattended days is most of it.
        self.console = self.dir / "console.log"
        self.echo = echo
        self.records: list[dict] = []

    # ------------------------------------------------------------- write ---

    def _emit(self, text: str) -> None:
        """Print and persist. Append-only, and a transcript failure must never
        take the agent down with it."""
        if self.echo:
            print(text)
        try:
            with self.console.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    def cycle(self, record: dict) -> None:
        # ET, not local. Every deadline in this system is Eastern -- the open,
        # the entry cutoff, the Thursday mark -- and the operator's machine is
        # twelve hours ahead of it. A local stamp made a Sunday 21:19 ET cycle
        # read as "09:19" in the log, which looks like a Monday morning run
        # that happened before the market opened. The record has to be
        # auditable against the schedule it is scored on.
        record.setdefault("timestamp", datetime.now(config.ET).isoformat())
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        self._print(record)

    def note(self, text: str) -> None:
        # Notes carry the startup tool-resolution table and the competition
        # account warning -- the evidence the broker integration resolved, and
        # which account was live. Previously echoed and never written down.
        self._emit(text)

    # ------------------------------------------------------------- print ---

    def _print(self, r: dict) -> None:
        n = r.get("cycle", "?")
        eq = r.get("equity") or 0.0
        dd = r.get("drawdown") or 0.0
        dep = r.get("deployed_pct") or 0.0
        self._emit(f"\n{'=' * 72}")
        self._emit(f"cycle {n}  {r.get('timestamp', '')[:19]} ET   "
              f"equity ${eq:,.2f}   deployed {dep:.0%}   dd {dd:.2%}")

        gated = r.get("gate_results") or []
        if gated:
            failed = [g for g in gated if not g.get("passed")]
            self._emit(f"\n  gates: {len(gated) - len(failed)}/{len(gated)} candidates runnable")
            for g in failed[:8]:
                self._emit(f"    x {g.get('ticker',''):6s} {g.get('reason','')[:88]}")

        table = r.get("runnable_table") or []
        if table:
            self._emit(f"\n  {'ticker':7s} {'signal':>7s} {'age':>4s} {'ubt':>7s} "
                  f"{'opbt':>6s} {'pwt':>8s}   sel")
            for row in table:
                self._emit(f"  {row['ticker']:7s} {row['signal']:7.3f} {row['age']:4d} "
                      f"{row['ubt']:7.3f} {row['opbt']:6.3f} {row['pwt']:8.3f}   "
                      f"{'<-- SELECTED' if row.get('selected') else ''}")

        first, crit, d = r.get("first_pass"), r.get("critique"), r.get("decision")
        if first:
            self._emit(f"\n  LLM    [{first.get('provider','?')}] "
                  f"{first.get('action','?').upper()} "
                  f"x{first.get('size_multiplier', 0):.2f}")
            self._emit(f"    {first.get('reasoning','')[:180]}")
        elif d:
            self._emit(f"\n  LLM    [{d.get('provider','?')}] {d.get('action','?').upper()} "
                  f"x{d.get('size_multiplier', 0):.2f}")
            self._emit(f"    {d.get('reasoning','')[:200]}")

        if crit:
            # A critic that failed is not a critic that agreed. Say which,
            # because a silent second pass looks identical to a working one
            # that concurred, and the whole point of the pass is disagreement.
            if crit.get("error"):
                mark = "UNAVAILABLE"
            elif crit.get("action") != (first or {}).get("action"):
                mark = "OVERRODE"
            else:
                mark = "concurred"
            self._emit(f"  CRITIC [{crit.get('provider','?')}] "
                  f"{crit.get('action','?').upper()} "
                  f"x{crit.get('size_multiplier', 0):.2f}  ({mark})")
            self._emit(f"    {crit.get('reasoning','')[:180]}")

        f = r.get("fill")
        if f:
            slip = f.get("slippage")
            slip_s = f"{slip:+.3f}" if isinstance(slip, (int, float)) else "n/a"
            self._emit(f"\n  ORDER {f.get('symbol','')}  status={f.get('status','?')}  "
                  f"filled {f.get('filled_qty',0)}/{f.get('requested_qty',0)}  "
                  f"limit {f.get('limit_price')}  fill {f.get('fill_price')}  "
                  f"slippage {slip_s}")
            if f.get("dropped_args"):
                self._emit(f"    note: server schema dropped args {f['dropped_args']}")

        rec = r.get("reconciliation")
        if rec:
            self._emit(f"  reconcile: {rec.get('summary','')}")
            if rec.get("fill_check", {}).get("diverged"):
                self._emit(f"    !! DIVERGENCE {rec['fill_check'].get('note','')}")

        if r.get("no_trade_reason"):
            self._emit(f"\n  NO TRADE: {r['no_trade_reason']}")

        if r.get("narrative"):
            self._emit(f"\n  > {r['narrative']}")

    # ----------------------------------------------------------- metrics ---

    def metrics(self) -> dict:
        """The four numbers §8 asks the write-up to report. They stay
        meaningful regardless of which way the market went."""
        cycles = self.records or []
        n = len(cycles)

        utilisation = (
            sum(c.get("deployed_pct") or 0.0 for c in cycles) / n if n else 0.0
        )

        waits: list[int] = []
        for c in cycles:
            for row in c.get("runnable_table") or []:
                if row.get("selected"):
                    waits.append(row.get("age", 0))
        mean_wait = sum(waits) / len(waits) if waits else 0.0

        premium = 0.0
        capital_days = 0.0
        by_ticker: dict[str, float] = {}
        slippages: list[float] = []
        slippage_cost = 0.0
        attempts: list[int] = []

        for c in cycles:
            f = c.get("fill") or {}
            if f.get("filled_qty"):
                px = f.get("fill_price") or 0.0
                qty = f.get("filled_qty") or 0
                premium += px * 100 * qty

                # Realised transaction cost, measured rather than assumed.
                slip = f.get("slippage")
                if isinstance(slip, (int, float)):
                    slippages.append(slip)
                    slippage_cost += abs(slip) * 100 * qty
                if isinstance(f.get("attempts"), int):
                    attempts.append(f["attempts"])

                sel = next(
                    (r for r in c.get("runnable_table") or [] if r.get("selected")), {}
                )
                by_ticker[sel.get("ticker", "?")] = (
                    by_ticker.get(sel.get("ticker", "?"), 0.0) + (sel.get("collateral") or 0.0)
                )
                capital_days += (sel.get("collateral") or 0.0) * (c.get("dte") or 0)

        total_coll = sum(by_ticker.values())
        hhi = (
            sum((v / total_coll) ** 2 for v in by_ticker.values()) if total_coll else 0.0
        )
        gross = premium + slippage_cost

        return {
            "cycles": n,
            "trades": sum(1 for c in cycles if (c.get("fill") or {}).get("filled_qty")),
            "capital_utilisation_pct": round(utilisation * 100, 2),
            "mean_candidate_wait_cycles": round(mean_wait, 2),
            "herfindahl_concentration": round(hhi, 4),
            "premium_collected_net": round(premium, 2),
            "premium_per_capital_day": (
                round(premium / capital_days, 6) if capital_days else None
            ),
            # --- realised transaction-cost model (§8) ---------------------
            # Not an assumption and not a parameter: this is what pricing at
            # or through the bid actually cost us, per fill, measured against
            # the mid we quoted from. Audits of LLM-trading studies find only
            # 1 in 19 report an explicit cost model at all.
            "mean_slippage_per_contract": (
                round(sum(slippages) / len(slippages), 4) if slippages else None
            ),
            "worst_slippage_per_contract": round(min(slippages), 4) if slippages else None,
            "total_slippage_cost": round(slippage_cost, 2),
            "slippage_pct_of_gross_premium": (
                round(100 * slippage_cost / gross, 2) if gross else None
            ),
            "mean_order_attempts": (
                round(sum(attempts) / len(attempts), 2) if attempts else None
            ),
            "no_trade_cycles": sum(1 for c in cycles if c.get("no_trade_reason")),
        }

    def finalise(self) -> dict:
        m = self.metrics()
        (self.dir / "metrics.json").write_text(
            json.dumps(m, indent=2), encoding="utf-8"
        )
        if self.echo:
            self._emit(f"\n{'=' * 72}\nrun metrics -> {self.dir / 'metrics.json'}")
            for k, v in m.items():
                self._emit(f"  {k:32s} {v}")
        return m
