"""Layer 3: signal. An admission threshold, not a selection mechanism.

Selection is the allocation layer's job (§6). All this does is decide which tickers
are eligible to be scored at all.

Three components, hand-weighted. No trained model -- there is no data to train
on (four sessions, ~100 cycles, a handful of trades), so any learned weighting
would be fitting noise.

  iv_rank   - is implied vol rich right now?
  momentum  - recent drift in the underlying
  rv_iv     - realised vol over implied. LOW is good: we are being paid more
              than the stock has actually been moving.

Cold-start honesty: §3 asks for IV *percentile*, which needs a trailing history
we do not have on Monday morning. So we accumulate observed IV into state each
cycle and use a true time-series percentile once we have enough observations;
until then we fall back to a cross-sectional rank across the universe. §2
already sanctions the relative-rank approach -- mega-cap earnings season just
ended, so an absolute IV threshold would find zero candidates for four straight
days. The log records which method produced each score.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

import config


@dataclass
class Signal:
    ticker: str
    score: float
    iv: float | None
    iv_rank: float
    iv_method: str  # "timeseries" | "cross_section" | "unavailable"
    momentum: float
    realized_vol: float | None
    rv_iv: float | None

    def row(self) -> dict:
        return {
            "ticker": self.ticker,
            "score": round(self.score, 4),
            "iv": round(self.iv, 4) if self.iv is not None else None,
            "iv_rank": round(self.iv_rank, 4),
            "iv_method": self.iv_method,
            "momentum": round(self.momentum, 4),
            "rv": round(self.realized_vol, 4) if self.realized_vol is not None else None,
            "rv_iv": round(self.rv_iv, 4) if self.rv_iv is not None else None,
        }


MIN_IV_HISTORY = 8  # observations before a time-series percentile means anything

# A correlation from a handful of daily returns is noise with a decimal point.
# Below this we report None and the allocator treats the pair as unmeasured
# rather than uncorrelated, which are very different claims.
MIN_CORR_SAMPLES = 15


def closes(bars: list[dict]) -> list[float]:
    out = []
    for b in bars:
        for k in ("c", "close", "ClosePrice", "close_price"):
            if k in b and b[k] is not None:
                try:
                    out.append(float(b[k]))
                except (TypeError, ValueError):
                    pass
                break
    return out


def log_returns(bars: list[dict], lookback: int | None = None) -> list[float]:
    """Daily log returns, most recent last."""
    px = closes(bars)
    if lookback:
        px = px[-(lookback + 1):]
    return [math.log(px[i] / px[i - 1])
            for i in range(1, len(px)) if px[i - 1] > 0]


def correlation(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation over the overlapping tail of two return series.

    Returns None rather than 0.0 when it cannot be computed. Zero is a
    statement -- "these move independently" -- and claiming it from missing
    data is the kind of fabricated number this system avoids elsewhere.
    """
    n = min(len(a), len(b))
    if n < MIN_CORR_SAMPLES:
        return None
    x, y = a[-n:], b[-n:]
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    sxx = sum((xi - mx) ** 2 for xi in x)
    syy = sum((yi - my) ** 2 for yi in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def empirical_itm_rate(bars: list[dict], moneyness: float,
                       horizon: int) -> tuple[float, int] | None:
    """How often this underlying actually finished below a strike this far
    out, over a holding period this long. Returns (rate, samples).

    An empirical assignment probability, measured from the tape, to set
    against the risk-neutral one the chain reports as delta. They answer the
    same question by different routes: delta prices what the market believes,
    this counts what the stock has done. When the second is much larger than
    the first, the option is underpricing the move that hurts us.

    Windows overlap, so the samples are autocorrelated and the effective count
    is far below the nominal one -- roughly len/horizon independent
    observations. Treat the number as a coarse check, never a precise
    probability, and see MIN_ITM_SAMPLES.
    """
    px = closes(bars)
    if len(px) < horizon + 2:
        return None
    hits = 0
    n = 0
    for i in range(len(px) - horizon):
        start, end = px[i], px[i + horizon]
        if start <= 0:
            continue
        n += 1
        if end < start * (1.0 - moneyness):
            hits += 1
    return (hits / n, n) if n else None


def realized_vol(bars: list[dict], lookback: int | None = None) -> float | None:
    """Annualised stdev of daily log returns."""
    px = closes(bars)
    if lookback:
        px = px[-(lookback + 1):]
    if len(px) < 3:
        return None
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(252)


def momentum(bars: list[dict], lookback: int) -> float:
    px = closes(bars)
    if len(px) < lookback + 1 or px[-(lookback + 1)] <= 0:
        return 0.0
    return (px[-1] - px[-(lookback + 1)]) / px[-(lookback + 1)]


def _mid_rank(value: float, pool: list[float]) -> float:
    """Fraction of `pool` below `value`, counting ties at half weight.

    The half-weight term is load-bearing, not a nicety. A naive `< value` count
    scores a fully-tied pool at 0.0, so if implied vol clusters across the
    universe -- which §2 says it will all week, with mega-cap earnings season
    just ended -- every ticker lands at the bottom, nothing clears the floor,
    and the agent trades zero times for four days. Ties belong in the middle.
    """
    if not pool:
        return 0.5
    below = sum(1 for p in pool if p < value)
    equal = sum(1 for p in pool if p == value)
    return (below + 0.5 * equal) / len(pool)


def percentile_of(value: float, history: list[float]) -> float:
    return _mid_rank(value, history)


def rank_within(value: float, peers: list[float]) -> float:
    """Cross-sectional rank in [0,1]. Used before we have IV history."""
    if len(peers) < 2:
        return 0.5
    return _mid_rank(value, peers)


def build(
    raw: dict[str, dict],
    iv_history: dict[str, list[float]],
) -> list[Signal]:
    """`raw` maps ticker -> {"iv": float|None, "bars": [...]}.

    Returns one Signal per ticker, unsorted.
    """
    ivs_now = [v["iv"] for v in raw.values() if v.get("iv") is not None]

    out: list[Signal] = []
    for ticker, payload in raw.items():
        iv = payload.get("iv")
        bars = payload.get("bars") or []

        rv = realized_vol(bars, config.RV_LOOKBACK_DAYS)
        mom = momentum(bars, config.MOMENTUM_LOOKBACK_DAYS)
        rv_iv = (rv / iv) if (rv is not None and iv) else None

        hist = iv_history.get(ticker, [])
        if iv is None:
            iv_rank, method = 0.0, "unavailable"
        elif len(hist) >= MIN_IV_HISTORY:
            iv_rank, method = percentile_of(iv, hist), "timeseries"
        else:
            iv_rank, method = rank_within(iv, ivs_now), "cross_section"

        # rv_iv below 1.0 means IV exceeds realised -- the structural edge we
        # are trying to harvest. Map it so lower ratio scores higher, clipped.
        rv_iv_score = 0.5 if rv_iv is None else max(0.0, min(1.0, 1.0 - rv_iv))

        # Mild negative momentum is fine for put selling; sharp downtrends are
        # not. Penalise magnitude of decline, ignore upside.
        mom_score = max(0.0, min(1.0, 1.0 + (mom * 10))) if mom < 0 else 1.0

        w = config.SIGNAL_WEIGHTS
        score = (
            w["iv_percentile"] * iv_rank
            + w["momentum"] * mom_score
            + w["rv_iv"] * rv_iv_score
        )

        out.append(
            Signal(ticker, score, iv, iv_rank, method, mom, rv, rv_iv)
        )
    return out


def eligible(signals: list[Signal]) -> list[Signal]:
    """Relative rank with a floor -- take the top N that clear SIGNAL_FLOOR.

    Not an absolute threshold: IV rank across this universe is depressed all
    week, and an absolute cut finds zero candidates for four straight days.
    """
    ranked = sorted(signals, key=lambda s: s.score, reverse=True)
    return [s for s in ranked[: config.SIGNAL_TOP_N] if s.score >= config.SIGNAL_FLOOR]


def record_iv(iv_history: dict[str, list[float]], signals: list[Signal],
              cap: int = 400) -> None:
    """Accumulate IV observations so the time-series percentile becomes usable
    as the week progresses. Mutates in place."""
    for s in signals:
        if s.iv is None:
            continue
        hist = iv_history.setdefault(s.ticker, [])
        hist.append(s.iv)
        if len(hist) > cap:
            del hist[: len(hist) - cap]
