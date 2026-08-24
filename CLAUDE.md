# Discord MCP Server (Python)

## Architecture

Tool definitions and module layout live in `src/discord_mcp/server.py`. `__init__.py` and `logger.py`
carry their own reasons in place — why `main` imports `server` inside the function body, and why the
`discord_mcp` logger sets `propagate = False`.

The daily review captures that logger's stderr stream as its trace file, the only record of per-feed
counts and paging timings. stdout is reserved — the MCP stdio channel here, the transcript channel in
`../bin/discord_messages.py`.

`get_channel_messages` pages by jumping the feed's scroller to 0 each pass, the one gesture that
triggers Discord's older-history load (~20 rows per chunk); a keyboard PageUp moves one viewport within
already-rendered rows and loads nothing. The loop is bounded by progress rather than a pass count and
returns which of three exits it took as a `StopReason`: `WINDOW_EDGE` (a row older than `since`
rendered), `FEED_EXHAUSTED` (the feed's opening banner rendered), or `STALLED` (three passes surfacing
no new row). `covers_window` is the first two, and is what a watermark caller checks before advancing
past the window. `since` is the read's only bound and every row inside the window comes back — a row
cap would buy wall-clock by cutting the rows nearest `since`, exactly the ones a watermark caller never
revisits, while reporting the window covered. The pass that reaches the edge overshoots it, so
`get_channel_messages` drops what it rendered past `since` before returning.

Both clean exits are read off a rendered row, so `STALLED` means only that Discord served no more
history and no beginning came into view. **Don't decide exhaustion by geometry** — nothing scrollable
under the list — because a loaded feed taller than its pane scrolls exactly like a stalled one, so a
feed read end to end reports as a stall. Geometry stays as a fast path for a feed shorter than its
pane; the banner decides. Nothing scrollable under a list that exists is `FEED_EXHAUSTED`, not a stall,
while no message list at all raises — `_open_channel` already waited for one, so that is a feed that
vanished mid-page.

A message's post time comes from its id, not from the row: Discord ids are snowflakes carrying
milliseconds since 2015-01-01 in their high 42 bits. A row cannot render without its id, so there is no
timestamp-less row to date to now and no fallback to guess. `_posted_at` raises on a decode that is no
post time — an id with no timestamp bits set (it decodes to the epoch instant, reads as older than any
window, and would end paging on the spot) or a result outside Discord's lifetime (the shift or epoch
constant is wrong). Both otherwise produce a plausible-looking date, and that date is the window
boundary.

`get_guilds` and `get_guild_channels` both read through `_extract_rows`, which waits on the extractor's
own output rather than on a container's selector — Discord renders a list's chrome ahead of what goes
in it — and treats an empty result as breakage rather than an answer: you are in at least one guild,
and a guild you are in shows you at least one channel, so zero would read as true and be believed.
`get_channel_threads` is the one reader that stays out of it, waiting on the channel's own sidebar row,
because there a zero *is* the answer.

`get_channel_threads` reads the sidebar's thread group for the channel just navigated to and returns
each thread as a `DiscordChannel` — a thread's id addresses it exactly like a channel, so
`get_channel_messages` reads one unchanged. A thread's messages never appear in the parent channel's
feed, which shows only a "started a thread" marker, so `../bin/discord_messages.py` calls it per
configured channel and reads each thread as its own feed — a channel carrying its traffic in per-arc
threads reads as empty otherwise. The sidebar lists active threads only.

Row extraction — how a row's author, validity, and the opening banner are read off a DOM Discord is
free to change — is `client.py`'s module docstring. Read it before touching a selector.

## Reliability

One browser is reused across MCP tool calls (`_execute_with_persistent_client`), rebuilt only when its
page closes or an attempt fails; a Playwright or `TransientLoginError` failure is retried once, and an
async lock serializes tool calls against the shared state. Cookies persist at
`~/.discord_mcp_cookies.json`.

`_open_channel` bounds its `goto` at 60s and retries the whole open once, navigation included. The bare
30s default aborted daily runs mid-feed on a cold navigation, and the retry that existed covered only
`wait_for_selector` — the step that does not fail.

## Login (cookie-only, always)

`README.md` carries the operator-facing story — cookie-only, no credential path, `discord-mcp-reseed`
as the one sign-in step. The invariants an edit must not break:

- **A stall is never `False`.** `_probe_login_state` returns only on a positive signal — a rendered
  guild nav is `True`, an auth url is `False` — and raises `TransientLoginError` on a stall, which
  `_login` re-probes once with a longer wait before letting it reach the caller's retry. So a timeout
  cannot be read as signed out.
- **Nothing catches `CookieExpiredError`.** It is structural, not retryable; the traceback escapes
  `../bin/discord_messages.py` for exit 1, and the daily review's `_last_error` lifts its final line,
  remedy included, into the report's `Discord prefetch failed:` line. That wording is what a reader of
  the morning report gets.
- **`reseed_cookie` waits on stdin, not a selector**, which is what keeps Discord's login markup out of
  this repo: device verification, 2FA and CAPTCHA are the human's to clear, and the form they clear
  them on is free to change. It is also the cookie file's only writer, saving only once
  `_probe_login_state` confirms. Its `goto` carries a 120s bound — a cold headed window is several
  times slower to `/login` than a headless one.
- `headless` is a `ClientState` field, left at its `True` default outside `reseed_cookie`'s caller.
  To watch a selector fail against live Discord, pass `ClientState(headless=False)`.

## Development workflow

Run what `.github/workflows/pr-checks.yml` gates a PR on, in its order: `uv run pyright`,
`uv run ruff check .`, `uv run ruff format --check .`. `uv run` takes ruff from this project's pinned
dev group; `uvx ruff` resolves its own version and can disagree with CI. There is no test suite, so
verify an affected MCP tool by driving the client path directly against live Discord.

## Configuration

The cookie file is the whole configuration surface, and a missing one is `_login`'s error to give at
the point of use, with the reseed command in it. `../discord-mcp-launch` — the `.mcp.json` `command`,
registered by absolute path — resolves the submodule beside itself and execs the server, setting no
environment and reading no secret.
