# Attention Weighted, build spec

Autonomous options-trading agent. Alpaca AI Trading Agents Hackathon.

Official measurement window: Mon 31 Aug 09:30 ET → equity marked EOD Thu 3 Sep.

This is the working spec. Anything not listed under "Must have" is optional
and can be added while the agent is already live.

Sources of truth: Alpaca's official guidelines + FAQ doc, and the lablab.ai
event page. Where they disagree, Alpaca governs P&L and judging; lablab governs
submission mechanics.

---

## 0. Non-negotiables

- Alpaca Trading API + Alpaca MCP server (V2) or CLI. Not raw REST alone.
- Options strategy. Autonomous, no human approval in the loop.
- Official account is brand new, funded at exactly $100,000, and begins
  trading Mon 31 Aug 09:30 ET. Dev/testing happens on a separate paper
  account. Reused accounts are not eligible for judging.
- Judged on total account equity, not cash balance (mark-to-market;
  unrealised counts) + creativity / autonomy / robustness of the workflow.
  Alpaca states explicitly that winners are not selected on P&L alone.
- Any pre-event work must be disclosed in the README. Reusing your own
  prior libraries is permitted; hiding it is not.
- No UI required. Alpaca: "If the agent runs autonomously and only places
  orders, a GitHub repository is sufficient." Do not build a dashboard.

---

## 1. Calendar reality

Four sessions. Mon 31, Tue 1, Wed 2, Thu 3. That is the whole game.

Equity is marked at EOD Thursday 3 Sep. The FAQ elsewhere says a snapshot
is taken Fri 4 Sep 09:30 ET; under either reading **Friday trading is worth
nothing**. Do not plan a Friday. Nonfarm Payrolls at Fri 08:30 lands after the
number you are scored on is already fixed. It is not a risk to manage.

Macro events: hard-block new entries in a window around each:

| When | Event |
|---|---|
| Mon 09:45 | Chicago PMI |
| Tue 10:00 | ISM Manufacturing + JOLTS |
| Wed 08:15 | ADP payrolls |
| Thu 10:00 | ISM Services |

Expiry calendar: this drives the whole strategy.

Single-name equities list Friday weeklies only. From a Monday 31 Aug entry
that means exactly two choices:

| Expiry | DTE Mon | State at Thu-close mark |
|---|---|---|
| **Fri 4 Sep** | 4 | ~1 day left, nearly all premium decayed |
| Fri 11 Sep | 11 | 8 days left, roughly a third decayed |

There is no 6–9 DTE contract for mega-cap single names in this window.
SPY / QQQ / IWM additionally list Wed 2 Sep and Thu 3 Sep.

Earnings inside the window: excluded from universe:

> **UNVERIFIED. Confirm against a live earnings calendar before the first
> entry Monday.** These dates were carried into the spec unchecked and may be
> from a prior-year calendar. A wrong exclusion list silently disarms gate 8.

- Tue AMC: DELL, PANW, MDB, CRDO, GTLB
- Wed AMC: AVGO, SNOW, HPE, NTAP, AI, PVH, FIVE
- Thu AMC: LULU, ZS, DOCU, PATH, ASAN, GWRE, IOT
- BMO: MDT, CIEN, CPB, TTC, NIO

---

## 2. Universe

6–8 liquid names, none reporting during the window. Candidates:
AAPL, MSFT, AMZN, GOOGL, META, TSLA, NVDA, JPM, XOM, plus SPY / QQQ / IWM.

Width matters: with fewer than ~6 names, capital is never scarce, candidates
never queue, and the allocation layer arbitrates nothing.

Do this before writing strategy code. Collateral for one cash-secured put
is `strike × 100`. At a 25% per-position cap on $100,000 that is $25,000, so:

> Any name whose target strike exceeds $250 is untradeable at one contract.

Fill this in Monday pre-open and drop the failures:

| Ticker | Spot | Target strike (~1–2.5% OTM) | Collateral | ≤ $25,000? |
|---|---|---|---|---|
| | | | | |

If fewer than six names clear, the options are: raise the cap toward 30%
(strike ≤ $300, weakens diversification), or lean the universe toward
sub-$250 names and IWM. Do not silently run a three-name universe, that
guts §6.

Note: mega-cap earnings season just ended, so IV rank across this universe
will be depressed all week. Use a relative rank (score the universe, take
the top one or two) with a floor, not an absolute threshold, or the agent
will find zero candidates for four straight days.

---

## 3. Strategy

Puts-led wheel. Sell cash-secured puts as the engine. If assigned, only sell a
covered call if the strike is comfortably above cost basis, never mechanically
roll a call that locks in a loss.

Expiry targeting (replaces the old 6–9 DTE rule):

- Default: sell the Fri 4 Sep expiry. Mon entry = 4 DTE, Tue = 3, Wed = 2.
  It expires *after* the mark, so you capture almost all the decay and never
  settle an assignment inside the measured window.
- SPY/QQQ/IWM Wed 2 Sep and Thu 3 Sep expiries are permitted. They realise
  fully before the mark. But they *do* settle inside the window, so
  assignment is a live outcome. Size accordingly.
- Fri 11 Sep is a last resort. Eight days of undecayed premium still on
  the book at the mark is a strictly worse position.
- Entry window closes Thu 12:00 ET. After that, manage only.

Why short-dated is right on its own terms, not just because of the mark.
The term structure of the price of variance risk is *downward sloping*.
Variance risk pricing decreases in absolute value with maturity, in both normal
and stressed regimes (NY Fed Staff Report 736). So the premium we are
harvesting is structurally densest at short tenors. The 4 Sep expiry is not
merely a scoring-window artifact; it is where the edge, such as it is, lives.
State it that way. It converts a constraint into a design choice.

Strike: delta is primary, band 16–30, but target the low end (0.18),
not the middle. Three reasons, and they all point the same way:

1. 16-delta sits at the peak of VRP overstatement and shows ~78% win rates
   across IV environments versus ~65% at 30-delta. Tastytrade's SPY backtest
   found 16-delta puts expired OTM ~95% of the time against the ~84% implied
   by delta.
2. Gamma rises exponentially into expiry. A ~1% move can swing delta from
   0.50 to 0.95 within hours. Our Monday 4-DTE position is a 1-DTE position on
   Thursday, which is exactly when equity is marked. Buying distance at entry
   is cheaper than managing gamma later, and we have no bracket orders to
   manage it with.
3. Over four sessions premium capture is ~0.09% while one adverse mark
   dominates the result. We are optimising for **probability of a non-negative
   mark**, not premium collected. Lower delta means less directional exposure,
   which means less of the outcome decided by the coin flip we already admit we
   cannot forecast.

30-delta earns more per winning trade. That is the wrong objective here and the
write-up should say why.

At 4 DTE the old "3–5% OTM" fallback is *not* equivalent to 16–30 delta. It
lands nearer 5–10 delta on a low-vol mega-cap and collects almost nothing.
Recalibrated fallback: ~1–2.5% OTM at ≤4 DTE. Delta governs; moneyness is
only the fallback when Alpaca returns null.

Strike fallback (required): Alpaca can't always solve IV on OTM near-dated
contracts, so delta comes back null exactly where you're selling. Fall back to
percentage moneyness as above, or compute Black-Scholes delta yourself.

Excluded: naked short calls, spreads, straddles, iron condors, 0DTE.
Alpaca imposes no strategy restrictions. These are our design choices and
should be defended as such in the write-up, not cited as rules.

Signal score (admission threshold, not selection):
IV percentile (more robust than IV rank), momentum, realised-vs-implied vol.
Weighted, hand-designed. No trained model.

---

## 4. Risk gates

Every gate is a boolean evaluated before the model sees the candidate.
A gate the LLM can reason past is not a gate. Log every rejection with reason.

| # | Gate | Setting | Notes |
|---|---|---|---|
| 1 | No naked options | structural | Alpaca enforces at level 1 too. Say so honestly |
| 2 | Per-position cap | **20–25% of equity** | collateral = strike × 100; 10% makes most names untradeable |
| 3 | Liquidity floor | OI ≥ 100; spread ≤ **$0.10 absolute OR ~20% relative**, whichever is more permissive | 10% relative rejects normal weekly quotes |
| 4 | **Expiry containment** | **rewritten. See below** | the old min-DTE rule is now actively harmful |
| 5 | Loss circuit breaker | 3% drawdown vs day's high-water mark, mark-to-market total equity | must **also** close/hedge largest-delta position, not just halt entries |
| 6 | Portfolio capital cap | total collateral ≤ 50–60% of equity | per-position caps don't stop 100% deployment |
| 7 | Economic calendar | block entries in window around each event in §1 | |
| 8 | Earnings | static exclusion list from §1. **verify the list first** | |

Gate 4, rewritten. The old rule ("close/roll inside 3 DTE") would force
closing a 4 Sep put on Wednesday. Paying the spread to hand back exactly the
decay the position exists to collect, one day before the mark. In a four-day
window it is a bug, not a safeguard. Replace with:

- No position may be held through an expiry occurring on or before Thu 3 Sep
  unless assignment is an accepted outcome and the collateral is already
  reserved. (Applies only to SPY/QQQ/IWM 2 Sep and 3 Sep contracts.)
- Positions expiring after the mark (4 Sep, 11 Sep) are held to the mark.
- Force-close any short put that goes ITM intraday, at any DTE.

**Why the ITM force-close is the load-bearing rule, and why "4 Sep expires
after the mark" is not the protection it looks like.** Every name in the
universe, equities and SPY/QQQ/IWM alike, is American-style. Early
assignment is available to the holder at any time, so "the contract expires
after we are scored" only rules out *expiry* settlement, not *exercise*.

For a short put, early exercise becomes rational for the holder when the
interest earned on the strike proceeds exceeds the remaining extrinsic value:
deep ITM, close to expiry, non-trivial rates. That is precisely the
Wednesday/Thursday state of a 4 Sep put that has gone against us. So the real
assignment risk is inside the measured window, not after it, and the ITM
force-close is what actually contains it. Say this in the write-up; "it expires
Friday so assignment can't affect us" is wrong and a judge with options
experience will know it.

This is also the second reason for the 0.18 delta target: the cheapest way to
avoid early assignment is to be far enough OTM that the question never arises.

Do not flatten before the Thursday mark. Unrealised P&L counts, and closing
costs the spread. Holding a decayed short put to the mark is worth strictly
more than buying it back. The only reasons to close Thursday are gate 4 ITM or
gate 5.

---

## 5. Architecture

1. Data. Account, positions, open orders, chain, Greeks, bars (via MCP).
2. State. What we hold and what's pending, read from Alpaca every cycle.
   Never decide from scratch.
3. Signal. IV percentile, momentum, RV/IV. Produces eligible set.
4. Gates. §4. Produces runnable set.
5. Allocation. §6. Selects which runnable candidate gets capital.
6. LLM decision. Reasons over the selected candidate. Can refine or veto,
   cannot override gates or re-select.
7. Self-critique. Second pass argues against the trade. Optional, add Tuesday.
8. Execution. Place order with client-side order ID.
9. Reconciliation. Verify fills/rejections/partials against Alpaca.
10. Logging. Every decision including "no trade" and why.

Cycle cadence: every 15 minutes, 09:30–15:45 ET. ≈25 cycles/day, ≈100 over
the window. This matters for §6: on a daily cycle, `age` never exceeds 4 and
the allocation layer has nothing to arbitrate. Exits are evaluated every cycle;
entries every cycle but subject to §3's Thu 12:00 ET cutoff.

---

## 6. Allocation layer (the novelty)

Capital allocation under contention is a scheduling problem: a finite resource,
jobs of known duration, arriving asynchronously. We score every runnable
candidate on a single scalar and take the highest.

```python
def pwt(candidate, cycle, state, runnable, equity):
    age  = cycle - state.first_qualified[candidate.ticker]
    ubt  = state.capital_days_used[candidate.ticker]
    # Resource-time of the OTHERS. It MUST exclude self.
    opbt = sum(c.collateral / equity * c.dte for c in runnable if c is not candidate)
    return age - ubt + opbt

choice = max(runnable, key=lambda c: (pwt(...), -c.spread, c.ticker))
```

**`opbt` must exclude the candidate itself.** Include its own size and the sign
of the mechanism inverts: the allocator starts preferring the largest,
longest-dated position instead of the cheapest. We shipped that inversion first
and caught it in testing.

Three properties, none of them separately coded:

- passed-over candidates gain `age` each cycle, so nothing starves;
- a name demanding more equity for longer scores lower, so capital efficiency
  is intrinsic;
- a ticker already holding capital carries high `ubt`, which is diversification
  without a concentration rule.

**Sizing is computed before scoring** (`opbt` needs collateral), or cycle one
has a circular dependency.

**Theory grounding for the write-up.** Gittins (1979) showed the optimal policy
for sequential resource allocation under contention reduces to an *index
policy*: one scalar per alternative, take the max. This is an index policy with
an explicit fairness term, the standard restless-bandit-with-fairness shape.
Claim structural fit, not optimality: a true Gittins index needs a stochastic
reward model we cannot estimate from four sessions.

**State the missing reward term before a judge finds it.** Signal gates
admission but never ranks, so the index has a fairness term and a cost term and
no reward term. That is deliberate, since the signal is too weak to rank on
without inventing precision, and a reward-augmented index is the natural future
work. Unstated, it reads as an oversight.

## 7. Platform landmines

Each of these produces a silent failure. All confirmed in Alpaca's docs.

- Paper only fills marketable orders. A sell limit at mid never fills. Price
  at or through the bid. This is the #1 cause of a week with zero trades.
- Random partial fills ~10% of eligible orders. Reconciliation must handle
  the remainder without double-ordering.
- Order size isn't checked against NBBO liquidity. Gate 3 protects nothing
  mechanically. It's for realism and credibility. Say so.
- Greeks/IV missing when: bid or ask is zero, contract is 0DTE, or IV fails
  to converge (common on OTM near-dated). Need the §3 fallback.
- NTAs sync next-day on paper. Assignment/exercise/expiry won't appear in
  activities until tomorrow. Detect assignment by diffing positions, never
  by reading activities.
- No websocket for assignments at all. REST polling only.
- No bracket/OTO for options. Gate 4 must be agent-driven. MCP option
  orders support market, limit, stop, stop-limit. Trailing stop is stocks only.
- ITM auto-exercises at ≥$0.01. If buying power is short, Alpaca sells the
  position out within the hour before expiry.
- Indicative feed ≠ OPRA. You price from indicative quotes; paper fills
  against real NBBO. Keep fill tolerance wide, log both. OPRA requires Algo
  Trader Plus and is not granted for the event.
- Latest option quotes are real-time on the free Basic tier. The 15-minute
  restriction applies to *historical bars and trades*, not the latest quote.
  Live pricing off the free tier is fine; only the backtest path is delayed.
  Dashboard charts lag. Always trust the API over the UI.
- MCP V2 renamed everything. Any tutorial older than a few months has wrong
  tool names. Clear the client tool cache after configuring.
- Historical options data starts Feb 2024. That's the whole backtest window.

Useful MCP V2 tools: `get_option_chain`, `get_option_snapshot` (Greeks + IV),
`place_option_order`, `get_order_by_client_id` (idempotency),
`get_all_positions`, `get_account_activities_by_type`.

Scope the server with `ALPACA_TOOLSETS` (drop crypto, watchlists, fixed income).
Least-privilege on a trading agent is a credible robustness argument and keeps
you under client tool limits. `ALPACA_PAPER_TRADE` defaults true. Fails safe.

---

## 8. Logging schema

Per cycle, write one record containing:

- timestamp, cycle number, account equity, buying power
- every candidate: ticker, signal score, gate results (pass/fail + reason)
- every runnable candidate: `pwt`, `age`, `ubt`, `opbt`. Selected and
  rejected alike
- the selection and why
- LLM reasoning, and the self-critique if present
- order submitted (with client ID), fill result, reconciliation delta
- explicit "no trade" records with the reason

The runnable-candidate table is the evidence the allocation layer is load-bearing
rather than decorative. Without it the novelty claim is unsupported.

Report these metrics in the write-up: they stay meaningful regardless of
which way the market went:
capital utilisation %, mean candidate wait-cycles, Herfindahl concentration
across tickers, premium collected per capital-day.

---

## 9. Build order

Must have (build first, in this order):

1. Execution that actually fills.
2. State + reconciliation + idempotent order IDs.
3. The eight gates, especially the circuit breaker and the rewritten gate 4.
4. Allocation + the candidate log.

Add later, while live:
self-critique pass, post-trade reflection, adaptive signal weights, news
sentiment.

Do not spend Sunday on the interesting layers and discover Monday that limit
orders never fill.

---

## 10. Monday timeline

You have no market hours before the official window opens, so the dry run and
the official start collide. Alpaca wants trading to begin at 09:30; against a
four-session window a late start costs proportionally more than it used to.
Still the right trade. An account that never filled an order scores zero.

- 09:30 ET. Dry run on the dev account (below).
- ~10:15–10:30 ET. Start the official account.

Dry run, on dev:

1. Confirm options trading level ≥ 1 and buying power.
2. Pull the 4 Sep chain for the whole universe. Count null Greeks. Fill in the
   §2 collateral table and finalise the universe.
3. Submit a short put at mid. Confirm it does not fill.
4. Cancel, re-place at the bid. Confirm it fills. Record slippage.
5. Verify position, order status, and `get_order_by_client_id` round-trip.
6. Hard-kill mid-cycle, restart, confirm no duplicate order.
7. Buy to close, confirm reconciliation matches Alpaca not internal assumption.
8. Run one full autonomous cycle, read the log as a judge would.

Go/no-go for the official account: a real fill, a clean restart, and a
populated candidate table. If any is missing at 10:30, fix it first.

---

## 11. Write-up framing

One page, covering AI logic, risk gates, and Alpaca infrastructure.

- The LLM has no predictive edge on direction. Its role is orchestration:
  reading messy data, weighing conditions, deciding when *not* to trade,
  producing auditable reasoning.
- Any structural edge comes from implied vol tending to overstate realised vol.
  It is a property visible over many trades, not provable in four days. Say this
  rather than treating one week's P&L as evidence.
- Cboe-sponsored research (Bondarenko, *Historical Performance of Put-Writing
  Strategies*, 2019). Use these figures, they are checkable:

  | Feb 2006 – Dec 2015 | WPUT | PUT | S&P 500 TR |
  |---|---|---|---|
  | Annual compound return | 5.6% | 6.6% | 7.1% |
  | Max drawdown | **24.2%** | 32.7% | 50.9% |
  | Avg annual gross premium (06–18) | **37.1%** | 22.1% |. |

  Weekly put-writing collects far more gross premium and returns less: it wins
  on drawdown, not return. Over four sessions that is ≈0.09%. Premium
  capture is small relative to directional noise. Name this honestly; it is a
  stronger position than pretending otherwise, and it is the reason to invest
  remaining hours in §6 and §8 rather than signal tuning. The P&L spread
  across entrants is noise-dominated, and Alpaca has said outright that P&L is
  not the sole criterion.
- Verified, with counts. Audits of LLM-trading research find only 1 of 19
  studies reports an explicit transaction-cost model, 2 of 19 report
  time-consistent split protocols, and none achieve full reproducibility; a
  separate 30-study audit recovers cost or turnover treatment in 14 of 30.
  We measure realised slippage per fill and report it in `metrics.json`, which
  puts us in the minority that documents a cost model at all.
- We are structurally immune to the field's biggest methodological problem.
  The standard complaint is look-ahead bias: LLMs encode historical market data
  from pretraining, so clean out-of-sample backtests are impossible. This agent
  trades live on genuinely unseen future data, with no training, no backtest,
  and no fitted parameters. Most published work cannot claim that. Say it.
- Capped upside on covered calls and loss-locking on mechanical rolls are real
  criticisms of the wheel. The puts-led, non-mechanical design is a direct
  response to that critique, not an oversight.
- Backtests are explicitly welcome as supporting evidence: "You may include
  backtests and simulated shocks in the project write-up and repository as
  additional evidence of the agent's guardrails." The Feb-2024-onward options
  history is a limitation *and* an asset. Replay the gates against it and show
  the circuit breaker firing. This is the cheapest available robustness score.
- Disclosure section (required). State plainly what existed before kickoff:
  any boilerplate, infrastructure, or reused personal libraries. Alpaca permits
  all of it and requires it be disclosed.

---

## 12. Submission checklist

lablab.ai fields. Repo may stay private during the event; it must be
original and MIT-compliant.

- [ ] Project title, short description, long description, tech + category tags
- [ ] Cover image
- [ ] Video presentation + slide presentation
- [ ] Public GitHub repository (with README disclosure section per §11)
- [ ] Alpaca paper trading account ID. Required for judging; without it
      the P&L cannot be attributed
- [ ] One-page write-up (§11)
- [ ] Application URL. *not required*, see §0. Skip it.
- [ ] Up to 5 social posts on X / LinkedIn tagging @lablabai and @AlpacaHQ

Social engagement is a separate prize track, not a tiebreaker: 2 winning
teams × $500 + one month of Algo Trader Plus per member, judged on content
quality and on engagement. It requires no trading code. Start posting
immediately. A build log written during the build is worth more than a
retrospective written Thursday night.

No scoreboard this year. Don't build anything that assumes one, and expect
no competitive signal mid-week.

---

## 13. Deadlines

| What | When |
|---|---|
| Official trading begins | Mon 31 Aug 09:30 ET |
| Entry window closes | Thu 3 Sep 12:00 ET |
| **Equity marked** | **EOD Thu 3 Sep** |
| Measurement window formally ends | Fri 4 Sep 09:30 ET |
| **Submission deadline** | **Fri 4 Sep 11:00 ET** (= 23:00 China Standard Time) |

The lablab site renders the deadline as "11:00 PM CST", which reads as US
Central to an American eye, that would be 13 hours later than it is. It is
China Standard Time. 11:00 ET Friday.
