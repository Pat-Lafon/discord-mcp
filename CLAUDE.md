# Discord MCP Server (Python)

## Current Architecture
- **`main.py`** - Entry point that starts the MCP server
- **`src/discord_mcp/server.py`** - FastMCP server with 4 tool definitions
- **`src/discord_mcp/client.py`** - Playwright-based Discord client with simplified message extraction
- **`src/discord_mcp/config.py`** - Configuration management for Discord credentials
- **`src/discord_mcp/messages.py`** - Message reading and time filtering logic
- **`src/discord_mcp/logger.py`** - Logging setup and configuration

## MCP Tools Implemented
- **`get_servers`** - List all Discord servers you have access to
- **`get_channels(server_id)`** - List all channels in a specific Discord server
- **`read_messages(server_id, channel_id, max_messages, hours_back?)`** - Read recent messages in reverse-chronological order (newest first): `max_messages: 1` is the most recent message, larger values reach further back in time
- **`send_message(server_id, channel_id, content)`** - Send messages to specific Discord channels (automatically splits long messages)

## Reliability
- Complete browser reset between every MCP tool call using `_execute_with_fresh_client()`, with async-lock serialization to prevent races
- Cookie persistence at `~/.discord_mcp_cookies.json` for login state

## Login (cookie-only when headless)
- `_login` probes the session, never trusting a timeout: a slow load is `Indeterminate` (re-probed once with a longer wait, then raised as `TransientLoginError` so the caller retries), never folded into `LoggedOut`. Only a positive guild-nav render is `LoggedIn`.
- **The headless path never does credential login.** A fresh headless login trips Discord's device verification with no human to clear it, so on `LoggedOut` the headless job raises `CookieExpiredError` (a structural `DiscordLoginError`, not retryable) instead of the doomed attempt. In the daily review this surfaces via `../bin/discord_messages.py` exit 4 → a report tombstone prompting a reseed.
- **Reseed is a headed run.** `DISCORD_HEADLESS=false` makes `_login` fall through to `_perform_credential_login`, which drives a visible browser so a human can sign in (and satisfy verification); `_save_storage_state` then rewrites `~/.discord_mcp_cookies.json`. The next headless run rides the fresh cookie. This is the only path that reads `DISCORD_EMAIL`/`DISCORD_PASSWORD`.

## Development Workflow
1. Make changes following functional programming patterns
2. Verify types with `uv run pyright`
3. Verify the affected MCP tool(s) against live Discord — there is no test suite; drive the client path directly (e.g. `uv run --project . python -c ...` or via `../bin/discord_messages.py`)

## Configuration
Deployed path: `../discord-mcp-launch` reads the macOS Keychain entry with service `discord-mcp` (account = Discord email, password via `-w`), exports `DISCORD_EMAIL` / `DISCORD_PASSWORD` / `DISCORD_HEADLESS` (default `true`), then execs the server. Rotate with `security add-generic-password -s discord-mcp -a "$EMAIL" -w '<new>' -U`. Normal (headless) operation runs off the cookie and never reads the creds — they matter only for a headed reseed (see Login above), so a rotated password takes effect the next time you reseed.

Local/dev: `src/discord_mcp/config.py` reads only env vars (loading a local `.env` if present) and raises if `DISCORD_EMAIL` / `DISCORD_PASSWORD` are missing.
