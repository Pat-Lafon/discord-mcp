__version__ = "0.1.0"
__all__ = ["main"]


def main() -> None:
    """Console-script entry point, importing `server` only when a server runs.

    `server` builds its FastMCP at module scope, and that constructor calls the
    MCP SDK's `configure_logging()`, which installs a bare `%(message)s` handler
    on the root logger. Importing it here would hand that side effect to every
    client-only consumer: `bin/discord_messages.py` imports `.client` and never
    runs a server.
    """
    from .server import main as _main

    _main()
