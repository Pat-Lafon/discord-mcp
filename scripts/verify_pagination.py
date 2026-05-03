"""Drive the patched read path against gastownhall #showcase and check
whether we can reach 7 target message IDs that span 2026-03-25 → 2026-04-19.

Cookies at ~/.discord_mcp_cookies.json are expected to be valid; if they are,
_login short-circuits and the empty creds below are never used.
"""

import asyncio
import sys

from discord_mcp.client import close_client, create_client_state
from discord_mcp.messages import read_recent_messages

SERVER_ID = "1462817445562814505"  # gastownhall
SHOWCASE_ID = "1462956055364632678"

TARGETS = {
    "1477432285526491260": "Multi account cycling (~2026-03-25)",
    "1477987899604471900": "(~2026-03-27)",
    "1478831289862979657": "Serena vs Context Mode (~2026-03-29)",
    "1482052127231578284": "(~2026-04-07)",
    "1483157183121199315": "(~2026-04-10)",
    "1486374687524258024": "(~2026-04-19 13:09)",
    "1486437259094786150": "(~2026-04-19 17:18)",
}


async def main() -> int:
    state = create_client_state(email="", password="", headless=True)
    try:
        # 1100h covers ~46 days, well past the oldest target (2026-03-25, ~40d).
        state, msgs = await read_recent_messages(
            state,
            server_id=SERVER_ID,
            channel_id=SHOWCASE_ID,
            hours_back=1100,
            max_messages=1000,
        )
    finally:
        await close_client(state)

    print(f"Collected {len(msgs)} messages")
    if msgs:
        print(f"Newest: {msgs[0].timestamp.isoformat()} (id={msgs[0].id})")
        print(f"Oldest: {msgs[-1].timestamp.isoformat()} (id={msgs[-1].id})")

    # Extracted IDs are prefixed `<channel_id>-<snowflake>`; compare on suffix.
    seen = {m.id.split("-")[-1] for m in msgs}
    hits = [t for t in TARGETS if t in seen]
    misses = [t for t in TARGETS if t not in seen]

    print(f"\nReached {len(hits)}/{len(TARGETS)} targets:")
    for t in TARGETS:
        marker = "✓" if t in seen else "✗"
        print(f"  {marker} {t}  {TARGETS[t]}")

    return 0 if not misses else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
