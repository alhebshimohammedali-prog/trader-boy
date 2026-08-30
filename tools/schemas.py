"""Dump the real input schemas for every tool we call, plus one live sample.

Guessing parameter names is what cost us the first probe run: the server
resolved `stock_quote` fine, then rejected `symbol=` because the tool actually
declares something else. This prints what each tool accepts and what it
returns, so data.py can be written against the truth instead of an assumption.

    python tools/schemas.py
"""

from __future__ import annotations

import asyncio
import json

from dotenv import load_dotenv

import config
from src.mcp_client import AlpacaMCP

WANT = ["account", "positions", "orders", "order_by_client_id", "cancel_order",
        "option_chain", "option_snapshot", "place_option_order",
        "stock_bars", "stock_quote"]


def brief(obj, limit=900) -> str:
    try:
        s = json.dumps(obj, indent=2, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s[:limit] + ("..." if len(s) > limit else "")


async def main() -> int:
    load_dotenv()
    async with AlpacaMCP() as mcp:
        print("\n" + "=" * 70)
        print("TOOL SCHEMAS")
        print("=" * 70)
        for logical in WANT:
            actual = mcp.resolved.get(logical)
            if not actual:
                print(f"\n{logical}: UNRESOLVED")
                continue
            tool = mcp.available.get(actual)
            schema = getattr(tool, "inputSchema", None) or {}
            props = schema.get("properties") or {}
            req = schema.get("required") or []
            print(f"\n{logical}  ->  {actual}")
            if not props:
                print("    (no parameters declared)")
            for name, spec in props.items():
                t = spec.get("type") or spec.get("anyOf") or "?"
                star = " *REQUIRED" if name in req else ""
                desc = (spec.get("description") or "")[:70]
                print(f"    {name:24s} {str(t)[:28]:28s}{star}  {desc}")

        print("\n" + "=" * 70)
        print("LIVE SAMPLES")
        print("=" * 70)

        print("\n--- account ---")
        try:
            print(brief(await mcp.call("account")))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {str(e)[:400]}")

        print("\n--- positions ---")
        try:
            print(brief(await mcp.call("positions"), 400))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {str(e)[:400]}")

        # Try the chain with several plausible argument spellings.
        print(f"\n--- option_chain (AAPL, {config.TARGET_EXPIRY}) ---")
        for attempt in (
            {"underlying_symbol": "AAPL", "expiration_date": config.TARGET_EXPIRY,
             "type": "put"},
            {"underlying_symbol": "AAPL", "expiration_date": config.TARGET_EXPIRY},
            {"underlying_symbols": "AAPL", "expiration_date": config.TARGET_EXPIRY},
            {"symbol": "AAPL", "expiration_date": config.TARGET_EXPIRY},
        ):
            try:
                r = await mcp.call("option_chain", **attempt)
                ok = bool(r) and r != {}
                print(f"  args={list(attempt)} -> {'DATA' if ok else 'empty'}")
                if ok:
                    print(brief(r, 1100))
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  args={list(attempt)} -> ERROR {str(e)[:160]}")

        print("\n--- stock_quote (AAPL) ---")
        for attempt in ({"symbols": "AAPL"}, {"symbol": "AAPL"},
                        {"symbol_or_symbols": "AAPL"}):
            try:
                r = await mcp.call("stock_quote", **attempt)
                print(f"  args={list(attempt)} -> {brief(r, 300)}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  args={list(attempt)} -> ERROR {str(e)[:160]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
