"""
Alpaca MCP bridge -- stub.

Mirrors investment-monitor's mcp_bridge.py, with one deliberate difference:
ALPACA_TOOLSETS here includes "trading", not just market data. That is a
materially more sensitive permission grant (it can place orders) than the
monitor ever needed, so this file exists as its own module specifically to
keep that grant visible and separate from the read-only signal code in
src/signals/.

Not implemented yet -- Phase 0 is scaffolding only. Wire this up when the
execution agent (src/agents/execution.py) is actually built.
"""

from __future__ import annotations

ALPACA_TOOLSETS = ["trading", "market_data"]  # TODO: confirm exact toolset names against Alpaca MCP docs


def get_alpaca_mcp_client():
    raise NotImplementedError("mcp_bridge.get_alpaca_mcp_client: implement alongside execution agent")
