# Attention Weighted

An autonomous options agent that sells cash-secured puts, and decides which
candidate gets capital using an index policy built for allocation under
contention.

Built for the Alpaca AI Trading Agents Hackathon. Eight deterministic layers,
plus two LLM passes that can veto or shrink a trade and do nothing else.

Scoring window: Mon 31 Aug 09:30 ET, with equity marked at EOD Thu 3 Sep.

---

## The idea

When several candidates qualify at once, something has to pick which one gets
capital. The obvious answer is to take the highest signal, but that pours every
cycle into whichever name is loudest and starves everything else.

So the allocator is an **index policy**: score every runnable candidate on one
scalar, take the highest, recompute from scratch next cycle.

```
pwt = age - ubt + opbt + lambda*rank(yield)      select max(pwt)
```

| term | meaning here |
|---|---|
| `age` | cycles since this ticker first became runnable and was passed over |
| `ubt` | collateral-days this ticker has already consumed |
| `opbt` | committed resource-time of every **other** queued candidate |
| `rank(yield)` | this contract's rank on `(bid/strike) x (1-abs(delta)) / dte` |

Three properties fall out of that, and none of them is a rule we wrote:

- **Nothing starves.** Winning charges `ubt`, which is subtracted, so an
  incumbent's own success pushes it down the ranking until someone else
  overtakes it. The loudest name cannot monopolise capital.
- **Capital efficiency.** `opbt` excludes the candidate itself, so a name
  demanding more equity for longer scores *lower*. Cheaper, shorter commitments
  are preferred automatically.
- **Diversification.** A ticker already holding capital carries high `ubt` and
  drops down the ranking, without any concentration limit being coded.

One correction to how this is often described, including by us. Anti-starvation
is usually attributed to `age`, and that is wrong for the common case: every
candidate that stays continuously runnable accrues `age` at the same rate, so
the term is identical across them and cancels out of the comparison entirely.
Live cycles show exactly this, every runnable name sitting at the same age.
`age` only separates candidates that became runnable at *different* times.
Rotation in steady state is driven by `ubt`, and that distinction is what sets
the scale for `lambda` -- `ubt` increments are around 0.09 per win, so a reward
term denominated as though it competed with `age` would be an order of
magnitude too strong.

### Why this is not a forced analogy

Gittins (1979) showed that the optimal policy for allocating a scarce resource
sequentially under contention reduces to an index policy: one scalar per
alternative, take the max. PWT is an index policy with an explicit fairness
term, which is the standard restless-bandit-with-fairness shape.

We claim structural fit, not optimality. A real Gittins index needs a stochastic
reward model, and four sessions is nowhere near enough data to estimate one.

The reward term was added after the first version shipped without one. The
distinction that makes it admissible is that it is *observed* rather than
*predicted*: the bid, the strike, the delta and the days are all read off the
contract in front of us. The signal, by contrast, is a weak opinion about the
future, which is why it still gates admission and still never ranks. Ranking on
it would be inventing precision; ranking on the premium a contract actually
pays is arithmetic.

The `1 - abs(delta)` factor is load-bearing rather than decorative. Raw premium
yield ranks whatever sits closest to the money highest, every single time,
because that is where the premium is. An index maximising it would
systematically select maximum assignment risk and report it as efficiency.

"Attention Weighted" refers to this allocation layer, not to neural attention.
Nothing in this system is trained.

---

## Does it earn its place?

`tools/ablation.py` runs five allocation policies over the same candidate
stream. Synthetic, seeded, reproducible.

| policy | tickers | starved | HHI | worst gap | capital-time | premium/capital-day |
|---|---|---|---|---|---|---|
| pwt | 5/5 | 0 | 0.221 | 8 | 0.709 | 90.6 |
| pwt+reward | 5/5 | 0 | 0.221 | 8 | 0.709 | 91.6 |
| greedy | 1/5 | 4 | 1.00 | 40 | 0.712 | 150.9 |
| random | 5/5 | 0 | 0.214 | 16 | 0.744 | 90.6 |
| roundrobin | 5/5 | 0 | 0.200 | 5 | 0.766 | 91.3 |

### A correction to an earlier version of this table

An earlier fixture gave every contract the same `bid` of 1.10. Premium per
capital-day is `premium / capital-time`, so with the numerator held constant
that column reduces to `1 / capital-time`, which is the quantity PWT minimises
by construction. The metric was measuring the policy against itself, and the
13.8% and 10.2% margins it produced over random and round-robin were artifacts.
They are withdrawn.

With realistic premium dispersion, and with gate 2 applied so the allocator
only arbitrates over names the agent could actually hold, PWT does **not** beat
round-robin on premium yield. The three of them tie at roughly 90 to 91.

What PWT does buy, and what the table above supports:

- **Bounded rotation latency.** Worst gap 8 against random's 16. Random
  diversifies on average and abandons individual names for long stretches; the
  index policy has an actual bound.
- **Capital efficiency.** 0.709 capital-time per trade against 0.744 and 0.766.
  It reaches the same premium while committing less equity for less time.
- **No starvation, without a rule saying so.** Round-robin also achieves this,
  but only because rotation is the entire policy, so it cannot respond to
  anything else.

Greedy earns 150.9, far above everything else, and we are not hiding that. It
also starves four of five tickers and never touches them across forty cycles.
That is concentration risk this metric does not price, and on four sessions of
live trading it is the difference between one good week and one catastrophic
one.

### What the reward term is worth

`pwt+reward` adds `lambda x rank(premium yield)` to the index, where yield is
`(bid / strike) x (1 - |delta|) / dte`. Every input is read off the quote, so
it is arithmetic on the contract in front of us rather than a forecast, which
is what makes it admissible where the signal is not.

At the shipped `lambda = 0.3` it is worth **+1.1%** premium per capital-day
with no change to starvation, concentration, or worst gap. Raising lambda keeps
buying yield: +3.9% at 1.0, +9.7% at 5.0.

We ship 0.3 anyway, for a reason worth stating. Reward is scored on rank, so
with `n` contenders the gap between adjacent ranks is `lambda / (n - 1)` -- the
term is four times stronger in a two-horse race than in a five-horse one.
Lambda tuned on the five-name fixture and shipped at 1.0 breaks rotation
outright when only two candidates clear the gates: the incumbent keeps the seat
even after paying `ubt` for it. `selftest.py` asserts against exactly this.

Two contenders is not a corner case. It is what a late session looks like once
spreads widen and names get gated out, which is when concentration hurts most.
So lambda is chosen for the smallest contested set rather than the average one,
and +1.1% is taken over +9.7% because the larger number is measured on forty
synthetic cycles and the fairness guarantee is the reason this is an index
policy rather than greedy with extra steps.

`--from-log` replays the same comparison over candidate sets the agent actually
faced live, which turns the simulation into a counterfactual on real data.

---

## Architecture

| # | Layer | File |
|---|---|---|
| 1 | Data | `src/data.py`, `src/mcp_client.py` |
| 2 | State | `src/state.py` |
| 3 | Signal | `src/signal.py` |
| 4 | Gates | `src/gates.py` |
| 5 | Allocation | `src/allocation.py` |
| 6 | LLM decision | `src/decide.py` |
| 7 | Self-critique | `src/decide.py` |
| 8 | Execution | `src/execution.py` |
| 9 | Reconciliation | `src/reconcile.py` |
| 10 | Logging | `src/logbook.py` |

Gates run before the model sees a candidate, and the model holds no tools. It
cannot place an order, re-select, or reach a gate. Its entire surface is a JSON
verdict. A gate the LLM can reason past is not a gate, so we made that
structural instead of promising it in a prompt.

The same reasoning removed the wall clock from its payload. It receives time
deltas and never a timestamp, so reading the polling interval as a signal is
impossible rather than merely forbidden.

The LLM layer fails closed. A timeout, an HTTP error, or unparseable JSON all
mean no trade. The agent is a complete trading system without it.

### Two passes, one direction

A second call sees the same candidate plus the first verdict and argues against
it. It has veto and shrink authority and nothing else, and the two verdicts are
merged by taking the more conservative action and the smaller multiplier. A
critic that likes the trade cannot make it larger, and a critic that dislikes a
veto cannot revive it.

That asymmetry is what makes the second pass safe to add. It can only ever
subtract exposure, so the worst case of a confused critic is a trade we skipped,
never a trade we upsized. `tools/selftest.py` asserts every direction of that
merge, including the one that matters most: the critic failing.

Which is also why the critic, unlike the primary call, does *not* fail closed.
An optional second opinion timing out would otherwise manufacture a zero-trade
week out of a network problem. Its failure is logged and the first verdict
stands.

A third call writes one or two sentences of narration per cycle, including on
cycles where nothing traded. It has no authority over anything and exists so the
log reads as a decision record rather than a table dump.

### Strategy

Cash-secured short puts, single leg, all on the 4 Sep expiry. That expiry lands
after the mark, so almost all the decay is captured and nothing settles inside
the measured window. Strikes target 0.18 delta, the low end of a 16 to 30 band,
with percentage moneyness near 2.1% OTM as the fallback when Alpaca cannot solve
delta.

Black-Scholes is deliberately not that fallback. It needs the implied vol Alpaca
failed to solve, which is exactly why delta was missing in the first place.
Computing a delta from a guessed sigma would fabricate the number we are
claiming to lack.

---

## Running it

```bash
uv venv .venv --python 3.11
uv pip install -e .
cp .env.example .env          # then add Alpaca keys

python tools/selftest.py              # unit regression suite, run after any config edit
python tools/scenarios.py             # failure paths: breaker, assignment, restart, no-fill
python tools/allocation_demo.py              # allocation proof, no credentials
python tools/ablation.py              # PWT vs greedy / random / round-robin
python tools/ablation.py --from-log   # replayed over real logged candidates
python tools/offline_cycle.py         # full cycle against a fake broker
python tools/probe.py                 # live tool discovery, null-Greek census, collateral table
python run.py --once --dry            # one live cycle, no order sent
python run.py                         # the loop
python run.py --comp                  # the competition account
python run.py --flatten               # emergency: close all shorts and stop
```

`--comp` is the only thing standing between the dev account and the scored one.

Run `selftest.py` after touching `config.py`. Every knob in there is one edit
away from silently disarming a risk gate, and a disarmed gate raises no
exception. It just stops enforcing. The suite covers all gate rejection paths,
OCC parsing, the OPBT-excludes-self property, signal tie handling, fail-closed
LLM coercion, and session rollover.

`scenarios.py` is the other half. It drives the real agent against a broker
rigged to misbehave, and checks the safety machinery actually engages: the
breaker firing and closing the nearest-the-money short, assignment spotted by
diffing positions, a crash-restart not resending an order it already placed, an
unparseable symbol halting entries instead of mis-capping the portfolio, and an
order that never fills backing out cleanly.

### Safety rails

| Rail | Behaviour |
|---|---|
| Circuit breaker | 3% drawdown against the day's high-water mark halts entries and closes the highest-delta short |
| Unparseable position symbol | Halts entries rather than cap the portfolio against an understated number |
| Daily order cap | 10 per session; exceeding it trips the breaker, since something is looping |
| LLM failure | Fails closed, no trade |
| `--flatten` | Human-only. The agent has no self-flatten path, because one would eventually fire for a bad reason |
| State corruption | Atomic writes, plus a `.bak` of the last good copy |

---

## What testing found

Three defects that would never have raised an exception.

The port computed OPBT from the candidate's own size instead of the others',
biasing the allocator toward the largest and longest-dated position. That is the
opposite of capital efficiency and the opposite of the source algorithm. Caught
by re-deriving the term from first principles.

Tied signals ranked at the floor. A naive "count strictly below" ranking scores a
fully tied pool at 0.0. Implied vol clusters across this universe, so every
ticker would have failed the admission floor and the agent would have traded zero
times in four days. Ties now take the mid-rank.

Gate 8 is dormant. The earnings exclusion list has no overlap with the universe,
so it cannot fire. It is insurance against a universe change, not an active
control, and it is counted that way rather than claimed as one of eight live
gates.

---

## What we expect

A book of short puts is a long-delta position. Over four sessions, premium
capture is around 0.09%, and a 1% move in the universe swamps it. Cboe's own
research (Bondarenko, 2019) has WPUT at 5.6% a year against PUT at 6.6% and the
S&P at 7.1% from 2006 to 2015. Weekly put-writing collects far more gross premium
and returns less. It wins on drawdown, not on return.

So P&L over this window is substantially a coin flip on direction, and we are not
going to present one week of it as evidence of edge.

Whatever structural edge exists comes from implied vol tending to overstate
realised vol, which is visible over many trades and unprovable in four days.
Short-dated is where that premium is densest: the term structure of the price of
variance risk slopes downward (NY Fed SR 736), so the 4 Sep expiry is an economic
choice and not only an artifact of the scoring window.

The metrics we report stay meaningful either way, and land in
`runs/<ts>/metrics.json`: capital utilisation, mean candidate wait-cycles,
Herfindahl concentration, premium per capital-day, and measured slippage.

---

## Execution semantics and reproducibility

Audits of LLM-trading research find execution timing under-specified in most
studies, cost treatment recoverable in fewer than half, and reproducibility rare.
Against the reporting standard they propose:

| Item | This system |
|---|---|
| Universe and inclusion rules | `config.UNIVERSE_CANDIDATES`, pruned by the collateral check (`strike x 100` at or below 25% of equity) |
| Data provenance | Alpaca via `alpaca-mcp-server`, free tier. Latest quotes are real-time; historical bars carry a 15-minute delay. Every cycle is timestamped |
| Point-in-time discipline | Structurally impossible to violate. No backtest, no training, no fitted parameters. The agent trades forward on data that did not exist when it was written |
| Train/validation/test splits | None. Nothing is trained |
| Execution timing | 15-minute cycle, 09:30 to 15:45 ET. Sell-to-open limit priced at or through the bid, since paper only fills marketable orders. Day TIF. Polling backs off from 0.5s to 8s over a 90s timeout, then cancels and re-prices one cent through, up to twice |
| Transaction costs | Measured rather than assumed. `fill_price - quote_mid` per fill, aggregated as mean and worst slippage, total cost, and share of gross premium |
| Model versions and prompts | Logged per decision. System prompt lives in `src/decide.py`. MCP tool names are resolved at runtime and printed at startup |
| Seeds and retry policy | No randomness in the decision path. Ties break on `(-spread, ticker)`. `ablation.py` is seeded |
| Artifact release | This repo: code, prompt, config, per-cycle logs, metrics, ablation harness |

One gap we do not close. An LLM provider can change model versions underneath us
mid-run. It is logged per decision, so at least the drift is visible.

---

## Disclosure

Pre-event work: `BUILD.md`, the specification. No code predates the event. The
repository was initialised during the hackathon and every module in `src/` was
written for it.

Prior work: the scoring function in `src/allocation.py` is adapted from the
authors' own earlier and unpublished research on resource allocation under
contention. That work is not public, and its derivation is deliberately not
reproduced here. The adaptation to capital, the objective it optimises, and
every line of this implementation were written during the event.

Third-party components: `alpaca-mcp-server` (Alpaca, run via `uvx`), and
`alpacahq/alpaca-skills` (Apache 2.0), which we read for reference but did not
vendor. It shaped three choices: resolving MCP tool names at runtime, building
order arguments from the tool's own schema, and proving paper mode from the
config flag rather than from an account response. Plus `mcp`, `httpx`,
`python-dotenv`, `tzdata`, and `anthropic`.

AI assistance: developed with AI coding assistance. The runtime LLM layer is a
separate, provider-agnostic API call in `src/decide.py`.

Not used: no trained model, no RAG, no vector store, no backtest-fitted
parameters. Signal weights in `config.py` are hand-designed, because four
sessions and a handful of trades is not data you can fit anything to.

---

## Disclaimers

Paper trading only. Not investment advice. Paper-trading results are hypothetical
and do not represent actual trading. Options trading is not suitable for all
investors; see
[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document).
