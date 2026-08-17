__version__ = "0.1.0"
__all__ = ["main", "reseed_main"]


def main() -> None:
    """Console-script entry point, importing `server` only when a server runs.

    `server` builds its FastMCP at module scope, and that constructor calls the
    MCP SDK's `configure_logging()`, which installs a bare `%(message)s` handler
    on the root logger. Importing it here would hand that side effect to every
    client-only consumer: `bin/discord_messages.py` imports `.client` and
    `.messages` and never runs a server.
    """
    from .server import main as _main

    _main()


def reseed_main() -> None:
    """Console-script entry point for `discord-mcp-reseed`.

    The cookie is this package's only credential, so minting one lives here
    rather than in whichever caller noticed it had expired.
    """
    import asyncio

    from .client import ClientState, close_client, reseed_cookie

    async def run() -> None:
        state = ClientState(headless=False)
        try:
            state = await reseed_cookie(state)
        finally:
            await close_client(state)

    asyncio.run(run())
