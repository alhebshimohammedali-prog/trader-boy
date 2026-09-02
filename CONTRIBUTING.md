# Working on this repo

Do not read the commit history to get oriented. Read six files in the order
below and you will understand the whole system; the history is there to explain
*why* a specific line looks odd, once you already know what it does.

## Read in this order

| # | File | What it gives you |
|---|---|---|
| 1 | `README.md` — "How a cycle runs" and "Architecture" | the nine steps, and what the LLM is and is not allowed to do |
| 2 | `config.py` | every tunable, each with the argument for its value. Nothing is fitted; if a number has no reason next to it, that is a bug |
| 3 | `src/agent.py` → `run_cycle()` | the spine. Every other module is called from here, in order |
| 4 | `src/gates.py` | the eight risk gates. These run *before* the model sees anything |
| 5 | `src/allocation.py` → `pwt()` and `select()` | the allocation index, which is the novel part of the project |
| 6 | `src/exits.py` | position management, and the one place the economics are subtle |

Everything else (`data.py`, `mcp_client.py`, `execution.py`, `logbook.py`,
`reconcile.py`, `state.py`, `ledger.py`) is plumbing you can read when you need
it.

## Invariants — do not break these without discussion

These are not style preferences. Each one is load-bearing, and most were
learned the hard way.

1. **Gates run before the model.** A gate the LLM can reason past is not a gate.
2. **The model may only subtract exposure** — veto or shrink, never widen, never
   re-select. Its entire surface is a JSON verdict.
3. **Entries fail closed; exits fail open.** A model timeout must cost a trade
   we skipped, never a position we failed to close. That is why there is no
   model call in `exits.py` at all.
4. **The agent never flattens itself.** It closes single positions on their own
   state. Closing the whole book on one signal is `--flatten`, human only.
5. **An exit is a purchase.** Its price is the extrinsic value given up, and no
   rule may pay away more time value than the position ever earned. On a
   cash-secured put the intrinsic is owed either way.
6. **Every term in the index is bounded.** An unbounded term eventually becomes
   the whole policy — `age` did exactly that and put 40% of the book in one
   name before it was caught.
7. **Alpaca is the source of truth**, `state.json` is a cache that converges to
   it. Anything the broker can be asked should be asked, not persisted.
8. **`client_order_id` is the idempotency key.** It is derived from cycle and
   contract so a restart can ask "did I already send this?" before placing.
9. **A rejection that is returned rather than raised must be turned into an
   exception.** This has cost the project twice.
10. **Missing data is not evidence.** A null delta, an unquoted name and an
    unreported open interest all rank neutral — never best, never worst.

## Traps that will cost you a day

- **Never run `tools/scenarios.py` or `tools/offline_cycle.py` from the repo
  root.** Both drive a real `Agent`, and `State.save()` ignores the path the
  harness sets, so they overwrite the live `state.json` with fixture data.
  `tools/selftest.py` is safe. Back up `state.json` before any test run.
- **The venv has an editable install pointing at this tree.** A script run from
  `tools/` without `PYTHONPATH` set imports the *installed* `src/`, not your
  working copy — so your changes appear to do nothing and the suite reports
  green against code you did not edit.
- **`tools/` is not on the agent's import path.** Adding a file there cannot
  affect a running agent. `src/`, `config.py` and `run.py` can.
- **The machine clock is not market time.** Every decision goes through
  `now_et()`. A `date.today()` anywhere in a trading path is a bug — that error
  once made a 4 Sep contract read as 2 DTE instead of 3.
- **Fakes must match the real payload shape.** A fixture that accepts anything
  agrees with broken code: the breaker scenario passed for weeks while
  exercising a fallback rather than the path it was named after.

## Running it

```bash
uv venv .venv --python 3.11
uv pip install -e .
cp .env.example .env          # then add YOUR OWN Alpaca keys
```

`.env` is gitignored and has never been committed. Use your own paper keys —
nobody else's belong in this repo, and no key in the history means none to
rotate.

```bash
python tools/selftest.py       # unit regression, no network, no credentials
python tools/scenarios.py      # failure paths (see the state.json warning above)
python run.py --once --dry     # one live cycle, decides everything, sends nothing
python tools/watch_book.py     # read-only view of the open book
```

Run `selftest.py` after touching `config.py`. Every knob there is one edit away
from silently disarming a risk gate, and a disarmed gate raises no exception —
it just stops enforcing.

## Where the evidence lives

`runs/<timestamp>/cycles.jsonl` is the auditable trace, one record per cycle.
`runs/ledger.jsonl` accumulates closed trades across sessions.
`tools/report.py` renders a run as a single self-contained HTML file.

Treat `runs/` as append-only history. Do not rewrite it, and do not force-push
`main` — those logs are the project's evidence that the agent decided what it
says it decided.
