# Discord MCP Server (Python)

## Architecture

Tool definitions and module layout live in `src/discord_mcp/server.py`. The `discord-mcp` console script resolves to `discord_mcp:main`, which imports `server` inside the function body: `server` builds its FastMCP at module scope, and that constructor installs a bare `%(message)s` handler on the root logger, so a client-only import (`../bin/discord_messages.py` takes `.client` and `.messages`) must not reach it.

`logger.py` configures the `discord_mcp` logger at import — DEBUG, stderr, `propagate = False` — and every consumer wants that: the MCP server logs to Claude Code's server log, the daily review captures the stream as its trace file (the only record of per-feed counts and paging timings), and an ad-hoc client drive is how this repo is tested. stdout is reserved: it is the MCP stdio channel here and the transcript channel in `../bin/discord_messages.py`.

`get_channel_messages` pages by jumping the feed's scroller to 0 each pass — that is the one gesture that triggers Discord's older-history load (~20 rows per chunk); a keyboard PageUp moves one viewport within already-rendered rows, several presses short of the top, and loads nothing. The loop is bounded by progress, not a pass count: it stops at the window edge (`since`, required), at `limit`, or after three consecutive passes surfacing no new row. Stopping anywhere but the window edge logs a warning — benign when the feed is younger than the window (its top is inside it).

A message's post time comes from its id, not from the row: Discord ids are snowflakes carrying milliseconds since 2015-01-01 in their high 42 bits (verified 2026-08-11 against 40 rendered rows — every one matched its `<time datetime>` exactly). A row cannot render without its id, so there is no timestamp-less row to date to now and no fallback to guess.

Paging accumulates *sightings* per message id, not finished messages: each pass re-extracts every rendered row, and what a row yields grows as history loads above it, so fields merge first-non-empty across passes. A row whose group start hadn't loaded yet gets its author from a later pass rather than relying on re-derivation. After each pass the accumulated set is re-read into messages; an id inside the window that still resolves to no author, or to no text and no attachment, is logged as a gap rather than dropped silently. The thread-opening banner is the one row that is no message — Discord gives it the thread's own id, and it is excluded before the window check because that id dates it to thread creation.

## Reliability
- One browser reused across MCP tool calls (`_execute_with_persistent_client`), rebuilt only when its page is closed or an attempt fails; a Playwright or `TransientLoginError` failure is retried once, and an async lock serializes tool calls against the shared state
- Cookie persistence at `~/.discord_mcp_cookies.json` for login state

## Login (cookie-only when headless)
- `_login` probes the session, never trusting a timeout: a slow load is `Indeterminate` (re-probed once with a longer wait, then raised as `TransientLoginError` so the caller retries), never folded into `LoggedOut`. Only a positive guild-nav render is `LoggedIn`.
- **The headless path never does credential login.** A fresh headless login trips Discord's device verification with no human to clear it, so on `LoggedOut` the headless job raises `CookieExpiredError` (a structural `DiscordLoginError`, not retryable) instead of the doomed attempt. Nothing catches it downstream: the traceback escapes `../bin/discord_messages.py` for exit 1, and the daily review's `_last_error` lifts the traceback's final line — the message above, remedy included — into the report's `Discord prefetch failed:` line. That line's wording is what a reader of the morning report gets (measured 2026-08-11 by moving the cookie file aside and running the scrape headless).
- **Reseed is a headed run.** `DISCORD_HEADLESS=false` makes `_login` fall through to `_perform_credential_login`, which drives a visible browser so a human can sign in (and satisfy verification); `_save_storage_state` then rewrites `~/.discord_mcp_cookies.json`. The next headless run rides the fresh cookie. This is the only path that reads `DISCORD_EMAIL`/`DISCORD_PASSWORD`.

## Development Workflow
1. Make changes following functional programming patterns
2. Verify types with `uv run pyright`
3. Verify the affected MCP tool(s) against live Discord — there is no test suite; drive the client path directly (e.g. `uv run --project . python -c ...` or via `../bin/discord_messages.py`)

## Configuration
Deployed path: `../discord-mcp-launch` reads the macOS Keychain entry with service `discord-mcp` (account = Discord email, password via `-w`), exports `DISCORD_EMAIL` / `DISCORD_PASSWORD` / `DISCORD_HEADLESS` (default `true`), then execs the server. Rotate with `security add-generic-password -s discord-mcp -a "$EMAIL" -w '<new>' -U`. Normal (headless) operation runs off the cookie and never reads the creds — they matter only for a headed reseed (see Login above), so a rotated password takes effect the next time you reseed.

Local/dev: `src/discord_mcp/config.py` reads only env vars (loading a local `.env` if present) and raises if `DISCORD_EMAIL` / `DISCORD_PASSWORD` are missing.
