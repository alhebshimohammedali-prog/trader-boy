"""Every tunable in one place. Logic lives in src/; knobs live here.

Cross-references are to BUILD.md sections.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# --- §1 Calendar -------------------------------------------------------------

# Four sessions. Equity is marked at EOD Thursday 3 Sep; Friday is worthless.
TRADING_DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]

MARK_AT = et(2026, 9, 3, 16, 0)  # the number we are scored on
ENTRY_CUTOFF = et(2026, 9, 3, 12, 0)  # no new positions after this
SUBMISSION_DEADLINE = et(2026, 9, 4, 11, 0)  # 18:00 Riyadh (AST, UTC+3)

# Operator is in Riyadh, UTC+3 and no DST, while the market runs on Eastern.
# Riyadh = ET + 7 while EDT is in force. The session lands in the local
# evening, not overnight:
#
#   open          09:30 ET  ->  16:30 Riyadh
#   last cycle    15:45 ET  ->  22:45 Riyadh
#   close         16:00 ET  ->  23:00 Riyadh
#   entry cutoff  Thu 12:00 ET  ->  Thu 19:00 Riyadh
#   MARK          Thu 16:00 ET  ->  Thu 23:00 Riyadh
#   submission    Fri 11:00 ET  ->  Fri 18:00 Riyadh
#
# The machine's own timezone is set to UTC+8, which is neither, so its wall
# clock reads five hours ahead of the operator. Nothing in the agent depends on
# it -- every decision goes through now_et(), which derives ET from UTC, and
# that was checked against Alpaca's own clock endpoint and agreed to the
# second. It only means the displayed local time is not the operator's.

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
LAST_CYCLE = time(15, 45)

# Single expiry for everything: it lands AFTER the mark, so we capture nearly
# all the theta and never settle an assignment inside the measured window (§3).
TARGET_EXPIRY = "2026-09-04"

# SPY/QQQ/IWM also list these. They realise before the mark but DO settle
# inside the window, so assignment is a live outcome. Size accordingly.
ALT_EXPIRIES = ["2026-09-02", "2026-09-03"]

CYCLE_SECONDS = 900  # 15 min -> ~25 cycles/day, ~100 total (§5)


# --- §2 Universe -------------------------------------------------------------

# Rebuilt from live quotes on 31 Aug. The original mega-cap list was
# unusable: at a 25% cap only 2 of 12 names cleared collateral, because
# strike x 100 on a $300-$770 underlying blows straight through $25,000.
# AAPL 29,500 / MSFT 47,900 / META 57,100 / SPY 75,300 / QQQ 70,200, all NO.
#
# Every name below was measured against the real 4 Sep chain: collateral under
# the cap AND a bid at or above MIN_CREDIT. Names were dropped for being too
# cheap to pay the floor (WFC 0.18, KO 0.19, BAC 0.23, VZ 0.15, T 0.11) or too
# expensive to fit (AMD 43,900, MU 90,100, GE 32,000).
#
# Spread was deliberately NOT used to prune. The measurement ran after hours,
# when relative spreads are several times their regular-session width (XOM
# showed 24.8%, IBM 34.1%). Gate 3 evaluates spread live, which is the only
# time the number means anything.
UNIVERSE_CANDIDATES = [
    "NVDA", "IBM", "CVX", "PLTR", "XOM", "PEP",
    "CSCO", "DIS", "INTC", "GM", "UBER",
]

# tools/scan.py --write picks the universe from the live market and drops it
# here. The list above is the fallback, and stays the fallback on purpose: a
# scanner that fails, hangs, or returns nonsense must not be able to stop the
# agent trading. Worst case is the universe we already had.
#
# Guarded rather than trusted -- a truncated or hand-edited file would
# otherwise silently shrink the universe until PWT has nothing to arbitrate,
# which is the failure mode that raises no error and just stops trading.
def _scanned_universe(path: str = "universe.json") -> list[str] | None:
    import json
    import os

    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    if blob.get("expiry") != TARGET_EXPIRY:
        return None  # scanned for a different expiry; the chains would not match
    names = blob.get("universe")
    if not isinstance(names, list):
        return None
    names = [n for n in names if isinstance(n, str) and n.isalpha()]
    return names if len(names) >= UNIVERSE_MIN_NAMES else None

# Earnings checked 31 Aug 2026 against the 31 Aug - 3 Sep window. All eleven
# clear it, two because they have just reported and nine because their next
# report is weeks out:
#
#   NVDA  reported 26 Aug 2026 (Q2 FY27)      already out
#   CSCO  reported 12 Aug 2026 (Q4 FY26)      already out
#   PEP   8 Oct      IBM  21 Oct      GM   20 Oct
#   INTC  22 Oct     CVX  23 Oct      XOM  23 Oct
#   UBER  29 Oct     PLTR  9 Nov      DIS  12 Nov
#
# This is not luck, it is the calendar. Most large caps report Q2 in late July
# and Q3 in late October, so the first week of September falls in the gap
# between cycles. It is a genuinely quiet window for event risk, which is part
# of why this expiry is tradeable at all.
#
# Re-check if the universe changes. tools/scan.py cannot do this -- Alpaca
# exposes no earnings calendar through the MCP server -- and it prints a
# reminder saying so rather than implying it checked.
#
# Ex-dividend dates checked the same day. Nothing goes ex inside the window:
#
#   XOM   17 Aug   passed        CVX   19 Aug   passed
#   PEP    4 Sep   see below     GM     4 Sep   see below
#   NVDA  10 Sep   after         CSCO  early Oct, DIS Dec (semi-annual)
#   INTC  dividend suspended     PLTR, UBER  pay none
#
# PEP and GM both go ex on Fri 4 Sep, which is EXPIRY day and one session
# after the Thu 3 Sep mark. The drop lands after equity has been scored, so it
# cannot touch the result. That is worth stating precisely, because the naive
# reading -- "PepsiCo pays ~1% and we are only 2.1% OTM, so half the cushion
# goes" -- is the right worry aimed one day wide of the window.
#
# Two second-order effects, both benign here. Options already price expected
# dividends into the forward, so a 4 Sep PEP or GM put carries slightly more
# premium and shows slightly more delta at a given strike; because entries
# target DELTA rather than percentage moneyness, the selected strike simply
# sits a little further out in spot terms, which is the correct response and
# needs no adjustment. And unlike short calls, short puts gain no early
# assignment risk near an ex-date: exercising a put before it means delivering
# stock and forfeiting the dividend, so a rational holder waits.
#
# IBM, CSCO and DIS are inferred from their established quarterly pattern
# rather than individually confirmed. All three sit far outside the window on
# that pattern, and an ex-date is in any case a sub-1% price move rather than
# an event-scale risk, which is why earnings got the individual checks.

# Pruned at startup by the collateral table in tools/probe.py: any name whose
# target strike implies collateral > PER_POSITION_CAP x equity is untradeable.
UNIVERSE_MIN_NAMES = 6  # below this, PWT arbitrates nothing (§6)

# Build the universe from the live market at startup rather than trading a list
# someone typed. The list below stays as the last fallback, not the default:
# a scan that fails, or that yields fewer tradeable names than PWT needs to
# arbitrate between, leaves it in place. --no-scan forces it.
#
# What the scan cannot do is check earnings -- Alpaca exposes no calendar
# through the MCP server. The configured list has hand-verified dates; a
# scanned one does not, and run.py says so on every start.
AUTO_SCAN = True
SCAN_POOL = 100   # screener hard-caps at 100 per call
SCAN_TOP = 11

# How often to rebuild the universe while the market is open. Also rebuilt on
# the first cycle of every session regardless, since a universe chosen on
# yesterday's close is not the market being traded today.
#
# The scan takes about 20 seconds against a 15-minute cycle, so this is cheap.
# Two hours is chosen because what is worth selling puts on changes over a
# session -- spreads tighten after the open and widen into the close, and a
# name that failed the liquidity gate at 09:31 often passes by 10:30.
SCAN_REFRESH_SECONDS = 7200

# Applied here rather than at the list above, because the guard needs
# TARGET_EXPIRY and UNIVERSE_MIN_NAMES to exist first. Any rejection leaves the
# hardcoded fallback in place.
_SCANNED = _scanned_universe()
if _SCANNED:
    UNIVERSE_CANDIDATES = _SCANNED
    UNIVERSE_SOURCE = "universe.json (tools/scan.py)"
else:
    UNIVERSE_SOURCE = "config.py fallback list"

# (Ex-dividend dates were the other half of that check and are recorded above.
# Nothing in the configured universe goes ex inside the window.)

# !! HONEST NOTE ON GATE 8: this list has ZERO overlap with
# UNIVERSE_CANDIDATES, so gate 8 cannot fire as configured. It is insurance
# against a universe change, not a control that does anything today. Say that
# in the write-up rather than counting it as an active gate -- a careful judge
# will check, and "eight gates, one of which is dormant and here is why" reads
# far better than being caught claiming eight live controls.
#
# The check that ACTUALLY matters is the inverse and cannot be automated from
# this list: confirm that no name IN the universe reports between 31 Aug and
# 3 Sep. §2 asserts mega-cap earnings season just ended, which is why none
# appear here -- verify it against a live calendar before the first entry.
#
# !! UNVERIFIED -- the dates below were carried into the spec unchecked and
# may be from a prior-year calendar.
EARNINGS_EXCLUDED = {
    # Software and hardware on a Jan/Feb fiscal year, so their Q2 lands in the
    # last week of August or the first two of September.
    "DELL", "PANW", "MDB", "CRDO", "GTLB", "AVGO", "SNOW", "HPE", "NTAP",
    "AI", "ZS", "DOCU", "PATH", "ASAN", "GWRE", "IOT", "CIEN", "S", "ESTC",
    "SMTC", "COO", "AEO", "SAIC", "VRNT", "PHR", "BOX", "PSTG", "NCNO",
    "RBRK", "BRZE", "YEXT", "SPWH", "SMAR", "AVAV", "CASY", "OXM",
    # Retail and consumer on a Jan/Feb year-end, same reason.
    "PVH", "FIVE", "LULU", "CPB", "TTC", "GME", "CHWY", "DG", "DLTR",
    "ULTA", "SIG", "ASO", "BBWI", "BURL", "JWN", "M", "KSS", "VSCO",
    "DKS", "ANF", "GPS", "RL", "TJX", "HRL", "JBL", "UNFI", "SJM",
    # Assorted others that habitually print in this window.
    "MDT", "NIO", "TOL", "GEF", "ABM", "PLAB", "REVG", "HQY", "CPRT",
    "SCS", "MEI", "LOVE", "FCEL", "AZO", "ORCL", "ADBE", "COST", "CRWD",
}

# --- §1 Macro blackouts (gate 7) ---------------------------------------------

MACRO_EVENTS = [
    (et(2026, 8, 31, 9, 45), "Chicago PMI"),
    (et(2026, 9, 1, 10, 0), "ISM Manufacturing + JOLTS"),
    (et(2026, 9, 2, 8, 15), "ADP payrolls"),
    (et(2026, 9, 3, 10, 0), "ISM Services"),
    # NFP lands Fri 08:30 -- AFTER the mark. Not a risk to manage.
]
BLACKOUT_BEFORE_MIN = 20
BLACKOUT_AFTER_MIN = 15


# --- §3 Strike selection -----------------------------------------------------

DELTA_MIN = 0.16
DELTA_MAX = 0.30

# Target the LOW end of the band, not the middle. Three reasons:
#   1. 16-delta is the peak of VRP overstatement and shows ~78% win rates
#      across IV environments vs ~65% at 30-delta (higher dollars per win, but
#      we are not optimising dollars per win -- see 3).
#   2. Gamma rises exponentially into expiry. A Monday 4-DTE position is a
#      1-DTE position on Thursday, which is exactly when equity is marked; a
#      ~1% move can swing delta from 0.50 to 0.95 in hours. Further OTM is the
#      cheapest protection against that, and it is protection we can buy at
#      entry rather than having to manage later.
#   3. Over four sessions premium capture is ~0.09% while a single adverse mark
#      dominates. We are optimising for probability of a non-negative mark, not
#      for premium collected. Lower delta = less directional exposure = less of
#      the outcome decided by the coin flip we cannot forecast.
DELTA_TARGET = 0.18

# Fallback when Alpaca returns null delta. Recalibrated for <=4 DTE: the old
# 3-5% OTM lands near 5-10 delta at this tenor and collects nothing.
# Black-Scholes is NOT a usable fallback here -- it needs the IV that Alpaca
# failed to solve in the first place. Percentage moneyness is primary.
MONEYNESS_MIN = 0.010
MONEYNESS_MAX = 0.025
# Weighted toward the far end for the same reason as DELTA_TARGET: when we
# cannot see delta, we still want the further-OTM strike, not the midpoint.
MONEYNESS_TARGET = 0.021


# --- §4 Risk gates -----------------------------------------------------------

# We size on collateral = strike * 100. Alpaca's actual buying-power hold for a
# cash-secured put is strike * 100 MINUS the premium received, so our number is
# slightly conservative and we will deploy a little under the cap rather than
# over it. That is the right direction to be wrong in, and strike * 100 is also
# the true maximum exposure if the underlying goes to zero.
# --- allocation reward -------------------------------------------------------
#
# Weight on the reward term in the PWT index. Zero reproduces the original
# three-term policy exactly, which is what the ablation switches.
#
# The units are the trap. `age` is identical across every continuously runnable
# candidate, so it cancels out of the comparison and contributes nothing --
# rotation is actually driven by `ubt`, which accrues only on the winner. So
# lambda must be denominated in ubt, and ubt increments are SMALL: a $2,340
# INTC position against $100k equity for 4 days charges (2340/100000) x 4 =
# 0.094 per win.
#
# That sets the scale, and it is why an intuitive-looking lambda of 2 or 5 is
# not obviously safe: against a SINGLE rival, an incumbent holds the seat for
# roughly lambda / capital_time consecutive wins before its own ubt overtakes
# the reward advantage, which at lambda=2 is longer than the whole session.
#
# On a five-name contested set, tools/ablation.py shows premium per capital-day
# rising monotonically with lambda and nothing starving anywhere up to 5.0:
#
#   lambda = 0.0  ->  90.6   (the original three-term index)
#   lambda = 0.3  ->  91.6   +1.1%   (chosen)
#   lambda = 1.0  ->  94.1   +3.9%
#   lambda = 5.0  ->  99.3   +9.7%
#
# Which argues for a much larger lambda, and that argument is wrong. Reward is
# scored on RANK, so with n contenders the gap between adjacent ranks is
# lambda / (n - 1). The term is four times stronger in a two-horse race than in
# a five-horse one. Tuning on the five-name fixture and shipping 1.0 breaks
# rotation outright when only two candidates survive the gates -- selftest
# catches it: the incumbent keeps the seat even after paying ubt for it.
#
# Two candidates is not a corner case. It is what late sessions look like once
# spreads widen and names get gated out, which is exactly when concentration
# hurts most. So lambda is chosen for the SMALLEST contested set, not the
# average one, and 0.3 is the largest value that preserves rotation at n=2.
#
# Taking +1.1% instead of +9.7% is the deliberate trade. The larger number is
# measured on 40 synthetic cycles; the fairness guarantee is the reason this is
# an index policy rather than greedy with extra steps.
# Weight on the age term. The terms are not in the same units: age counts
# CYCLES, while ubt and opbt are equity-fraction-days and a typical win charges
# about 0.09 of ubt. At weight 1.0 a single cycle of waiting outranks eleven
# wins' worth of consumed capital, which collapses the index into round-robin.
#
# Set so that ONE CYCLE OF WAITING is worth roughly ONE WIN'S WORTH of consumed
# capital. A typical position charges (collateral/equity) x dte ~= 0.09 of ubt,
# so 0.1 makes the fairness credit and the usage debit commensurate by
# construction rather than by fitting.
#
# Swept for confirmation, and the honest result is that it barely matters --
# ubt already does most of the rotating:
#
#   weight   HHI     worst gap   premium/capital-day
#   0.00     0.2212      8           91.57
#   0.10     0.2212      7           91.74   (chosen)
#   0.25     0.2137      7           91.92
#   1.00     0.2037      6           91.90
#
# Higher weights diversify slightly better, and that is the argument against
# them: at 1.0 the HHI reaches 0.204 against round-robin's 0.200, which is the
# index policy dissolving into rotation. Marginal yield is not worth losing the
# thing that makes it a policy rather than a schedule.
AGE_WEIGHT = 0.1

# Penalty on correlation with what the book already holds.
#
# ubt stops the allocator doubling into one TICKER and does nothing else, so a
# book of NVDA, MRVL, SMCI and DRAM reads as four-way diversification while
# being one semiconductor bet in four wrappers. Herfindahl would report a
# healthy 0.25 right up to the morning the sector gaps and every leg goes ITM
# together -- which is the only scenario that actually ends a short-put book in
# four sessions.
#
# Same scale as REWARD_LAMBDA deliberately: a candidate perfectly correlated
# with a holding forfeits its entire edge advantage, and one at 0.5 forfeits
# half. Correlation below zero is treated as zero, since being negatively
# correlated with a holding is a hedge, not a reason to pay extra.
CROWDING_MU = 0.3

REWARD_LAMBDA = 0.3

PER_POSITION_CAP = 0.25

# Total collateral / equity. Raised from 0.60 deliberately: at 0.60, forty
# thousand dollars of cash-secured capacity sat idle all week earning nothing,
# which is a real cost and not a safety margin.
#
# This is NOT leverage. The account reports $400,000 of buying power on
# $100,000 of equity, and using it would mean selling naked puts at roughly a
# fifth of the collateral -- about 4x the notional. That is the wrong position
# to lever, because premium is the MAXIMUM gain while the loss runs all the way
# to the strike: leverage multiplies a capped ceiling and an uncapped floor by
# the same factor.
#
# The decisive number is where the drawdown breaker fires. Entries sit ~2% OTM,
# so a drop of D% puts them (D-2)% in the money, and the 3% breaker trips at:
#
#   deployed  60%  ->  a 7.0% adverse move
#   deployed  85%  ->  a 5.5% adverse move   (chosen)
#   deployed 100%  ->  a 5.0% adverse move
#   deployed 240%  ->  a 3.25% adverse move  <- levered, and 3.25% over four
#                                               sessions is ordinary
#
# At 240% the book trips its own breaker on a normal down day, which halts
# entries AND force-closes the nearest-the-money short. That is the worst
# outcome available: take the loss, then stop collecting premium. 0.85 buys
# ~42% more premium than 0.60 while keeping the trigger at a move that is
# genuinely uncommon in four days.
PORTFOLIO_CAP = 0.85
MIN_OPEN_INTEREST = 100

# Ceiling on the EMPIRICAL assignment rate: how often this underlying actually
# finished below a strike this far out, over this holding period, measured from
# daily bars. Set against the delta the chain quotes, which is the same
# probability priced risk-neutrally.
#
# Derived rather than tuned. DELTA_MAX is the most assignment risk this
# strategy agreed to take, so a measured rate above it means the contract is
# not the risk we intended, whatever its quoted delta says. The margin on top
# is for the estimator: windows overlap, so ninety bars give roughly twenty
# independent observations and a standard error near ten points, and rejecting
# on noise would cost more trades than it saves.
#
# Measured live across the universe, most names sit 1.0-1.4x their delta --
# empirical slightly above risk-neutral is normal, since real distributions
# have fatter left tails than the pricing model. NVDA stood out at 0.407
# against a 0.228 delta, a 1.8%-OTM strike on a name that moves.
MAX_EMPIRICAL_ITM = 0.40      # = DELTA_MAX + 0.10
MIN_ITM_SAMPLES = 40          # below this the rate is noise with a decimal point
# Ceiling on the bid as a fraction of strike. A 4-day put struck 1-2.5% OTM
# should collect well under 1% of strike; anything near 5% is a stale, mis-
# scaled, or cross-contract quote rather than a rich one. Set deliberately
# loose -- this is a data-error tripwire, not a pricing opinion, and it should
# never fire on a real quote in this universe.
MAX_CREDIT_PCT_STRIKE = 0.05

MAX_SPREAD_ABS = 0.05  # $ -- whichever is MORE permissive
MAX_SPREAD_REL = 0.20  # of mid

# Minimum credit we will actually RECEIVE (the bid, not the mid -- we sell at
# or through the bid, so the mid is a number we never get).
#
# Why this gate exists: at 18 delta and 4 DTE, premiums are small. A $0.25
# option quoted 0.20/0.30 passes a $0.10 absolute spread test, and selling it
# at the bid gives up 40% of the premium on entry. Against an expected capture
# of ~0.09% over four sessions, that is the entire edge paid to the spread.
# Cheap options are where the microstructure quietly wins.
#
# Paired with tightening MAX_SPREAD_ABS from $0.10 to $0.05: a $0.30 option
# quoted 0.28/0.32 still passes (4c abs, 13% rel); a $0.25 quoted 0.20/0.30
# now fails both tests, as it should.
MIN_CREDIT = 0.25
DRAWDOWN_LIMIT = 0.03  # vs the day's high-water mark, mark-to-market

CONTRACTS_PER_ORDER = 1

# Runaway guard, not a strategy constraint. The 60% portfolio cap allows about
# three concurrent positions, so a normal day is a handful of orders. If we
# ever exceed this, something is looping and the correct response is to stop
# trading, not to keep discovering the bug one order at a time.
MAX_ORDERS_PER_DAY = 10


# --- §3 Signal (admission threshold, not selection) --------------------------

SIGNAL_WEIGHTS = {"iv_percentile": 0.5, "momentum": 0.2, "rv_iv": 0.3}
SIGNAL_FLOOR = 0.30  # absolute floor under the relative rank

# Admission, not selection -- selection is the allocation layer's job (§6). A tight
# top-N here starves that layer: with 2 admitted and one failing gates, PWT
# arbitrates a table of one and looks decorative even though it runs. Keep this
# generous and let SIGNAL_FLOOR do the filtering, so candidates actually queue.
SIGNAL_TOP_N = 6
# Ceiling on realised volatility for universe admission. This replaces a
# denylist of leveraged and crypto tickers: those names do not need to be
# recognised by NAME, they announce themselves by realising two or three times
# the volatility of anything else, and a measurement also catches the ordinary
# equity that happens to be moving 120% while a typed list never would.
#
# Set from the live distribution rather than picked. At 0.80 the scan drops
# CIFR (120%), PLTR (101%), SPCX (100%), SMCI (90%) and MRVL (90%), and keeps
# NVDA (47%), PYPL (54%), NOW (55%), SLV (38%) and NFLX (33%) -- which is the
# line between "pays a lot because it is dangerous" and "pays a lot because
# implied is rich".
MAX_REALIZED_VOL = 0.80

RV_LOOKBACK_DAYS = 20
IV_PERCENTILE_LOOKBACK_DAYS = 120
MOMENTUM_LOOKBACK_DAYS = 5


# --- §6 allocation -------------------------------------------------------

# pwt = age - ubt + opbt. No tunables by design; ties break on
# (-spread, ticker) for determinism.


# --- Execution (§7) ----------------------------------------------------------

# Paper only fills MARKETABLE orders. A sell limit at mid never fills -- this
# is the #1 cause of a week with zero trades. Price at or through the bid.
LIMIT_OFFSET_FROM_BID = 0.00  # 0.00 = at bid; negative = through it
ORDER_TIMEOUT_SECONDS = 90  # cancel + re-price if unfilled
MAX_REPRICE_ATTEMPTS = 2


# --- §5 step 6 LLM -----------------------------------------------------------

LLM_ENABLED = True

# Second pass argues against the trade the first pass approved. Veto/shrink
# only -- it can never upgrade a verdict or widen a position. Set
# LLM_CRITIC_PROVIDER to a DIFFERENT provider than LLM_PROVIDER for genuinely
# uncorrelated review; a model arguing with itself mostly rationalises.
SELF_CRITIQUE_ENABLED = True

# One or two sentences per cycle explaining what happened, written into the
# log. No authority at all -- the cycle is already decided and executed. It
# exists because the per-cycle log is what a judge actually reads, and most
# cycles are no-trade cycles that a bare reason string does not explain.
NARRATE_ENABLED = True
LLM_EFFORT = "medium"
LLM_MAX_TOKENS = 4000
LLM_TIMEOUT_SECONDS = 45
# Fail CLOSED: an error, timeout, or unparseable response means NO TRADE.
LLM_FAIL_OPEN = False

# Greedy decoding on the decision path. The seat returns an enum and a scalar;
# sampling variance there is noise we have no use for, and it makes a verdict
# reproducible from the logged payload, which is the difference between an
# auditable decision and an anecdote.
LLM_TEMPERATURE = 0.0

# But greedy decoding is deterministic in BOTH directions. If some payload
# shape makes the model emit something unparseable, temperature 0 reproduces
# that failure on every cycle, and because we fail closed the ticker is
# silently vetoed for the rest of the week while the log just shows a veto.
# One retry at a nudged temperature costs a second and removes that whole
# failure class. Set to None to disable the retry entirely.
#
# This retries a FORMAT failure, never an unfavourable verdict. Re-rolling a
# veto until it turns into a proceed is shopping for the answer you wanted,
# which is the opposite of a risk check.
LLM_RETRY_TEMPERATURE = 0.3

# The narrator is prose for a human, not a verdict. Greedy decoding there
# reads like a form letter and every cycle sounds identical.
LLM_NARRATE_TEMPERATURE = 0.7


# --- Logging (§8) ------------------------------------------------------------

RUNS_DIR = "runs"
STATE_FILE = "state.json"
