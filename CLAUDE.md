# Discord MCP Server (Python)

## Architecture

Tool definitions and module layout live in `src/discord_mcp/server.py`. `__init__.py` and `logger.py`
carry their own reasons in place — why `main` imports `server` inside the function body, and why the
`discord_mcp` logger sets `propagate = False`.

The daily review captures that logger's stderr stream as its trace file, the only record of per-feed
counts and paging timings. stdout is reserved — the MCP stdio channel here, the transcript channel in
`../bin/discord_messages.py`.

A thread's messages never appear in the parent channel's feed, which shows only a "started a thread"
marker, so `../bin/discord_messages.py` calls `get_channel_threads` per configured channel and reads
each thread as its own feed — a channel carrying its traffic in per-arc threads reads as empty
otherwise.

Paging and its three stop reasons, snowflake dating, why an empty extraction is a failure rather than
an answer, the voice-channel chat toggle, and row extraction — how a row's author, validity, and the
opening banner are read off a DOM Discord is free to change — are in `src/discord_mcp/client.py`'s
module and function docstrings. Read them before touching a selector.

## Reliability

One browser is reused across MCP tool calls (`_execute_with_persistent_client`), rebuilt only when its
page closes or an attempt fails; a Playwright or `TransientLoginError` failure is retried once, and an
async lock serializes tool calls against the shared state. Cookies persist at
`~/.discord_mcp_cookies.json`.

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
