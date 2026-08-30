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

CRITIQUE_PROMPT = """\
You are the second of two independent reviewers in an autonomous
options-trading agent. A first pass has already approved this trade. Your
job is to argue against it.

Your authority is one-way. You can veto, or shrink the size. You can never
approve something the first pass rejected, and you can never increase size.
If you find nothing wrong, return the first pass verdict unchanged.

Do not simply agree because the first pass was confident. Do not invent a
defect either. Look for the specific things a first pass rationalises:

- A quote that is internally inconsistent with the strike or tenor
- A spread that has widened past what the gate measured
- Concentration building in one ticker or sector across the portfolio
- Deployment close enough to the cap that this fill leaves no headroom
- An entry late in the window where remaining premium no longer pays for
  the gamma being taken on
- Reasoning in the first pass that cites something not present in the data

You are not a forecaster. Never veto because you think the underlying will
fall. Direction is not knowable here and is not your job.

An agent that vetoes everything scores zero. If the trade is merely
unexciting rather than defective, let it stand.

Reply with ONLY a JSON object, no prose and no code fence:
{"action": "proceed"|"shrink"|"veto",
 "size_multiplier": <float 0.0-1.0>,
 "reasoning": "<one sentence: the specific defect, or why it stands>"}"""

NARRATE_PROMPT = """\
You write the one-line human summary at the end of an autonomous trading
agent's cycle log. You have no authority over anything; the cycle has
already happened.

Given the cycle record, write ONE OR TWO plain sentences describing what the
agent did and why. Write for someone reading the log later to understand the
run, not for a trader.

Rules:
- State what actually happened, including when nothing happened. Most cycles
  are no-trade cycles and those are the interesting ones to explain.
- Name the binding reason. "No candidate passed all gates" is not useful;
  "every candidate failed the spread test after the open" is.
- Never speculate about where prices will go.
- Never editorialise about whether the decision was good.
- No preamble, no markdown, no bullet points. Just the sentences."""

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


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


async def _featherless(system: str, payload: str, json_mode: bool,
                       max_tokens: int, temperature: float | None,
                       model_override: str | None) -> tuple[str, str | None, str]:
    """Returns (text, error, model)."""
    import httpx

    key = os.getenv("FEATHERLESS_API_KEY", "")
    model = model_override or os.getenv("FEATHERLESS_MODEL", "")
    base = os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1").rstrip("/")
    if not key or not model:
        return "", "FEATHERLESS_API_KEY or FEATHERLESS_MODEL not set", model

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature

    # Measured against the live endpoint, not assumed. Both reasoning models we
    # tested return HTTP 200 with an EMPTY `content` when response_format is
    # set: the whole answer lands in a separate `reasoning` field and content
    # never gets written. Since this layer fails closed, shipping json_object
    # on those models vetoes every trade of the week while the log shows
    # nothing but ordinary vetoes. Off by default; opt in per model only after
    # tools/bench.py shows it actually returns content.
    if json_mode and _flag("FEATHERLESS_JSON_MODE", False):
        body["response_format"] = {"type": "json_object"}

    # Qwen-style models accept this and skip the chain of thought entirely,
    # which is what we want in a seat that returns an enum. GLM ignores it and
    # keeps reasoning, so this is opt-in rather than assumed.
    if _flag("FEATHERLESS_DISABLE_THINKING", False):
        body["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_SECONDS) as http:
            r = await http.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return "", f"featherless call failed: {type(exc).__name__}: {exc}", model

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = (msg.get("content") or "").strip()
    if text:
        return text, None, model

    # Content empty. A reasoning model that talked itself out of answering
    # still usually emitted the verdict inside its scratchpad, so look there
    # before giving up -- parse_verdict pulls the first balanced object out.
    thinking = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    if thinking and parse_verdict(thinking) is not None:
        return thinking, None, model

    # Genuinely nothing usable. Name the actual failure: "empty content" is
    # diagnosable from a log, "unparseable JSON" sends you hunting the parser.
    return "", (f"empty content (finish_reason={choice.get('finish_reason')!r}, "
                f"reasoning={len(thinking)}ch) -- model returned no answer"), model


async def _anthropic(system: str, payload: str, json_mode: bool,
                     max_tokens: int, temperature: float | None,
                     model_override: str | None) -> tuple[str, str | None, str]:
    # Temperature is deliberately not forwarded here. This path drives the
    # model through `output_config.effort`, and pinning temperature alongside
    # effort-based sampling is rejected on current models. Determinism on this
    # backend comes from the json_schema output format instead, which
    # constrains decoding directly and is strictly stronger than temperature 0.
    del temperature
    model = model_override or os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "", "ANTHROPIC_API_KEY not set", model
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(timeout=config.LLM_TIMEOUT_SECONDS)
        output_config: dict[str, Any] = {"effort": config.LLM_EFFORT}
        if json_mode:
            output_config["format"] = {"type": "json_schema", "schema": SCHEMA}
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            output_config=output_config,
            # Stable prefix + 1h TTL: our cycle is 15 min, so the default
            # 5-minute cache would expire between every single call.
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": payload}],
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"anthropic call failed: {type(exc).__name__}: {exc}", model

    # Safety classifiers can decline with a 200 and empty content.
    if getattr(resp, "stop_reason", None) == "refusal":
        return "", "model refused the request", model

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, None, model


async def _ask(provider: str, system: str, payload: str, *, json_mode: bool = True,
               max_tokens: int | None = None, temperature: float | None = -1.0,
               model: str | None = None) -> tuple[str, str | None, str]:
    mt = max_tokens or config.LLM_MAX_TOKENS
    # -1.0 is the "caller did not specify" sentinel, so that an explicit
    # temperature=None (meaning "send no temperature at all") stays reachable.
    temp = config.LLM_TEMPERATURE if temperature == -1.0 else temperature
    if provider == "featherless":
        return await _featherless(system, payload, json_mode, mt, temp, model)
    if provider == "anthropic":
        return await _anthropic(system, payload, json_mode, mt, temp, model)
    return "", f"unknown provider {provider!r}", ""


# --- entry points ------------------------------------------------------------


def _provider(env_name: str = "LLM_PROVIDER") -> str:
    return (os.getenv(env_name) or "none").strip().lower()


def _model(provider: str, role: str = "") -> str | None:
    """Per-role model override, so three roles on ONE provider can still run
    three different models.

    This matters most for the critic. A model critiquing itself shares training
    data, tokenizer and failure modes, so it mostly agrees and the second call
    buys nothing. Pointing the critic at a different family is what makes the
    second pass an actual second opinion rather than an echo.

    Returns None to mean "use the provider's default model".
    """
    if not role:
        return None
    prefix = "ANTHROPIC" if provider == "anthropic" else "FEATHERLESS"
    return os.getenv(f"{prefix}_{role}_MODEL") or None


async def decide(candidate: dict) -> Decision:
    """The whole primary LLM surface. One candidate in, one verdict out."""
    if not config.LLM_ENABLED:
        return Decision("proceed", 1.0, "LLM layer disabled by config", "none")

    provider = _provider()
    if provider == "none":
        return Decision("proceed", 1.0, "LLM layer disabled by provider=none", "none")

    payload = json.dumps(candidate, indent=2, default=str)
    text, err, model = await _ask(provider, SYSTEM_PROMPT, payload)
    if err:
        return _closed(err, provider, model)

    obj = parse_verdict(text)
    if obj is None and config.LLM_RETRY_TEMPERATURE is not None:
        # We decode greedily, so this payload will break the same way on every
        # cycle. Without one nudged retry, a single format quirk quietly
        # blackballs a ticker for the rest of the week and the log shows
        # nothing but vetoes. Retrying an unreadable ANSWER is not the same as
        # retrying an answer we did not like -- an actual verdict, including a
        # veto, is returned untouched below.
        text2, err2, _m = await _ask(provider, SYSTEM_PROMPT, payload,
                                     temperature=config.LLM_RETRY_TEMPERATURE)
        if not err2:
            retried = parse_verdict(text2)
            if retried is not None:
                obj, text = retried, text2

    if obj is None:
        return _closed("response was not parseable JSON", provider, model, text)
    return coerce(obj, provider, model, text)


async def critique(candidate: dict, first: Decision) -> Decision | None:
    """Second pass: argue AGAINST the trade the first pass approved.

    Authority is strictly one-way. This can veto or shrink; it can never
    upgrade a verdict or widen a position. `combine()` takes the more
    conservative of the two, so a critic that malfunctions toward caution
    costs a trade, and one that malfunctions toward approval changes nothing.

    Unlike the primary call this does NOT fail closed. The primary verdict and
    eight deterministic gates have already passed; blocking every trade because
    an optional second opinion timed out would manufacture the zero-trade week
    we spend the rest of this system avoiding. A failure is logged and skipped.

    Set LLM_CRITIC_PROVIDER to a different provider than LLM_PROVIDER to get
    genuinely uncorrelated review. Same model arguing with itself mostly
    rationalises; a different model family actually disagrees.
    """
    if not config.SELF_CRITIQUE_ENABLED:
        return None
    provider = _provider("LLM_CRITIC_PROVIDER")
    if provider == "none":
        provider = _provider()
    if provider == "none":
        return None

    payload = json.dumps(
        {"candidate": candidate,
         "first_pass_verdict": {"action": first.action,
                                "size_multiplier": first.size_multiplier,
                                "reasoning": first.reasoning}},
        indent=2, default=str)
    text, err, model = await _ask(provider, CRITIQUE_PROMPT, payload,
                                  model=_model(provider, "CRITIC"))
    if err:
        return Decision("proceed", 1.0, f"critique unavailable: {err}",
                        provider, model, err)
    obj = parse_verdict(text)
    if obj is None:
        return Decision("proceed", 1.0, "critique unparseable; first pass stands",
                        provider, model, "unparseable", text)
    return coerce(obj, provider, model, text)


def combine(first: Decision, second: Decision | None) -> Decision:
    """Take the more conservative of the two verdicts. Never the looser."""
    if second is None or second.error:
        return first
    rank = {"veto": 0, "shrink": 1, "proceed": 2}
    keep = first if rank[first.action] <= rank[second.action] else second
    out = Decision(
        action=keep.action,
        size_multiplier=min(first.size_multiplier, second.size_multiplier),
        reasoning=first.reasoning,
        provider=first.provider,
        model=first.model,
        raw=first.raw,
    )
    if keep is second:
        out.reasoning = f"{first.reasoning} | CRITIC OVERRODE: {second.reasoning}"
    elif second.action != first.action or second.size_multiplier < first.size_multiplier:
        out.reasoning = f"{first.reasoning} | critic: {second.reasoning}"
    if out.action == "veto":
        out.size_multiplier = 0.0
    return out


async def narrate(summary: dict) -> str:
    """Two sentences on what the agent did this cycle, for the log.

    No authority whatsoever: the trade is already decided and executed before
    this runs. It exists because the per-cycle log is the artifact a judge
    actually reads, and a bare reason string like "no candidate passed all
    gates" does not convey that the system reasoned its way there. Runs on
    no-trade cycles too, which are most of them.
    """
    if not config.NARRATE_ENABLED:
        return ""
    provider = _provider("LLM_NARRATOR_PROVIDER")
    if provider == "none":
        provider = _provider()
    if provider == "none":
        return ""
    text, err, _m = await _ask(
        provider, NARRATE_PROMPT, json.dumps(summary, indent=2, default=str),
        json_mode=False, max_tokens=300,
        temperature=config.LLM_NARRATE_TEMPERATURE,
        model=_model(provider, "NARRATE"))
    if err:
        return ""
    return " ".join((text or "").split())[:400]
