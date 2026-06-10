# Discord MCP Server (Python)

## Code Quality
- Always run `uv run pyright` for Python type checking

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
- **`read_messages(server_id, channel_id, max_messages, hours_back?)`** - Read recent messages in reverse-chronological order (newest first)
- **`send_message(server_id, channel_id, content)`** - Send messages to specific Discord channels (automatically splits long messages)

## Dependencies
- **mcp** - Official MCP library via FastMCP
- **playwright** - Browser automation for Discord web scraping
- **python-dotenv** - Environment variable management
- **pytest** - Testing framework

## Test Strategy & Reliability
The implementation prioritizes **reliability over speed** through:

### Browser State Management
- Complete browser reset between every MCP tool call using `_execute_with_fresh_client()`
- Async lock serialization to prevent race conditions
- Cookie persistence at `~/.discord_mcp_cookies.json` for login state

### Message Extraction
- **Chronological ordering**: Messages returned newest-first
- **Robust scrolling**: JavaScript-based scroll to bottom for newest messages
- **Proper filtering**: Time-based and count-based message limiting

### Test Execution
- Sequential test execution (`-n 0` in pytest.ini) to avoid resource conflicts
- Comprehensive integration tests covering all 4 MCP tools

## Development Workflow
1. Make changes following functional programming patterns
2. Run `uv run pyright` for type checking
3. Run `uv run pytest -v tests/` for integration testing
4. Verify all 4 MCP tools work correctly

## Configuration
Set environment variables:
```env
DISCORD_EMAIL=your_email@example.com
DISCORD_PASSWORD=your_password
DISCORD_HEADLESS=true  # For production
```

## Message Ordering Behavior
Messages are returned in **reverse-chronological order (newest first)**:
- `max_messages: 1` returns the most recent message
- `max_messages: 20` returns the 20 most recent messages
- More messages means going further back in time
