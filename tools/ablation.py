"""Ablation: does the allocation layer actually earn its place?

Runs four allocation policies over an IDENTICAL candidate stream and reports
the §8 metrics side by side. Same gates, same signals, same universe, same
capital -- only the selection rule differs.

  pwt          the index policy: age - ubt + opbt, opbt excluding self
  greedy       max(signal) -- the obvious baseline
  random       seeded uniform choice -- the null hypothesis
  roundrobin   deterministic rotation -- fairness with no economics

This is the artifact that converts "we built something novel" into "we built
something novel and here is what it bought." Reproducible, no credentials, no
network: `python tools/ablation.py`.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import date, datetime


from src.allocation import capital_time, dte, select
from src.data import Contract
from src.state import State

TODAY = date(2026, 8, 31)
EQUITY = 100_000.0
CYCLES = 40
SEED = 20260831

# (ticker, strike, base signal). Deliberately varied in BOTH signal and size so
# a policy can trade quality against capital efficiency.
UNIVERSE = [
    ("NVDA", 178.0, 0.91),
    ("AAPL", 226.0, 0.74),
    ("GOOGL", 203.0, 0.68),
    ("XOM", 112.0, 0.61),
    ("IWM", 238.0, 0.57),
    ("TSLA", 341.0, 0.55),
]


def make_candidates(rng: random.Random) -> list[tuple[Contract, float]]:
    """One contract per ticker, with a little signal jitter per cycle so the
    greedy policy is not trivially frozen on one name."""
    out = []
    for ticker, strike, base in UNIVERSE:
        sig = max(0.0, min(1.0, base + rng.uniform(-0.04, 0.04)))
        c = Contract(
            symbol=f"{ticker}260904P{int(strike * 1000):08d}",
            underlying=ticker, strike=strike, expiry="2026-09-04",
            bid=1.10, ask=1.18, open_interest=2500, delta=-0.22,
        )
        out.append((c, sig))
    return out


@dataclass
class Outcome:
    policy: str
    picks: list[str]
    waits: list[int]
    capital_time: list[float]
    premium: float

    def metrics(self) -> dict:
        by_ticker: dict[str, int] = {}
        for t in self.picks:
            by_ticker[t] = by_ticker.get(t, 0) + 1
        n = len(self.picks)
        hhi = sum((v / n) ** 2 for v in by_ticker.values()) if n else 0.0
        starved = len(UNIVERSE) - len(by_ticker)
        total_ct = sum(self.capital_time)

        # Rotation latency: cycles since this ticker LAST received capital.
        # (Counting from first-qualified instead is degenerate -- every ticker
        # qualifies on cycle 1, so it returns the same number for every policy
        # and measures nothing.)
        last_seen: dict[str, int] = {}
        gaps: list[int] = []
        for i, t in enumerate(self.picks):
            if t in last_seen:
                gaps.append(i - last_seen[t])
            last_seen[t] = i
        # A never-selected ticker starves for the whole run.
        worst = max(gaps) if gaps else 0
        if starved:
            worst = max(worst, n)

        return {
            "policy": self.policy,
            "tickers_used": f"{len(by_ticker)}/{len(UNIVERSE)}",
            "starved": starved,
            "herfindahl": round(hhi, 4),
            "mean_revisit_gap": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
            "worst_gap": worst,
            "mean_capital_time": round(total_ct / n, 4) if n else 0.0,
            "premium_per_capital_day": round(self.premium / total_ct, 4) if total_ct else 0.0,
        }


def run(policy: str) -> Outcome:
    rng = random.Random(SEED)
    state = State()
    picks: list[str] = []
    waits: list[int] = []
    cts: list[float] = []
    premium = 0.0
    rotation = 0

    for _ in range(CYCLES):
        state.begin_cycle(datetime.now(), EQUITY)
        cands = make_candidates(rng)
        for c, _s in cands:
            state.mark_qualified(c.underlying)

        if policy == "pwt":
            winner, _scored = select(cands, state, EQUITY, TODAY)
            chosen, age = winner.contract, winner.age
        else:
            if policy == "greedy":
                chosen = max(cands, key=lambda x: x[1])[0]
            elif policy == "random":
                chosen = rng.choice(cands)[0]
            else:  # roundrobin
                chosen = cands[rotation % len(cands)][0]
                rotation += 1
            age = state.age(chosen.underlying)

        d = dte(chosen, TODAY)
        ct = capital_time(chosen, EQUITY, d)

        picks.append(chosen.underlying)
        waits.append(age)
        cts.append(ct)
        # Premium is identical per contract by construction, so the metric
        # isolates capital efficiency rather than rewarding a pricing guess.
        premium += (chosen.bid or 0.0) * 100

        state.charge_capital(chosen.underlying, chosen.collateral, EQUITY, d)

    return Outcome(policy, picks, waits, cts, premium)


def load_stream(pattern: str) -> list[list[tuple[Contract, float]]]:
    """Rebuild real candidate sets from logged runnable tables.

    After the Thursday mark this turns the ablation from a simulation into a
    counterfactual on live data: given the candidate sets the agent actually
    faced, what would greedy / random / round-robin have allocated instead?
    The input is already produced by §8 logging -- no extra instrumentation.
    """
    import glob
    import json

    stream: list[list[tuple[Contract, float]]] = []
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                table = rec.get("runnable_table") or []
                if len(table) < 2:  # no contention -> nothing to arbitrate
                    continue
                cands = []
                for row in table:
                    strike = float(row.get("strike") or 0)
                    if strike <= 0:
                        continue
                    cands.append((
                        Contract(
                            symbol=row.get("symbol") or "",
                            underlying=row.get("ticker") or "",
                            strike=strike,
                            expiry="2026-09-04",
                            bid=1.10, ask=1.18, open_interest=2500, delta=-0.22,
                        ),
                        float(row.get("signal") or 0.0),
                    ))
                if len(cands) >= 2:
                    stream.append(cands)
    return stream


def run_stream(policy: str, stream: list[list[tuple[Contract, float]]]) -> Outcome:
    rng = random.Random(SEED)
    state = State()
    picks, waits, cts, premium, rotation = [], [], [], 0.0, 0

    for cands in stream:
        state.begin_cycle(datetime.now(), EQUITY)
        for c, _s in cands:
            state.mark_qualified(c.underlying)

        if policy == "pwt":
            winner, _ = select(cands, state, EQUITY, TODAY)
            chosen, age = winner.contract, winner.age
        else:
            if policy == "greedy":
                chosen = max(cands, key=lambda x: x[1])[0]
            elif policy == "random":
                chosen = rng.choice(cands)[0]
            else:
                chosen = cands[rotation % len(cands)][0]
                rotation += 1
            age = state.age(chosen.underlying)

        d = dte(chosen, TODAY)
        picks.append(chosen.underlying)
        waits.append(age)
        cts.append(capital_time(chosen, EQUITY, d))
        premium += (chosen.bid or 0.0) * 100
        state.charge_capital(chosen.underlying, chosen.collateral, EQUITY, d)

    return Outcome(policy, picks, waits, cts, premium)


def main() -> int:
    policies = ("pwt", "greedy", "random", "roundrobin")

    if "--from-log" in sys.argv:
        pattern = "runs/*/cycles.jsonl"
        i = sys.argv.index("--from-log")
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
            pattern = sys.argv[i + 1]
        stream = load_stream(pattern)
        if not stream:
            print(f"no contested candidate sets found in {pattern}")
            print("(needs >=2 runnable candidates in a cycle to arbitrate anything)")
            return 1
        header = (f"replay of {len(stream)} REAL contested candidate sets "
                  f"from {pattern}")
        results = [run_stream(p, stream).metrics() for p in policies]
    else:
        header = (f"synthetic: {CYCLES} cycles, identical candidate stream, "
                  f"seed {SEED}")
        results = [run(p).metrics() for p in policies]

    cols = ["policy", "tickers_used", "starved", "herfindahl",
            "mean_revisit_gap", "worst_gap", "mean_capital_time",
            "premium_per_capital_day"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in results)) + 2 for c in cols}

    print(f"\nallocation ablation -- {header}\n")
    print("".join(c.ljust(widths[c]) for c in cols))
    print("-" * sum(widths.values()))
    for r in results:
        print("".join(str(r[c]).ljust(widths[c]) for c in cols))

    print("\nreading it:")
    print("  starved                  lower is better -- names never allocated capital")
    print("  worst_gap                lower is better -- longest any name went without capital")
    print("  herfindahl               lower is better -- 1.0 means one ticker took everything")
    print("  mean_capital_time        lower is better -- equity-fraction x days tied up per trade")
    print("  premium_per_capital_day  higher is better -- premium earned per unit of committed capital")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
