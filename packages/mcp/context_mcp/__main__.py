"""Stdio entrypoint for Enterprise Context Lake FastMCP server (`python -m context_mcp`)."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from context_mcp.server import create_codemode_mcp_server, create_primitive_mcp_server


async def _run_stdio(mode: str, user_id: str, channel_mode: str, actor_mode: str) -> None:
    from context_api.bootstrap import create_runtime_context

    ctx = await create_runtime_context(
        db_path=os.environ.get("CONTEXT_LAKE_DB"),
        fixtures_dir=Path(os.environ.get("CONTEXT_LAKE_FIXTURES", "fixtures")),
        default_user_id=user_id,
        default_channel_mode=channel_mode,
        default_actor_mode=actor_mode,
    )
    try:
        server = (
            create_codemode_mcp_server(ctx.mcp_runtime)
            if mode == "codemode"
            else create_primitive_mcp_server(ctx.mcp_runtime)
        )
        await server.run_stdio_async()
    finally:
        await ctx.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Enterprise Context Lake FastMCP stdio server")
    parser.add_argument(
        "--mode",
        choices=("primitive", "codemode"),
        default=os.environ.get("CONTEXT_LAKE_MCP_MODE", "primitive"),
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("CONTEXT_LAKE_MOCK_USER", "user:public-analyst"),
    )
    parser.add_argument(
        "--channel-mode",
        choices=("direct", "shared"),
        default=os.environ.get("CONTEXT_LAKE_CHANNEL_MODE", "direct"),
    )
    parser.add_argument(
        "--actor-mode",
        choices=("obo", "service"),
        default=os.environ.get("CONTEXT_LAKE_ACTOR_MODE", "obo"),
    )
    args = parser.parse_args()
    asyncio.run(_run_stdio(args.mode, args.user, args.channel_mode, args.actor_mode))


if __name__ == "__main__":
    main()
