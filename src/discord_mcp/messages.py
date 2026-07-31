from datetime import datetime, timezone, timedelta
from .client import ClientState, WindowRead, get_channel_messages
from .logger import logger


async def read_recent_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    hours_back: int = 24,
    max_messages: int = 1000,
    since: datetime | None = None,
) -> tuple[ClientState, WindowRead]:
    """Read the window's messages, newest first. `since` names the window's
    start directly (a caller resuming from a watermark); without it the window
    is `hours_back` from now."""
    cutoff_time = since or datetime.now(timezone.utc) - timedelta(hours=hours_back)
    logger.debug(
        f"read_recent_messages called for server {server_id}, channel {channel_id}, "
        f"cutoff {cutoff_time}, max {max_messages}"
    )

    # Get messages in reverse-chronological order (newest first). The cutoff
    # goes down so paging stops at the window's edge; the filter below still
    # runs because the pass that reaches the edge overshoots it.
    state, window = await get_channel_messages(
        state,
        server_id=server_id,
        channel_id=channel_id,
        limit=max_messages,
        since=cutoff_time,
    )
    logger.debug(f"Retrieved {len(window.messages)} total messages")

    # Filter to only recent messages within the time window
    recent_messages = [m for m in window.messages if m.timestamp > cutoff_time]
    logger.debug(
        f"Filtered to {len(recent_messages)} messages after cutoff {cutoff_time}"
    )

    return state, WindowRead(
        messages=recent_messages, reached_since=window.reached_since
    )
