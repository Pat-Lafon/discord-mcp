# Discord MCP Server

A Model Context Protocol (MCP) server that lets LLMs read messages, discover channels, and monitor Discord communities using web scraping. Read-only: it exposes no tool that writes to Discord.

## Features

- List Discord servers and channels you have access to
- Read recent messages with time filtering (newest first)
- Web scraping approach - works with any Discord server you can access as a user
- No bot permissions or API tokens required

## Quick Start with Claude Code

The server authenticates with a browser session at `~/.discord_mcp_cookies.json` and
nothing else — it stores no credentials and never submits a login form. Discord's device
verification, 2FA and CAPTCHA each expect a person, so signing in is a human step, run
once by `discord-mcp-reseed`. With the cookie missing or expired, every tool call raises
`CookieExpiredError` naming that command.

```bash
# 1. Seed the cookie — a browser window opens; sign in there and press Enter
uv run discord-mcp-reseed

# 2. Add the server; it runs on the cookie from step 1
claude mcp add discord-mcp -s user -- uvx --from git+https://github.com/elyxlz/discord-mcp.git discord-mcp
claude
> use get_servers to show me all my Discord servers
```

Repeat step 1 whenever the cookie expires. The session is written only after the sign-in
confirms, so a reseed you abandon leaves the existing cookie alone.

### Usage Examples

```bash
# List your Discord servers
> use get_servers to show me all my Discord servers

# Read recent messages
> read the last day of messages from channel ID 123 in server ID 456

# Monitor communities
> summarize discussions from the last 24 hours across my Discord servers
```

## Available Tools

- **`get_servers`** - List all Discord servers you have access to
- **`get_channels(server_id)`** - List channels in a specific server
- **`read_messages(server_id, channel_id, hours_back?)`** - Read every message posted in the last `hours_back` hours (newest first)

## Manual Setup

### Prerequisites
- Python 3.12+ with `uv` package manager
- A Discord account you can sign into at a browser

### Installation
```bash
git clone https://github.com/elyxlz/discord-mcp.git
cd discord-mcp
uv sync
uv run playwright install
```

### Configuration
None. The cookie at `~/.discord_mcp_cookies.json` is the only state the server reads,
and `discord-mcp-reseed` is what writes it.

### Run Server
```bash
uv run discord-mcp-reseed   # once, to mint the cookie
uv run discord-mcp
```

## Claude Desktop Integration

Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "discord": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/elyxlz/discord-mcp.git", "discord-mcp"]
    }
  }
}
```

Seed the cookie from a terminal first with `discord-mcp-reseed` (Quick Start step 1) —
Claude Desktop has no browser of its own to sign in at. Both read the same
`~/.discord_mcp_cookies.json`.

## Development

What `.github/workflows/pr-checks.yml` gates a PR on:

```bash
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

Run them through `uv run` rather than `uvx`, which resolves a ruff version of its own
and can disagree with CI.

## Security Notes

- Consider using a dedicated Discord account for automation
- No password is stored anywhere: the session cookie is the only secret at rest
- 2FA and device verification are cleared by the human at the `discord-mcp-reseed` browser

## Troubleshooting

- **`CookieExpiredError`**: the cookie is missing or expired. Run `discord-mcp-reseed`
  and sign in at the browser it opens
- **Browser errors**: Run `uv run playwright install --force`
- **Rate limits**: Reduce `hours_back`, monitor for Discord warnings
- **A tool call hangs or the session looks wrong**: delete
  `~/.discord_mcp_cookies.json` and run `discord-mcp-reseed`

## Legal Notice

Ensure compliance with Discord's Terms of Service. Only access information you would normally have access to as a user. Use for legitimate monitoring and research purposes.