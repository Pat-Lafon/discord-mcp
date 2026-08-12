# Discord MCP Server

A Model Context Protocol (MCP) server that lets LLMs read messages, discover channels, and monitor Discord communities using web scraping. Read-only: it exposes no tool that writes to Discord.

## Features

- List Discord servers and channels you have access to
- Read recent messages with time filtering (newest first)
- Web scraping approach - works with any Discord server you can access as a user
- No bot permissions or API tokens required

## Quick Start with Claude Code

Login restores the session from a persisted cookie (`~/.discord_mcp_cookies.json`).
Headless with that cookie missing or expired raises `CookieExpiredError` instead of
submitting the credentials: a fresh headless login trips Discord's device verification,
which needs a human at a visible browser. `DISCORD_EMAIL` and `DISCORD_PASSWORD` are
read on the headed path only, so seed the cookie headed once and run headless after.

```bash
# 1. Seed the cookie — headed, so the sign-in and any verification prompt are clickable
claude mcp add discord-mcp -s user -e DISCORD_EMAIL=your_email@example.com -e DISCORD_PASSWORD=your_password -e DISCORD_HEADLESS=false -- uvx --from git+https://github.com/elyxlz/discord-mcp.git discord-mcp
claude
> use get_servers to show me all my Discord servers
# A browser window opens. Sign in there; the cookie is written when the session confirms.

# 2. Switch to headless for normal use — the cookie is what it runs on
claude mcp remove discord-mcp -s user
claude mcp add discord-mcp -s user -e DISCORD_EMAIL=your_email@example.com -e DISCORD_PASSWORD=your_password -e DISCORD_HEADLESS=true -- uvx --from git+https://github.com/elyxlz/discord-mcp.git discord-mcp
```

Repeat step 1 whenever the cookie expires.

### Usage Examples

```bash
# List your Discord servers
> use get_servers to show me all my Discord servers

# Read recent messages (max_messages is required)
> read the last 20 messages from channel ID 123 in server ID 456

# Monitor communities
> summarize discussions from the last 24 hours across my Discord servers
```

## Available Tools

- **`get_servers`** - List all Discord servers you have access to
- **`get_channels(server_id)`** - List channels in a specific server
- **`read_messages(server_id, channel_id, max_messages, hours_back?)`** - Read recent messages (newest first, max_messages required)

## Manual Setup

### Prerequisites
- Python 3.12+ with `uv` package manager
- Discord account credentials

### Installation
```bash
git clone https://github.com/elyxlz/discord-mcp.git
cd discord-mcp
uv sync
uv run playwright install
```

### Configuration
Create `.env` file:
```env
DISCORD_EMAIL=your_email@example.com
DISCORD_PASSWORD=your_password
DISCORD_HEADLESS=true
```

Set `DISCORD_HEADLESS=false` for the first run — and any run after the cookie expires —
so the sign-in happens at a visible browser.

### Run Server
```bash
uv run discord-mcp
```

## Claude Desktop Integration

Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "discord": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/elyxlz/discord-mcp.git", "discord-mcp"],
      "env": {
        "DISCORD_EMAIL": "your_email@example.com",
        "DISCORD_PASSWORD": "your_password",
        "DISCORD_HEADLESS": "true"
      }
    }
  }
}
```

Claude Desktop has no headed browser of its own to sign in at, so seed the cookie from a
terminal first (Quick Start step 1, or `DISCORD_HEADLESS=false uv run discord-mcp` driven
by any client). Both read the same `~/.discord_mcp_cookies.json`.

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
- Run `DISCORD_HEADLESS=true` in production; reserve `false` for reseeding the cookie
- 2FA and device verification are cleared by the human at the headed reseed browser

## Troubleshooting

- **`CookieExpiredError`**: the cookie is missing or expired. Rerun with
  `DISCORD_HEADLESS=false` and sign in at the visible browser (Quick Start step 1);
  checking the credentials changes nothing, since the headless path never submits them
- **Browser errors**: Run `uv run playwright install --force`
- **Rate limits**: Reduce `max_messages`, monitor for Discord warnings
- **A tool call hangs or the session looks wrong**: delete
  `~/.discord_mcp_cookies.json` and reseed headed

## Legal Notice

Ensure compliance with Discord's Terms of Service. Only access information you would normally have access to as a user. Use for legitimate monitoring and research purposes.