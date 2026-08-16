import asyncio
import typing as tp
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP
from playwright.async_api import Error as PlaywrightError
from .logger import logger
from .client import (
    ClientState,
    get_guilds,
    get_guild_channels,
    close_client,
    TransientLoginError,
)
from .config import load_config
from .messages import read_recent_messages


@dataclass
class DiscordContext:
    config: tp.Any
    client_lock: asyncio.Lock
    client_state: ClientState | None = None


@asynccontextmanager
async def discord_lifespan(server: FastMCP) -> AsyncIterator[DiscordContext]:
    config = load_config()
    client_lock = asyncio.Lock()
    logger.debug("Discord MCP server starting up")
    ctx = DiscordContext(config=config, client_lock=client_lock)
    try:
        yield ctx
    finally:
        logger.debug("Discord MCP server shutting down")
        if ctx.client_state is not None:
            await close_client(ctx.client_state)


async def _execute_with_persistent_client[T](
    discord_ctx: DiscordContext,
    operation: Callable[[ClientState], tp.Awaitable[tuple[ClientState, T]]],
) -> T:
    """Execute Discord operation with a persistent client, retrying once on Playwright errors."""
    cfg = discord_ctx.config
    async with discord_ctx.client_lock:
        state = discord_ctx.client_state
        if state is not None:
            if state.page is None or state.page.is_closed():
                await close_client(state)
                state = None
            else:
                # Force _login to re-probe login state; it early-returns on logged_in=True.
                state = replace(state, logged_in=False)

        retried = False
        while True:
            if state is None:
                state = ClientState(cfg.email, cfg.password, cfg.headless)
            try:
                state, result = await operation(state)
                discord_ctx.client_state = state
                return result
            except Exception as e:
                await close_client(state)
                state = discord_ctx.client_state = None
                if retried or not isinstance(e, (PlaywrightError, TransientLoginError)):
                    raise
                retried = True
                logger.warning("operation failed, retrying: %s", e)


mcp = FastMCP("discord-mcp", lifespan=discord_lifespan)


@mcp.tool()
async def get_servers() -> list[dict[str, str]]:
    """List all Discord servers (guilds) you have access to"""
    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    guilds = await _execute_with_persistent_client(discord_ctx, get_guilds)
    return [{"id": g.id, "name": g.name} for g in guilds]


@mcp.tool()
async def get_channels(server_id: str) -> list[dict[str, str]]:
    """List all channels in a specific Discord server"""
    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    async def operation(state):
        return await get_guild_channels(state, server_id)

    channels = await _execute_with_persistent_client(discord_ctx, operation)
    return [{"id": c.id, "name": c.name} for c in channels]


@mcp.tool()
async def read_messages(
    server_id: str, channel_id: str, hours_back: int = 24
) -> list[dict[str, tp.Any]]:
    """Read recent messages from a specific channel"""
    if not (1 <= hours_back <= 8760):
        raise ValueError("hours_back must be between 1 and 8760 (1 year)")

    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)
    since = datetime.now(UTC) - timedelta(hours=hours_back)

    async def operation(state):
        return await read_recent_messages(state, server_id, channel_id, since)

    window = await _execute_with_persistent_client(discord_ctx, operation)
    return [
        {
            "id": m.id,
            "content": m.content,
            "author_name": m.author_name,
            "timestamp": m.timestamp.isoformat(),
            "attachments": m.attachments,
        }
        for m in window.messages
    ]


def main():
    mcp.run()


if __name__ == "__main__":
    main()
