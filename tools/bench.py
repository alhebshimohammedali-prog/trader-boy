"""Measure whether a model can actually hold the decision seat.

    python tools/bench.py                                  # whatever .env points at
    python tools/bench.py zai-org/GLM-5.3-Flash
    python tools/bench.py modelA modelB --repeats 5

Benchmarks tell you a model is smart. They do not tell you it will return
parseable JSON on the 40th call of a Thursday afternoon, and that is the only
property this seat needs. The decision layer fails closed, so ONE unparseable
response is one trade we never place. A model that is brilliant 95% of the time
is a model that silently skips 5% of the week.

So this measures the four things that can actually cost us a trade:

  parse      did a JSON object come back at all
  schema     was `action` in the enum and `size_multiplier` a number
  judgment   did it veto the defective fixtures and clear the clean one
  discipline did it stay inside the three forbidden failure modes

Fixtures are hand-built to have an unambiguous right answer. A model that
vetoes everything scores 100% on the defective ones and is still useless, which
is why `clean` is in here and weighted the same.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from src.decide import SYSTEM_PROMPT, _ask, coerce, parse_verdict  # noqa: E402

# Words that betray a directional view. The prompt says the model has no
# predictive edge on price and must not form one; if these show up in the
# reasoning, the model is forecasting rather than risk-checking.
FORECAST = re.compile(
    r"\b(bullish|bearish|rally|rebound|downtrend|uptrend|oversold|overbought"
    r"|expect(?:s|ed)? (?:the )?(?:stock|price|underlying)|will (?:fall|drop|rise|climb)"
    r"|likely to (?:fall|drop|rise|climb)|support level|resistance level)\b", re.I)

# Reading the polling interval as information. "Cadence trading."
CADENCE = re.compile(
    r"\b(cycle number|this cycle|nth cycle|polling|fifteen minutes|15[- ]minute"
    r"|since the last (?:cycle|check))\b", re.I)


def _c(ticker, strike, bid, ask, delta, iv, oi, deployed, dte=4,
       hours_to_mark=76.0, hours_to_cutoff=25.0, macro=None, equity=100_000.0):
    collateral = strike * 100
    return {
        "ticker": ticker,
        "contract": {"symbol": f"{ticker}260904P{int(strike*1000):08d}",
                     "strike": strike, "expiry": "2026-09-04", "dte": dte,
                     "bid": bid, "ask": ask, "open_interest": oi,
                     "delta": delta, "iv": iv},
        "signal": {"ticker": ticker, "score": 0.61, "iv_rank": 0.58,
                   "rv": 0.29, "momentum": 0.004},
        "allocation": {"ticker": ticker, "pwt": 3.2, "age": 4,
                       "ubt": 0.0, "opbt": 1.4, "selected": True},
        "sizing": {"collateral": collateral, "pct_equity": collateral / equity},
        "portfolio": {"equity": equity, "deployed_pct": deployed,
                      "open_positions": 2},
        "clock": {"hours_to_mark": hours_to_mark,
                  "hours_to_entry_cutoff": hours_to_cutoff,
                  "minutes_to_next_macro_event": macro},
    }


# (name, payload, acceptable actions, what we are testing)
FIXTURES = [
    ("clean",
     _c("CSCO", 67.0, 0.44, 0.48, -0.18, 0.263, 3100, 0.21),
     {"proceed"},
     "nothing wrong; a veto here is a model that scores zero"),

    ("zero_bid",
     _c("GM", 55.0, 0.0, 0.31, -0.19, 0.301, 900, 0.19),
     {"veto"},
     "bid is zero, an explicitly listed defect"),

    ("implausible_premium",
     _c("PEP", 141.0, 44.80, 45.60, -0.17, 0.224, 2200, 0.17),
     {"veto"},
     "$45 credit on a 2% OTM weekly put is a data error"),

    ("blown_spread",
     _c("INTC", 23.0, 0.21, 0.79, -0.22, 0.402, 1400, 0.23),
     {"veto", "shrink"},
     "spread far wider than the gate measured"),

    ("near_cap",
     _c("XOM", 108.0, 0.39, 0.43, -0.18, 0.241, 5200, 0.575),
     {"shrink", "veto"},
     "57.5% deployed against a 60% cap; full size leaves no headroom"),

    ("macro_blackout",
     _c("NVDA", 168.0, 1.02, 1.09, -0.19, 0.437, 8800, 0.24, macro=12),
     {"veto", "shrink"},
     "opening 12 minutes before a macro event"),

    ("comfortable_not_maximal",
     _c("DIS", 88.0, 0.36, 0.40, -0.16, 0.252, 2700, 0.08),
     {"proceed"},
     "well inside every bound; shrinking here is number hardening"),
]


async def run_model(model: str | None, provider: str, repeats: int) -> dict:
    if model:
        os.environ["FEATHERLESS_MODEL"] = model
    env_var = "ANTHROPIC_MODEL" if provider == "anthropic" else "FEATHERLESS_MODEL"
    label = model or os.getenv(env_var) or "(unset)"
    print(f"\n{'=' * 74}\n{label}  via {provider}\n{'=' * 74}")

    calls = parsed = schema_ok = judged_ok = 0
    forecast_hits: list[str] = []
    cadence_hits: list[str] = []
    latencies: list[float] = []
    errors: list[str] = []
    per_fixture: dict[str, list[str]] = {}

    import json
    for name, payload, acceptable, _why in FIXTURES:
        got: list[str] = []
        for _ in range(repeats):
            calls += 1
            t0 = time.perf_counter()
            text, err, _m = await _ask(provider, SYSTEM_PROMPT,
                                       json.dumps(payload, indent=2, default=str))
            latencies.append(time.perf_counter() - t0)
            if err:
                errors.append(err)
                got.append("ERR")
                continue
            obj = parse_verdict(text)
            if obj is None:
                got.append("unparseable")
                continue
            parsed += 1
            d = coerce(obj, provider, label, text)
            if d.error:
                got.append(f"bad-schema({d.error[:24]})")
                continue
            schema_ok += 1
            got.append(d.action)
            if d.action in acceptable:
                judged_ok += 1
            if FORECAST.search(d.reasoning):
                forecast_hits.append(f"{name}: {d.reasoning[:90]}")
            if CADENCE.search(d.reasoning):
                cadence_hits.append(f"{name}: {d.reasoning[:90]}")
        per_fixture[name] = got
        want = "/".join(sorted(acceptable))
        hits = sum(1 for g in got if g in acceptable)
        flag = "ok  " if hits == repeats else ("part" if hits else "MISS")
        print(f"  [{flag}] {name:24s} want {want:14s} got {', '.join(got)}")

    pct = lambda n: (100.0 * n / calls) if calls else 0.0  # noqa: E731
    print(f"\n  parse      {parsed:3d}/{calls}  ({pct(parsed):.0f}%)"
          "   <- anything below 100% is trades silently skipped")
    print(f"  schema     {schema_ok:3d}/{calls}  ({pct(schema_ok):.0f}%)")
    print(f"  judgment   {judged_ok:3d}/{calls}  ({pct(judged_ok):.0f}%)")
    print(f"  discipline forecasting {len(forecast_hits)}, cadence {len(cadence_hits)}")
    if latencies:
        s = sorted(latencies)
        print(f"  latency    p50 {statistics.median(s):.1f}s  "
              f"p95 {s[max(0, int(len(s) * 0.95) - 1)]:.1f}s  max {s[-1]:.1f}s"
              f"   (timeout {config.LLM_TIMEOUT_SECONDS}s)")
    for h in forecast_hits[:3]:
        print(f"    FORECASTING  {h}")
    for h in cadence_hits[:3]:
        print(f"    CADENCE      {h}")
    if errors:
        print(f"  errors     {len(errors)}: {errors[0][:150]}")

    return {"model": label, "calls": calls, "parse": pct(parsed),
            "schema": pct(schema_ok), "judgment": pct(judged_ok),
            "forecast": len(forecast_hits), "cadence": len(cadence_hits),
            "p50": statistics.median(latencies) if latencies else 0.0}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", help="model ids (Featherless); default: .env")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--provider", default=None)
    args = ap.parse_args()

    load_dotenv(".env")
    provider = (args.provider or os.getenv("LLM_PROVIDER") or "none").strip().lower()
    if provider == "none":
        print("LLM_PROVIDER is none. Nothing to bench.")
        return 1

    rows = [await run_model(m or None, provider, args.repeats)
            for m in (args.models or [None])]

    if len(rows) > 1:
        print(f"\n{'=' * 74}\nSUMMARY\n{'=' * 74}")
        print(f"  {'model':38s} {'parse':>6s} {'schema':>7s} {'judge':>6s} "
              f"{'fcast':>6s} {'cad':>4s} {'p50':>6s}")
        for r in sorted(rows, key=lambda r: (-r["parse"], -r["judgment"])):
            print(f"  {r['model'][:38]:38s} {r['parse']:5.0f}% {r['schema']:6.0f}% "
                  f"{r['judgment']:5.0f}% {r['forecast']:6d} {r['cadence']:4d} "
                  f"{r['p50']:5.1f}s")

    print("\nPick on parse first, judgment second, latency last. A 15-minute\n"
          "cycle makes a 6-second model and a 20-second model indistinguishable,\n"
          "but one dropped JSON object is one trade that never happened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
