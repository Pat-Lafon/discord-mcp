from datetime import datetime

from .client import ClientState, WindowRead, get_channel_messages
from .logger import logger


async def read_recent_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    since: datetime,
    max_messages: int = 1000,
) -> tuple[ClientState, WindowRead]:
    """Read the window's messages, newest first. `since` names the window's
    start, whether the caller got it from a watermark or from a lookback."""
    logger.debug(
        f"read_recent_messages called for server {server_id}, channel {channel_id}, "
        f"cutoff {since}, max {max_messages}"
    )

    # `since` goes down so paging stops at the window's edge; the filter below
    # still runs because the pass that reaches the edge overshoots it.
    state, window = await get_channel_messages(
        state,
        server_id=server_id,
        channel_id=channel_id,
        since=since,
        limit=max_messages,
    )
    logger.debug(f"Retrieved {len(window.messages)} total messages")

    recent = [m for m in window.messages if m.timestamp > since]
    logger.debug(f"Filtered to {len(recent)} messages after cutoff {since}")

    return state, WindowRead(messages=recent, stop=window.stop)
