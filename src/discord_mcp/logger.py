import logging
import sys


def setup_logger(
    name: str = "discord_mcp", level: int = logging.DEBUG
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # In the server process the root logger carries a bare `%(message)s` handler
    # that FastMCP's constructor installs; propagation would emit every line a
    # second time through it, stripped of the timestamp and call site.
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
    )

    # stderr, never stdout: stdout is the MCP stdio channel when running as a
    # server, and the transcript channel for bin/discord_messages.py — a log
    # line on stdout corrupts both.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()
