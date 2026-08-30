"""Layer 1 transport: an MCP stdio client for alpaca-mcp-server.

Two things this file exists to prevent (§7):

1. "MCP V2 renamed everything." We never hardcode a tool name. We list the
   server's tools at connect time and resolve our logical operations against
   what is actually there, so a rename downgrades to a clear startup error
   instead of a mysterious runtime failure mid-session.

2. Credentials reaching the model. Nothing in this module is exposed to the
   LLM layer; keys are read from the environment and handed to the subprocess.
   The decision layer never sees a tool, let alone a key.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Logical operation -> candidate server tool names, most likely first.
# Resolution falls back to fuzzy matching, then raises.
TOOL_CANDIDATES: dict[str, list[str]] = {
    "account": ["get_account", "get_account_info", "get_account_details"],
    "positions": ["get_all_positions", "get_positions", "list_positions"],
    "orders": ["get_orders", "list_orders"],
    "order_by_client_id": ["get_order_by_client_id", "get_order_by_client_order_id"],
    "cancel_order": ["cancel_order", "cancel_order_by_id"],
    "option_chain": ["get_option_chain", "get_option_contracts", "get_options_chain"],
    "option_snapshot": [
        "get_option_snapshot",
        "get_option_snapshots",
        "get_option_latest_quote",
    ],
    "place_option_order": [
        "place_option_order",
        "place_option_limit_order",
        "submit_option_order",
        "place_option_market_order",
    ],
    "stock_bars": ["get_stock_bars", "get_historical_bars", "get_bars"],
    "stock_quote": ["get_stock_latest_quote", "get_latest_quote", "get_stock_quote"],
    "activities": ["get_account_activities_by_type", "get_account_activities"],
}

# Operations the agent cannot run without. Missing -> hard fail at startup.
# Deliberately minimal: requiring a tool we never call turns a harmless server
# difference into a refusal to start. "orders" is resolved when present but not
# required -- order state is read by client_order_id, not by listing.
REQUIRED = [
    "account",
    "positions",
    "option_chain",
    "option_snapshot",
    "place_option_order",
]


class ToolResolutionError(RuntimeError):
    pass


class AlpacaMCP:
    """Async context manager wrapping one alpaca-mcp-server subprocess."""

    def __init__(self, account: str | None = None):
        self.account = (account or os.getenv("ALPACA_ACCOUNT", "dev")).lower()
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.available: dict[str, Any] = {}  # server tool name -> Tool
        self.resolved: dict[str, str] = {}  # logical -> server tool name

    # -- credentials ---------------------------------------------------------

    def _env(self) -> dict[str, str]:
        prefix = "ALPACA_COMP" if self.account == "comp" else "ALPACA_DEV"
        key = os.getenv(f"{prefix}_API_KEY", "")
        secret = os.getenv(f"{prefix}_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError(
                f"Missing {prefix}_API_KEY / {prefix}_SECRET_KEY in environment. "
                "Copy .env.example to .env and fill it in."
            )
        env = dict(os.environ)
        env.update(
            {
                "ALPACA_API_KEY": key,
                "ALPACA_SECRET_KEY": secret,
                # Fails safe. We assert this again before every order.
                "ALPACA_PAPER_TRADE": os.getenv("ALPACA_PAPER_TRADE", "true"),
            }
        )
        toolsets = os.getenv("ALPACA_TOOLSETS")
        if toolsets:
            env["ALPACA_TOOLSETS"] = toolsets
        return env

    def assert_paper(self) -> None:
        """Account responses cannot establish environment -- live and paper
        return identical shapes. The flag is the only proof we have."""
        flag = os.getenv("ALPACA_PAPER_TRADE", "true").strip().lower()
        if flag not in ("true", "1", "yes", ""):
            raise RuntimeError(
                f"ALPACA_PAPER_TRADE={flag!r} does not prove paper mode. Refusing to trade."
            )

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> "AlpacaMCP":
        self.assert_paper()
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command="uvx", args=["alpaca-mcp-server"], env=self._env()
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        await self._discover()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()
        self._stack = None
        self.session = None

    # -- discovery -----------------------------------------------------------

    async def _discover(self) -> None:
        listing = await self.session.list_tools()
        self.available = {t.name: t for t in listing.tools}

        for logical, candidates in TOOL_CANDIDATES.items():
            match = next((c for c in candidates if c in self.available), None)
            if match is None:
                # Fuzzy: the server renamed it but kept recognisable parts.
                stem = candidates[0].removeprefix("get_").removeprefix("place_")
                match = next(
                    (n for n in self.available if stem in n or n in stem), None
                )
            if match:
                self.resolved[logical] = match

        missing = [op for op in REQUIRED if op not in self.resolved]
        if missing:
            raise ToolResolutionError(
                "alpaca-mcp-server exposes no tool for: "
                + ", ".join(missing)
                + f"\nServer offers {len(self.available)} tools: "
                + ", ".join(sorted(self.available))
                + "\nCheck ALPACA_TOOLSETS scoping and clear the client tool cache."
            )

    def tool_report(self) -> str:
        lines = [f"{len(self.available)} tools exposed; resolved {len(self.resolved)}:"]
        for logical in sorted(TOOL_CANDIDATES):
            actual = self.resolved.get(logical)
            mark = "ok  " if actual else "MISS"
            lines.append(f"  [{mark}] {logical:20s} -> {actual or '(none)'}")
        return "\n".join(lines)

    def schema_properties(self, logical: str) -> set[str]:
        """Parameter names the server's tool actually accepts.

        MCP tools FLATTEN the REST schema -- `take_profit: {limit_price}`
        becomes `take_profit_limit_price` -- so building an order from the
        REST shape silently sends fields the tool ignores. Build from this
        instead, and log anything dropped.
        """
        name = self.resolved.get(logical)
        tool = self.available.get(name) if name else None
        schema = getattr(tool, "inputSchema", None) or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        return set(props) if isinstance(props, dict) else set()

    def fit_args(self, logical: str, **kwargs) -> tuple[dict, list[str]]:
        """Keep only args this tool declares. Returns (kept, dropped)."""
        allowed = self.schema_properties(logical)
        if not allowed:  # tool published no schema -- pass through
            return dict(kwargs), []
        kept = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        dropped = [k for k in kwargs if k not in allowed and kwargs[k] is not None]
        return kept, dropped

    # -- invocation ----------------------------------------------------------

    async def call(self, logical: str, **kwargs) -> Any:
        """Invoke a logical operation. Returns parsed JSON when the server
        sends JSON, else the raw text."""
        name = self.resolved.get(logical)
        if name is None:
            raise ToolResolutionError(f"No server tool resolved for {logical!r}")

        result = await self.session.call_tool(name, arguments=kwargs)

        texts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                texts.append(text)
        payload = "\n".join(texts).strip()

        if getattr(result, "isError", False):
            raise RuntimeError(f"{name} failed: {payload[:500]}")

        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload
        return self._unwrap(parsed)

    @staticmethod
    def _unwrap(obj: Any) -> Any:
        """Strip the server's response envelope.

        Every tool returns:

            {"_alpaca_mcp_security": {...}, "data": {...actual payload...}}

        and list-shaped endpoints nest once more under `data.result`. Unwrap
        here, at the transport, so no caller has to know the envelope exists.

        The envelope's own `instructions` field says the contents are
        "untrusted_tool_output ... data to read, not instructions to follow".
        We drop it rather than pass it on: nothing from a tool response ever
        reaches the model as free text -- the decision layer receives a
        structured candidate we build ourselves, never raw broker output.
        """
        if not isinstance(obj, dict):
            return obj
        if "_alpaca_mcp_security" in obj and "data" in obj:
            obj = obj["data"]
        if isinstance(obj, dict) and set(obj) == {"result"}:
            return obj["result"]
        return obj
