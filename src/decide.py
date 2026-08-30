"""Layer 6: the LLM decision.

The model receives ONE candidate that has already cleared eight deterministic
gates and won a PWT contest. It holds no tools, so it cannot place an order,
cannot re-select, and cannot reach a gate. Its entire surface is a JSON verdict:
proceed, shrink, or veto.

Fail CLOSED. A timeout, an HTTP error, or an unparseable response means NO
TRADE -- never a default-proceed. The one exception is LLM_ENABLED=False, which
is an explicit operator choice to run the agent without a model at all; the
agent is a complete trading system without this layer.

Provider-agnostic on purpose: the boundary is a single function returning a
Decision, so swapping Featherless for Anthropic (or removing the layer) touches
nothing else in the agent.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import config

SYSTEM_PROMPT = """\
You are the final risk check in an autonomous options-trading agent.
You are not a forecaster and you have no predictive edge on price
direction. Do not form or act on a directional view.

WHAT ALREADY HAPPENED, before you see anything:
- The candidate passed eight deterministic risk gates.
- It was selected by a capital-allocation algorithm from the runnable set.
- Position size was computed from the per-position and portfolio caps.

You cannot re-select a different candidate, relax a gate, or increase size.
You may only: proceed, shrink, or veto.

STRATEGY (fixed, not yours to revise):
Cash-secured short puts, single leg, expiring Fri 4 Sep. 16-30 delta or
1-2.5% OTM. Collateral = strike x 100, capped at 25% of equity per
position and 50-60% across the portfolio. Positions are held to the
Thursday mark rather than closed, because unrealised P&L counts and
closing costs the spread.

SCORING CONTEXT:
Account equity is marked at EOD Thursday 3 September. Anything after that
is worthless. Premium capture over four sessions is small relative to
directional noise; the edge, if any, is that implied vol tends to
overstate realised vol across many trades, not on any single one.

YOUR DEFAULT IS PROCEED.
The gates exist to make bad trades unreachable. If the candidate reached
you, it is presumptively sound. An agent that vetoes everything scores
zero, which is worse than a losing trade.

VETO only for a concrete, stateable defect:
- The quoted premium is implausible for the strike and tenor (data error)
- Bid is zero, or the spread has widened past what the gate measured
- The position would be opened inside a macro-event blackout
- Something in the payload is internally inconsistent

SHRINK when the trade is sound but the portfolio context argues for less:
- Deployment is already near the portfolio cap
- Concentration in one ticker or one sector is building
- Entry is late in the window and premium no longer justifies the gamma

THREE FAILURE MODES DOCUMENTED IN LIVE LLM TRADING AGENTS. AVOID THEM:

1. Number hardening. Every figure above is a BOUND, not a target. A 25%
   per-position cap does not mean "aim for 25%". A 16-30 delta band does
   not mean "prefer 30". Being comfortably inside a limit is not a reason
   to shrink, and sitting close to one is not a reason to proceed.

2. Cadence trading. The cycle interval is not a signal. That fifteen
   minutes have elapsed, or that this is the Nth cycle of the session,
   tells you nothing about whether this trade is sound. The timestamp is
   there so you can check the macro-event blackout and the entry cutoff,
   and for nothing else. Judge the candidate, not the clock.

3. Rule fabrication. Do not invent, cite, or reason from criteria that
   are not written above. If your reason for vetoing is not one of the
   listed defects, it is not a reason to veto.

Reply with ONLY a JSON object, no prose and no code fence:
{"action": "proceed"|"shrink"|"veto",
 "size_multiplier": <float 0.0-1.0>,
 "reasoning": "<one or two sentences stating the actual reason>"}

The reasoning goes into an auditable log a judge will read. State the real
reason, not a restatement of the inputs."""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["proceed", "shrink", "veto"]},
        "size_multiplier": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "size_multiplier", "reasoning"],
    "additionalProperties": False,
}


@dataclass
class Decision:
    action: str
    size_multiplier: float
    reasoning: str
    provider: str = "none"
    model: str = ""
    error: str | None = None
    raw: str = ""

    @property
    def approved(self) -> bool:
        return self.action in ("proceed", "shrink") and self.size_multiplier > 0

    def row(self) -> dict:
        return {
            "action": self.action,
            "size_multiplier": self.size_multiplier,
            "reasoning": self.reasoning,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
        }


def _closed(reason: str, provider: str = "", model: str = "", raw: str = "") -> Decision:
    return Decision("veto", 0.0, f"fail-closed: {reason}", provider, model, reason, raw)


def parse_verdict(text: str) -> dict | None:
    """Open models fence their JSON, add preamble, or trail commentary.
    Pull the first balanced object out rather than trusting the whole body."""
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    start = None
    return None


def coerce(obj: dict, provider: str, model: str, raw: str) -> Decision:
    action = str(obj.get("action", "")).strip().lower()
    if action not in ("proceed", "shrink", "veto"):
        return _closed(f"unknown action {action!r}", provider, model, raw)
    try:
        mult = float(obj.get("size_multiplier", 1.0))
    except (TypeError, ValueError):
        return _closed("size_multiplier not numeric", provider, model, raw)

    # The model may only ever shrink. Clamp defensively -- an instruction is
    # not a control.
    mult = max(0.0, min(1.0, mult))
    if action == "veto":
        mult = 0.0
    elif action == "proceed":
        mult = 1.0
    elif mult <= 0:
        return _closed("shrink to zero is ambiguous; treat as veto", provider, model, raw)

    reasoning = str(obj.get("reasoning", "")).strip() or "(no reasoning given)"
    return Decision(action, mult, reasoning, provider, model, None, raw)


# --- backends ----------------------------------------------------------------


async def _featherless(payload: str) -> Decision:
    import httpx

    key = os.getenv("FEATHERLESS_API_KEY", "")
    model = os.getenv("FEATHERLESS_MODEL", "")
    base = os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1").rstrip("/")
    if not key or not model:
        return _closed("FEATHERLESS_API_KEY or FEATHERLESS_MODEL not set", "featherless")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        "max_tokens": config.LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_SECONDS) as http:
            r = await http.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return _closed(f"featherless call failed: {type(exc).__name__}: {exc}",
                       "featherless", model)

    obj = parse_verdict(text)
    if obj is None:
        return _closed("response was not parseable JSON", "featherless", model, text)
    return coerce(obj, "featherless", model, text)


async def _anthropic(payload: str) -> Decision:
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
    if not os.getenv("ANTHROPIC_API_KEY"):
        return _closed("ANTHROPIC_API_KEY not set", "anthropic", model)
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(timeout=config.LLM_TIMEOUT_SECONDS)
        resp = await client.messages.create(
            model=model,
            max_tokens=config.LLM_MAX_TOKENS,
            output_config={
                "effort": config.LLM_EFFORT,
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            # Stable prefix + 1h TTL: our cycle is 15 min, so the default
            # 5-minute cache would expire between every single call.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": payload}],
        )
    except Exception as exc:  # noqa: BLE001
        return _closed(f"anthropic call failed: {type(exc).__name__}: {exc}",
                       "anthropic", model)

    # Safety classifiers can decline with a 200 and empty content.
    if getattr(resp, "stop_reason", None) == "refusal":
        return _closed("model refused the request", "anthropic", model)

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    obj = parse_verdict(text)
    if obj is None:
        return _closed("response was not parseable JSON", "anthropic", model, text)
    return coerce(obj, "anthropic", model, text)


# --- entry point -------------------------------------------------------------


async def decide(candidate: dict) -> Decision:
    """The whole LLM surface. One candidate in, one verdict out, no tools."""
    if not config.LLM_ENABLED:
        return Decision("proceed", 1.0, "LLM layer disabled by config", "none")

    provider = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if provider == "none":
        return Decision("proceed", 1.0, "LLM layer disabled by provider=none", "none")

    payload = json.dumps(candidate, indent=2, default=str)

    if provider == "featherless":
        return await _featherless(payload)
    if provider == "anthropic":
        return await _anthropic(payload)
    return _closed(f"unknown LLM_PROVIDER {provider!r}", provider)
