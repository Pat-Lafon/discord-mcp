# Discord MCP Server (Python)

## Architecture

Tool definitions and module layout live in `src/discord_mcp/server.py`. `main` imports `server` inside
the function body: `server` builds its FastMCP at module scope, and that constructor installs a bare
`%(message)s` handler on the root logger, so a client-only import (`../bin/discord_messages.py` takes
`.client` and `.messages`) must not reach it.

`logger.py` configures the `discord_mcp` logger at import — DEBUG, stderr, `propagate = False` — and
every consumer wants that, not least the daily review, which captures the stream as its trace file:
the only record of per-feed counts and paging timings. stdout is reserved — the MCP stdio channel here,
the transcript channel in `../bin/discord_messages.py`.

`get_channel_messages` pages by jumping the feed's scroller to 0 each pass, the one gesture that
triggers Discord's older-history load (~20 rows per chunk); a keyboard PageUp moves one viewport within
already-rendered rows and loads nothing. The loop is bounded by progress rather than a pass count and
returns which of three exits it took as a `StopReason`: `WINDOW_EDGE` (a row older than `since`
rendered), `FEED_EXHAUSTED` (the feed's opening banner rendered), or `STALLED` (three passes surfacing
no new row). `covers_window` is the first two, and is what a watermark caller checks before advancing
past the window. `since` is the read's only bound and every rendered row comes back — a row cap would
buy wall-clock by cutting the rows nearest `since`, exactly the ones a watermark caller never revisits,
while reporting the window covered.

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

`get_channel_threads` reads the sidebar's thread group for the channel just navigated to and returns
each thread as a `DiscordChannel` — a thread's id addresses it exactly like a channel, so
`get_channel_messages` reads one unchanged. A thread's messages never appear in the parent channel's
feed, which shows only a "started a thread" marker, so `../bin/discord_messages.py` calls it per
configured channel and reads each thread as its own feed: `#the-furious-five` carries its traffic in
per-arc threads and reads as empty otherwise. The sidebar lists active threads only, and no MCP tool
exposes this.

Paging keys extractions by message id and overwrites: each pass re-extracts every rendered row, every
field belongs to the row itself, and whichever pass last rendered it read the whole story. An edit, or
a component that mounted late, is therefore not masked by the first thing the row was ever seen to say.
No field merges across passes.

A row's author is read from the row, not inferred from the name above it. Discord writes a username
node only on the first row of a run, but every continuation labels itself with that node's id — the
`message-username-*` entry in its `aria-labelledby`. That holds across virtualization, which evicts
whole groups and never renders a continuation without its head. A reply row labels its article with the
message it quotes, so the extractor matches the `message-username-` prefix rather than taking the first
idref.

**A row names someone and says something, or it is not a row this extractor understands.** A row that
reads as authorless, or as having no text, attachment, poll or link, is a selector that stopped
matching, so it returns null and is counted rather than reported under a placeholder. The extractor
tolerates *one* null — a pass with more than that, or more than a tenth, raises, because a half-broken
extractor returns a short read and short is the shape of an ordinary quiet day here.

The opening banner is the one row that is no message — a thread's starter, or a channel's "Welcome to
#name!" — and Discord gives it the feed's own id, so the extractor drops the row whose trailing id
segment equals the channel id it was passed. That id dates the banner to the feed's creation, which
would end paging on the spot. It is also what `FEED_EXHAUSTED` is read from, by id rather than class,
since class names are content-hashed and churn.

## Reliability

One browser is reused across MCP tool calls (`_execute_with_persistent_client`), rebuilt only when its
page closes or an attempt fails; a Playwright or `TransientLoginError` failure is retried once, and an
async lock serializes tool calls against the shared state. Cookies persist at
`~/.discord_mcp_cookies.json`.

`_open_channel` bounds its `goto` at 60s and retries the whole open once, navigation included. The bare
30s default aborted daily runs mid-feed on a cold navigation, and the retry that existed covered only
`wait_for_selector` — the step that does not fail.

## Login (cookie-only, always)

- `_login` probes the session and never trusts a timeout: a slow load is `Indeterminate` (re-probed
  once with a longer wait, then raised as `TransientLoginError` so the caller retries), never folded
  into `LoggedOut`. Only a positive guild-nav render is `LoggedIn`.
- **Nothing here logs in.** `_login` has no credential path: `LoggedOut` raises `CookieExpiredError`
  (structural, not retryable) naming `discord-mcp-reseed`. Nothing catches it downstream — the
  traceback escapes `../bin/discord_messages.py` for exit 1, and the daily review's `_last_error` lifts
  its final line, remedy included, into the report's `Discord prefetch failed:` line. That wording is
  what a reader of the morning report gets.
- **`reseed_cookie` is the only writer of the cookie file**, reached through the `discord-mcp-reseed`
  console script. It opens a visible browser at `/login`, blocks on stdin, and saves only once
  `_probe_login_state` confirms `LoggedIn` — so an Enter pressed early raises and leaves a working
  cookie alone. Waiting on stdin rather than a selector is what keeps Discord's login markup out of
  this repo: device verification, 2FA and CAPTCHA are all the human's to clear, and the form they clear
  them on is free to change. Its `goto` carries a 120s bound, since a cold headed window is several
  times slower to `/login` than a headless one.
- `headless` is a `ClientState` field, set by `reseed_cookie`'s caller and left at its `True` default
  everywhere else. To watch a selector fail against live Discord, pass `ClientState(headless=False)`.

## Development workflow

Run what `.github/workflows/pr-checks.yml` gates a PR on, in its order: `uv run pyright`,
`uv run ruff check .`, `uv run ruff format --check .`. `uv run` takes ruff from this project's pinned
dev group; `uvx ruff` resolves its own version and can disagree with CI. There is no test suite, so
verify an affected MCP tool by driving the client path directly against live Discord.

## Configuration

There is none. `../discord-mcp-launch` — the `.mcp.json` `command`, registered by absolute path —
resolves the submodule beside itself and execs the server, setting no environment and reading no
secret. The cookie file is the whole configuration surface, and a missing one is `_login`'s error to
give at the point of use, with the reseed command in it.
