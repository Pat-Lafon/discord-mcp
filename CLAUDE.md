# Discord MCP Server (Python)

## Architecture

Tool definitions and module layout live in `src/discord_mcp/server.py`. The `discord-mcp` console script resolves to `discord_mcp:main`, which imports `server` inside the function body: `server` builds its FastMCP at module scope, and that constructor installs a bare `%(message)s` handler on the root logger, so a client-only import (`../bin/discord_messages.py` takes `.client` and `.messages`) must not reach it.

`logger.py` configures the `discord_mcp` logger at import — DEBUG, stderr, `propagate = False` — and every consumer wants that: the MCP server logs to Claude Code's server log, the daily review captures the stream as its trace file (the only record of per-feed counts and paging timings), and an ad-hoc client drive is how this repo is tested. stdout is reserved: it is the MCP stdio channel here and the transcript channel in `../bin/discord_messages.py`.

`get_channel_messages` pages by jumping the feed's scroller to 0 each pass — that is the one gesture that triggers Discord's older-history load (~20 rows per chunk); a keyboard PageUp moves one viewport within already-rendered rows, several presses short of the top, and loads nothing. The loop is bounded by progress, not a pass count, and returns which of four exits it took as a `StopReason`: `WINDOW_EDGE` (a row older than `since` rendered), `FEED_EXHAUSTED` (nothing scrolls, so the whole feed fits one viewport), `LIMIT`, or `STALLED` (three passes surfacing no new row). `covers_window` is the first two, and is what a watermark caller checks before advancing past the window. `STALLED` is the one exit that stays ambiguous — a feed taller than a viewport but younger than the window looks exactly like paging that broke.

The scroll gesture reports what it found rather than returning nothing: no message list at all raises, since `_open_channel` already waited for one, and it is the reading of a feed that vanished mid-page. Nothing scrollable under a list that exists is `FEED_EXHAUSTED`, not a stall.

A message's post time comes from its id, not from the row: Discord ids are snowflakes carrying milliseconds since 2015-01-01 in their high 42 bits (verified 2026-08-11 against 40 rendered rows — every one matched its `<time datetime>` exactly). A row cannot render without its id, so there is no timestamp-less row to date to now and no fallback to guess. `_posted_at` raises on a decode that is no post time — an id with no timestamp bits set (not a snowflake; it decodes to the epoch instant, reads as older than any window, and would end paging on the spot) or a result outside Discord's lifetime (the shift or epoch constant is wrong). Both otherwise produce a plausible-looking date, and this date is the window boundary.

`get_channel_threads` reads the sidebar's thread group for the channel it just navigated to and returns each thread as a `DiscordChannel`: a thread's id addresses it exactly like a channel, so `get_channel_messages` reads one unchanged. No MCP tool exposes it. A thread's messages never appear in the parent channel's feed, which shows only a "started a thread" marker, so `../bin/discord_messages.py` calls it per configured channel and reads each thread as its own feed — `#the-furious-five` carries its traffic in per-arc threads and reads as empty otherwise. The sidebar lists active (non-archived) threads only; the docstring carries that boundary.

Paging accumulates *sightings* per message id, not finished messages: each pass re-extracts every rendered row, and a row scrolled out of Discord's virtualized list never comes back. Exactly one field grows across passes — the author. Discord writes a name only on the first row of a run, so a row whose group start hadn't loaded yet has none to carry down, and a later pass supplies it; that field alone merges (first non-empty wins). `content` and `attachments` belong to the row itself and are overwritten by whichever pass last rendered it, so an edit, or a component that mounted late, isn't masked by the first thing the row was ever seen to say.

**A row read only in part still reports**, standing in for what wasn't read: `(unknown author)` or `(row rendered but no text, attachment, poll or link was read)`, neither of which a real row produces. Withholding it made the gap invisible — the only reader positioned to notice is the human reading the report, and a warning on the trace stream isn't in front of them. `_Obs.degradation()` names which of the two it is for the trace, and drives nothing else.

The thread-opening banner is the one row that is no message — Discord gives it the thread's own id, and it is excluded before the window check because that id dates it to thread creation.

The per-row `try` in the extractor is there to tolerate *one* malformed row, so a pass where more than that (or more than a tenth) come back null raises: a half-broken extractor returns a short read, and short is the shape of an ordinary quiet day here.

## Reliability
- One browser reused across MCP tool calls (`_execute_with_persistent_client`), rebuilt only when its page is closed or an attempt fails; a Playwright or `TransientLoginError` failure is retried once, and an async lock serializes tool calls against the shared state
- Cookie persistence at `~/.discord_mcp_cookies.json` for login state

## Login (cookie-only when headless)
- `_login` probes the session, never trusting a timeout: a slow load is `Indeterminate` (re-probed once with a longer wait, then raised as `TransientLoginError` so the caller retries), never folded into `LoggedOut`. Only a positive guild-nav render is `LoggedIn`.
- **The headless path never does credential login.** A fresh headless login trips Discord's device verification with no human to clear it, so on `LoggedOut` the headless job raises `CookieExpiredError` (a structural `DiscordLoginError`, not retryable) instead of the doomed attempt. Nothing catches it downstream: the traceback escapes `../bin/discord_messages.py` for exit 1, and the daily review's `_last_error` lifts the traceback's final line — the message above, remedy included — into the report's `Discord prefetch failed:` line. That line's wording is what a reader of the morning report gets (measured 2026-08-11 by moving the cookie file aside and running the scrape headless).
- **Reseed is a headed run.** `DISCORD_HEADLESS=false` makes `_login` fall through to `_perform_credential_login`, which drives a visible browser so a human can sign in (and satisfy verification); `_save_storage_state` then rewrites `~/.discord_mcp_cookies.json`. The next headless run rides the fresh cookie. This is the only path that reads `DISCORD_EMAIL`/`DISCORD_PASSWORD`.

## Development Workflow
1. Make changes following functional programming patterns
2. Run what `.github/workflows/pr-checks.yml` gates a PR on, in its order: `uv run pyright`, `uv run ruff check .`, `uv run ruff format --check .`. `uv run` takes ruff from this project's pinned dev group; `uvx ruff` resolves its own version and can disagree with CI
3. Verify the affected MCP tool(s) against live Discord — there is no test suite; drive the client path directly (e.g. `uv run --project . python -c ...` or via `../bin/discord_messages.py`)

## Configuration
Deployed path: `../discord-mcp-launch` reads the macOS Keychain entry with service `discord-mcp` (account = Discord email, password via `-w`), exports `DISCORD_EMAIL` / `DISCORD_PASSWORD` / `DISCORD_HEADLESS` (default `true`), then execs the server. Rotate with `security add-generic-password -s discord-mcp -a "$EMAIL" -w '<new>' -U`. Normal (headless) operation runs off the cookie and never reads the creds — they matter only for a headed reseed (see Login above), so a rotated password takes effect the next time you reseed.

Local/dev: `src/discord_mcp/config.py` reads only env vars (loading a local `.env` if present) and raises if `DISCORD_EMAIL` / `DISCORD_PASSWORD` are missing.
