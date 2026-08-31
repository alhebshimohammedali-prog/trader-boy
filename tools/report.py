"""Turn a run's logs into something a person will actually read.

    python tools/report.py                          # newest run
    python tools/report.py runs/20260903-093015     # a specific run
    python tools/report.py --out report.html

cycles.jsonl is the auditable record and is the right thing to replay, but it
is not the thing you hand someone. This renders the same records as a report:
metrics first, then every cycle in order with the gates that rejected, the
allocation table that arbitrated, both model verdicts, and the fill.

No-trade cycles are included deliberately and are most of them. A report
containing only trades cannot show the agent declining for good reasons, which
is half of what the run is evidence of.

Output is a self-contained HTML fragment -- no external CSS, fonts, or scripts,
so it opens offline and survives being emailed around.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

CSS = """
:root {
  --ink:#12161c; --paper:#fbfcfd; --panel:#f2f5f8; --rule:#dde3ea;
  --slate:#5a6473; --mute:#8b95a3;
  --accent:#b4762a; --accent-soft:#f2e4d0;
  --pass:#2f6f4f; --fail:#9c3b34; --idle:#6b7280;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink:#e6ebf2; --paper:#0e1216; --panel:#161c23; --rule:#262f39;
    --slate:#9aa5b4; --mute:#6f7b8a;
    --accent:#d9a05b; --accent-soft:#3a2c18;
    --pass:#6fbf90; --fail:#e0796f; --idle:#7c8794;
  }
}
:root[data-theme="dark"] {
  --ink:#e6ebf2; --paper:#0e1216; --panel:#161c23; --rule:#262f39;
  --slate:#9aa5b4; --mute:#6f7b8a;
  --accent:#d9a05b; --accent-soft:#3a2c18;
  --pass:#6fbf90; --fail:#e0796f; --idle:#7c8794;
}
:root[data-theme="light"] {
  --ink:#12161c; --paper:#fbfcfd; --panel:#f2f5f8; --rule:#dde3ea;
  --slate:#5a6473; --mute:#8b95a3;
  --accent:#b4762a; --accent-soft:#f2e4d0;
  --pass:#2f6f4f; --fail:#9c3b34; --idle:#6b7280;
}

:root { --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace; }

body { background:var(--paper); color:var(--ink);
  font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  margin:0; padding:2.5rem 1.25rem 5rem; }
.wrap { max-width: 60rem; margin:0 auto; display:flex; flex-direction:column; gap:2.5rem; }

h1 { font-size:1.55rem; font-weight:640; letter-spacing:-.018em; margin:0;
     text-wrap:balance; }
h2 { font-size:.72rem; font-weight:660; letter-spacing:.1em; text-transform:uppercase;
     color:var(--mute); margin:0 0 .85rem; }
.sub { color:var(--slate); margin:.4rem 0 0; font-size:.95rem; }

.head { border-bottom:2px solid var(--ink); padding-bottom:1.1rem; }
.meta { display:flex; flex-wrap:wrap; gap:.4rem 1.5rem; margin-top:.9rem;
        font-family:var(--mono); font-size:.78rem; color:var(--slate); }
.meta b { color:var(--ink); font-weight:600; }

.metrics { display:grid; gap:1px; background:var(--rule);
  grid-template-columns:repeat(auto-fit, minmax(9.5rem,1fr)); border:1px solid var(--rule); }
.metric { background:var(--paper); padding:.85rem .95rem; }
.metric .k { font-size:.66rem; letter-spacing:.07em; text-transform:uppercase;
             color:var(--mute); }
.metric .v { font-family:var(--mono); font-variant-numeric:tabular-nums;
             font-size:1.28rem; margin-top:.2rem; }

.cycle { border:1px solid var(--rule); background:var(--panel); }
.cycle > .bar { display:flex; flex-wrap:wrap; align-items:baseline; gap:.75rem;
  padding:.7rem .95rem; border-bottom:1px solid var(--rule); background:var(--paper); }
.cycle .n { font-family:var(--mono); font-weight:680; }
.cycle .ts { font-family:var(--mono); font-size:.76rem; color:var(--mute); }
.cycle .fig { margin-left:auto; font-family:var(--mono); font-size:.78rem;
              color:var(--slate); font-variant-numeric:tabular-nums; }
.body { padding:.95rem; display:flex; flex-direction:column; gap:1.05rem; }

.chip { font-family:var(--mono); font-size:.68rem; letter-spacing:.05em;
  text-transform:uppercase; padding:.16rem .5rem; border:1px solid currentColor; }
.chip.traded { color:var(--pass); } .chip.none { color:var(--idle); }
.chip.veto { color:var(--fail); } .chip.err { color:var(--fail); }

.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-family:var(--mono);
        font-size:.79rem; font-variant-numeric:tabular-nums; }
th { text-align:right; font-weight:600; color:var(--mute); padding:.3rem .55rem;
     border-bottom:1px solid var(--rule); font-size:.68rem; letter-spacing:.05em;
     text-transform:uppercase; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
td { padding:.3rem .55rem; border-bottom:1px solid var(--rule); text-align:right;
     white-space:nowrap; }
tr.sel td { background:var(--accent-soft); }
tr.sel td:first-child { font-weight:680; color:var(--accent); }

.gates { list-style:none; margin:0; padding:0; font-family:var(--mono);
         font-size:.78rem; display:flex; flex-direction:column; gap:.22rem; }
.gates li { color:var(--slate); }
.gates .t { color:var(--fail); font-weight:640; display:inline-block; min-width:4.5rem; }

.verdict { display:flex; flex-direction:column; gap:.5rem; }
.vrow { display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap;
        font-family:var(--mono); font-size:.79rem; }
.vrow .who { font-weight:680; min-width:4.2rem; }
.vrow .act { font-weight:640; }
.act.proceed { color:var(--pass); } .act.veto { color:var(--fail); }
.act.shrink { color:var(--accent); }
.vrow .model { color:var(--mute); font-size:.72rem; }
.reason { color:var(--slate); font-size:.86rem; margin:0; padding-left:.15rem;
          border-left:2px solid var(--rule); padding-left:.7rem; }

.narr { font-size:.95rem; color:var(--ink); margin:0; padding:.7rem .85rem;
        background:var(--paper); border:1px solid var(--rule); }
.narr::before { content:"NARRATED"; display:block; font-family:var(--mono);
  font-size:.62rem; letter-spacing:.1em; color:var(--mute); margin-bottom:.3rem; }

.fill { font-family:var(--mono); font-size:.79rem; padding:.6rem .8rem;
        background:var(--paper); border:1px solid var(--rule);
        font-variant-numeric:tabular-nums; }
.fill b { color:var(--pass); }

.foot { border-top:1px solid var(--rule); padding-top:1.1rem; color:var(--mute);
        font-size:.8rem; }
"""


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def newest_run() -> Path | None:
    runs = [Path(p).parent for p in glob.glob(f"{config.RUNS_DIR}/*/cycles.jsonl")]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def load(run: Path) -> tuple[list[dict], dict]:
    records = []
    with (run / "cycles.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    mpath = run / "metrics.json"
    metrics = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    return records, metrics


def fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.2f}"
    return str(v)


def verdict_row(who: str, d: dict, mark: str = "") -> str:
    act = str(d.get("action", "?")).lower()
    model = d.get("model") or d.get("provider") or ""
    tail = f' <span class="model">{esc(model)}{" - " + mark if mark else ""}</span>'
    return (f'<div class="vrow"><span class="who">{esc(who)}</span>'
            f'<span class="act {esc(act)}">{esc(act.upper())}</span>'
            f'<span>x{d.get("size_multiplier", 0):.2f}</span>{tail}</div>'
            f'<p class="reason">{esc(d.get("reasoning", ""))}</p>')


def render_cycle(r: dict) -> str:
    parts = []

    fill = r.get("fill") or {}
    if r.get("error"):
        chip = '<span class="chip err">cycle error</span>'
    elif fill.get("filled_qty"):
        chip = '<span class="chip traded">filled</span>'
    elif str((r.get("decision") or {}).get("action", "")) == "veto":
        chip = '<span class="chip veto">vetoed</span>'
    else:
        chip = '<span class="chip none">no trade</span>'

    eq, dep = r.get("equity") or 0.0, r.get("deployed_pct") or 0.0
    dd = r.get("drawdown") or 0.0
    parts.append(
        f'<div class="bar"><span class="n">cycle {esc(r.get("cycle", "?"))}</span>'
        f'{chip}<span class="ts">{esc(str(r.get("timestamp", ""))[:19])}</span>'
        f'<span class="fig">equity ${eq:,.0f} &nbsp; deployed {dep:.0%} '
        f'&nbsp; dd {dd:.2%}</span></div>')

    body = []

    failed = [g for g in (r.get("gate_results") or []) if not g.get("passed")]
    if failed:
        items = "".join(
            f'<li><span class="t">{esc(g.get("ticker", ""))}</span>'
            f'{esc(g.get("reason", ""))}</li>' for g in failed)
        body.append(f'<div><h2>Rejected by gates</h2><ul class="gates">{items}</ul></div>')

    table = r.get("runnable_table") or []
    if table:
        has_reward = any("reward" in row for row in table)
        cols = ["ticker", "signal", "age", "ubt", "opbt"]
        if has_reward:
            cols.append("reward")
        cols.append("pwt")
        head = "".join(f"<th>{c}</th>" for c in cols)
        rows = []
        for row in table:
            cells = [f'<td>{esc(row.get("ticker", ""))}</td>']
            for c in cols[1:]:
                v = row.get(c, 0)
                cells.append(f"<td>{v:.3f}</td>" if isinstance(v, float)
                             else f"<td>{esc(v)}</td>")
            cls = ' class="sel"' if row.get("selected") else ""
            rows.append(f"<tr{cls}>{''.join(cells)}</tr>")
        body.append(
            f'<div><h2>Allocation</h2><div class="scroll"><table>'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody>"
            f"</table></div></div>")

    first, crit = r.get("first_pass"), r.get("critique")
    vs = []
    if first:
        vs.append(verdict_row("Model", first))
    elif r.get("decision"):
        vs.append(verdict_row("Model", r["decision"]))
    if crit:
        if crit.get("error"):
            mark = "unavailable"
        elif crit.get("action") != (first or {}).get("action"):
            mark = "overrode"
        else:
            mark = "concurred"
        vs.append(verdict_row("Critic", crit, mark))
    if vs:
        body.append(f'<div><h2>Verdict</h2><div class="verdict">{"".join(vs)}</div></div>')

    if fill:
        slip = fill.get("slippage")
        slip_s = f"{slip:+.3f}" if isinstance(slip, (int, float)) else "n/a"
        body.append(
            f'<div class="fill"><b>{esc(fill.get("symbol", ""))}</b> &nbsp; '
            f'{esc(fill.get("status", "?"))} &nbsp; '
            f'{esc(fill.get("filled_qty", 0))}/{esc(fill.get("requested_qty", 0))} '
            f'&nbsp; limit {esc(fill.get("limit_price"))} &nbsp; '
            f'fill {esc(fill.get("fill_price"))} &nbsp; slippage {slip_s}</div>')

    if r.get("no_trade_reason"):
        body.append(f'<div><h2>No trade</h2><p class="reason">'
                    f'{esc(r["no_trade_reason"])}</p></div>')

    if r.get("narrative"):
        body.append(f'<p class="narr">{esc(r["narrative"])}</p>')

    parts.append(f'<div class="body">{"".join(body)}</div>')
    return f'<section class="cycle">{"".join(parts)}</section>'


NICE = {
    "cycles": "Cycles", "trades": "Trades",
    "capital_utilisation_pct": "Capital used", "no_trade_cycles": "No-trade cycles",
    "mean_candidate_wait_cycles": "Mean wait", "herfindahl_concentration": "Concentration",
    "premium_collected_net": "Premium net", "premium_per_capital_day": "Premium/capital-day",
    "mean_slippage_per_contract": "Mean slippage", "worst_slippage_per_contract": "Worst slippage",
    "total_slippage_cost": "Slippage cost", "slippage_pct_of_gross_premium": "Slippage % gross",
    "mean_order_attempts": "Order attempts",
}


def build(run: Path) -> str:
    records, metrics = load(run)
    traded = sum(1 for r in records if (r.get("fill") or {}).get("filled_qty"))

    cards = "".join(
        f'<div class="metric"><div class="k">{esc(NICE.get(k, k))}</div>'
        f'<div class="v">{esc(fmt(v))}</div></div>'
        for k, v in metrics.items() if k != "policy")

    models = sorted({(r.get("first_pass") or {}).get("model", "")
                     for r in records} - {"", None})
    critics = sorted({(r.get("critique") or {}).get("model", "")
                      for r in records} - {"", None})

    meta = [f"run <b>{esc(run.name)}</b>", f"cycles <b>{len(records)}</b>",
            f"filled <b>{traded}</b>"]
    if models:
        meta.append(f"model <b>{esc(models[0])}</b>")
    if critics and critics != models:
        meta.append(f"critic <b>{esc(critics[0])}</b>")

    return f"""<title>Attention Weighted - run {esc(run.name)}</title>
<style>{CSS}</style>
<div class="wrap">
  <header class="head">
    <h1>Attention Weighted &mdash; session report</h1>
    <p class="sub">Cash-secured short puts allocated by an index policy.
       Every cycle is listed, including the ones that declined to trade.</p>
    <div class="meta">{" &nbsp;&middot;&nbsp; ".join(meta)}</div>
  </header>

  <section><h2>Metrics</h2><div class="metrics">{cards}</div></section>

  <section style="display:flex;flex-direction:column;gap:1rem">
    <h2>Cycles</h2>
    {"".join(render_cycle(r) for r in records)}
  </section>

  <footer class="foot">
    Paper trading only. Not investment advice. Paper-trading results are
    hypothetical and do not represent actual trading.
  </footer>
</div>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", help="run directory (default: newest)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    run = Path(args.run) if args.run else newest_run()
    if run is None or not (run / "cycles.jsonl").exists():
        print(f"no run found (looked in {config.RUNS_DIR}/*/cycles.jsonl)")
        return 1

    out = Path(args.out) if args.out else run / "report.html"
    out.write_text(build(run), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
