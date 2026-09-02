"""Layer 11: the closed-trade ledger.

The agent had no memory of how a trade turned out. It tracked how much capital
a ticker had CONSUMED -- `ubt` in state.json -- and nothing about whether the
capital came back. PLTR is the demonstration: after closing it for a realised
loss of $561 the state file still held `capital_days_used: {"PLTR": 0.525}` and
`first_qualified: {"PLTR": 3}`, and nowhere any record that the trade lost
money. Had PLTR cleared the gates the next morning it would have been judged on
its quote alone, by an agent that had just lost on that exact name.

This file is the missing half. It writes one line per closed position to
`runs/ledger.jsonl`, outside any single run directory, so it accumulates across
restarts and across sessions the way `supervisor.log` does.

It deliberately does NOT feed back into any decision yet, and that restraint is
the point. Six closed trades cannot calibrate a threshold; fitting on them
would be overfitting with a straight face, and this codebase already refuses
that elsewhere ("Signal weights in config.py are hand-designed, because four
sessions and a handful of trades is not data you can fit anything to"). What a
ledger does is make the calibration POSSIBLE later, and make its absence
honest now: the loop is open because the sample is too small, not because
nobody thought about it.

Three questions it is built to answer once the sample exists, each of which is
currently answered by convention rather than measurement:

  1. Of positions that reached EXIT_LOSS_MULTIPLE, what share recovered by
     expiry? That is 2.5x tested against reality instead of tradition -- and it
     is precisely the question that would have caught the first version of the
     exit layer before it paid 6.55 to close a put sold for 0.94.
  2. Do trades the model SHRANK or vetoed do worse than the ones it waved
     through? The counterfactual is already recorded per decision; joining it
     to outcomes is what turns "what is the LLM for" into a number.
  3. Which exit rule actually pays for itself, and which just pays the spread?

Every field is measured or explicitly marked an estimate. A ledger that mixes
the two is worse than none, because it invites conclusions its data cannot
support.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import config

def _default_path() -> str:
    """Read from config at call time, not import time, so a test can redirect
    it after the module is loaded."""
    return getattr(config, "LEDGER_PATH", os.path.join("runs", "ledger.jsonl"))

# How a position left the book.
CLOSED_BY_RULE = "exit_rule"      # the exit layer bought it back
CLOSED_BY_ROTATION = "rotation"   # capital moved to a better candidate
CLOSED_BY_BREAKER = "breaker"     # the drawdown circuit breaker
CLOSED_BY_HUMAN = "flatten"       # --flatten
VANISHED = "vanished"             # gone from the book without us closing it


def _append(row: dict[str, Any], path: str | None = None) -> None:
    """Append one record. Never raises: a bookkeeping failure must not be able
    to take down a trading cycle, and a missing ledger line is recoverable from
    cycles.jsonl while a crashed cycle is not."""
    path = path or _default_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001 - see docstring
        pass


def record_close(
    symbol: str,
    ticker: str,
    strike: float,
    expiry: str,
    qty: int,
    closed_by: str,
    *,
    cycle: int | None = None,
    credit: float | None = None,
    exit_cost: float | None = None,
    last_unrealized_pl: float | None = None,
    rule: str | None = None,
    spot: float | None = None,
    dte: int | None = None,
    note: str = "",
    path: str | None = None,
) -> dict[str, Any]:
    """Write one closed-position record and return it.

    `realized_pl` is computed only when both legs of the trade are known --
    what we were paid to open and what we paid to close. When a position simply
    vanished (expiry, assignment) we never bought it back, so there is no exit
    cost; the last mark-to-market figure is carried instead and labelled an
    estimate rather than quietly promoted to a result.
    """
    realized = None
    basis = "unknown"
    if credit is not None and exit_cost is not None:
        realized = round(credit - exit_cost, 2)
        basis = "measured"          # both legs observed
    elif last_unrealized_pl is not None:
        realized = round(last_unrealized_pl, 2)
        basis = "estimated_from_last_mark"

    row = {
        "ts": datetime.now(config.ET).isoformat(),
        "cycle": cycle,
        "symbol": symbol,
        "ticker": ticker,
        "strike": strike,
        "expiry": expiry,
        "qty": qty,
        "closed_by": closed_by,
        "rule": rule,
        "credit": round(credit, 2) if credit is not None else None,
        "exit_cost": round(exit_cost, 2) if exit_cost is not None else None,
        "realized_pl": realized,
        "pl_basis": basis,
        "spot_at_close": spot,
        "dte_at_close": dte,
        "note": note,
    }
    _append(row, path)
    return row


def read_all(path: str | None = None) -> list[dict[str, Any]]:
    """Every record written so far. Empty when the ledger does not exist yet."""
    path = path or _default_path()
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line must not poison the history
    return out


def summary(path: str | None = None) -> dict[str, Any]:
    """Aggregate the ledger. Reports the sample size first, because with six
    trades every other number here is an anecdote wearing a decimal point."""
    rows = read_all(path)
    measured = [r for r in rows if r.get("pl_basis") == "measured"
                and r.get("realized_pl") is not None]
    wins = [r for r in measured if r["realized_pl"] > 0]
    by_rule: dict[str, list[float]] = {}
    for r in measured:
        by_rule.setdefault(r.get("rule") or r.get("closed_by") or "?", []).append(
            r["realized_pl"])
    return {
        "n_closed": len(rows),
        "n_measured": len(measured),
        "realized_total": round(sum(r["realized_pl"] for r in measured), 2),
        "win_rate": round(len(wins) / len(measured), 3) if measured else None,
        "by_rule": {k: {"n": len(v), "total": round(sum(v), 2)}
                    for k, v in sorted(by_rule.items())},
        # Stated, not implied. A win rate over a handful of trades is noise, and
        # the whole reason this loop is not wired into any decision.
        "enough_to_calibrate": len(measured) >= 30,
    }
