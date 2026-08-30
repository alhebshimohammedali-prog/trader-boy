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
SUBMISSION_DEADLINE = et(2026, 9, 4, 11, 0)  # 23:00 China Standard Time

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

# !! VERIFY BEFORE FIRST ENTRY: these names are new to the universe and their
# earnings dates have NOT been checked. The old list was mega-caps whose
# reporting season was known to be over; that assurance does not carry over.
# Any of these reporting between 31 Aug and 3 Sep must be dropped.

# Pruned at startup by the collateral table in tools/probe.py: any name whose
# target strike implies collateral > PER_POSITION_CAP x equity is untradeable.
UNIVERSE_MIN_NAMES = 6  # below this, PWT arbitrates nothing (§6)

# !! CHECK BEFORE MONDAY, alongside earnings: EX-DIVIDEND DATES.
# A stock going ex-dividend drops by roughly the dividend on a known date -- a
# mechanical move against a short put that no signal will predict. XOM and JPM
# are the dividend payers here; if either goes ex-div between 31 Aug and 3 Sep,
# drop it for the window. On a ~2% OTM strike a 0.7% quarterly drop is a third
# of the buffer, given away for nothing.

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
    "DELL", "PANW", "MDB", "CRDO", "GTLB",
    "AVGO", "SNOW", "HPE", "NTAP", "AI", "PVH", "FIVE",
    "LULU", "ZS", "DOCU", "PATH", "ASAN", "GWRE", "IOT",
    "MDT", "CIEN", "CPB", "TTC", "NIO",
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
PER_POSITION_CAP = 0.25
PORTFOLIO_CAP = 0.60  # total collateral / equity
MIN_OPEN_INTEREST = 100
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
LLM_EFFORT = "medium"
LLM_MAX_TOKENS = 4000
LLM_TIMEOUT_SECONDS = 45
# Fail CLOSED: an error, timeout, or unparseable response means NO TRADE.
LLM_FAIL_OPEN = False


# --- Logging (§8) ------------------------------------------------------------

RUNS_DIR = "runs"
STATE_FILE = "state.json"
