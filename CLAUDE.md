# Discord MCP Server (Python)

## Current Architecture
- **`main.py`** - Entry point that starts the MCP server
- **`src/discord_mcp/server.py`** - FastMCP server with 4 tool definitions
- **`src/discord_mcp/client.py`** - Playwright-based Discord client with simplified message extraction
- **`src/discord_mcp/config.py`** - Configuration management for Discord credentials
- **`src/discord_mcp/messages.py`** - Message reading and time filtering logic
- **`src/discord_mcp/logger.py`** - Logging setup and configuration
- **`tests/test_integration.py`** - Integration tests for all MCP tools

## MCP Tools Implemented
- **`get_servers`** - List all Discord servers you have access to
- **`get_channels(server_id)`** - List all channels in a specific Discord server
- **`read_messages(server_id, channel_id, max_messages, hours_back?)`** - Read recent messages in reverse-chronological order (newest first): `max_messages: 1` is the most recent message, larger values reach further back in time
- **`send_message(server_id, channel_id, content)`** - Send messages to specific Discord channels (automatically splits long messages)

## Reliability
- Complete browser reset between every MCP tool call using `_execute_with_fresh_client()`, with async-lock serialization to prevent races
- Cookie persistence at `~/.discord_mcp_cookies.json` for login state

## Development Workflow
1. Make changes following functional programming patterns
2. Verify types with `uv run pyright`
3. Run `uv run pytest -v tests/` for integration testing
4. Verify all 4 MCP tools work correctly

## Configuration
Deployed path: `../discord-mcp-launch` reads the macOS Keychain entry with service `discord-mcp` (account = Discord email, password via `-w`), exports `DISCORD_EMAIL` / `DISCORD_PASSWORD` / `DISCORD_HEADLESS` (default `true`), then execs the server. Rotate with `security add-generic-password -s discord-mcp -a "$EMAIL" -w '<new>' -U`.

Local/dev: `src/discord_mcp/config.py` reads only env vars (loading a local `.env` if present) and raises if `DISCORD_EMAIL` / `DISCORD_PASSWORD` are missing.
