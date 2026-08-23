import asyncio
import pathlib as pl
from datetime import datetime, timezone
import dataclasses as dc
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from .logger import logger


@dc.dataclass(frozen=True)
class DiscordMessage:
    id: str
    content: str
    author_name: str
    author_id: str
    channel_id: str
    timestamp: datetime
    attachments: list[str]


@dc.dataclass(frozen=True)
class DiscordChannel:
    id: str
    name: str


@dc.dataclass(frozen=True)
class DiscordGuild:
    id: str
    name: str


@dc.dataclass(frozen=True)
class ClientState:
    headless: bool = True
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    logged_in: bool = False
    cookies_file: pl.Path = dc.field(
        default_factory=lambda: pl.Path.home() / ".discord_mcp_cookies.json"
    )


async def _ensure_browser(state: ClientState) -> ClientState:
    if state.playwright and state.browser and state.context and state.page:
        return state

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=state.headless)

    ctx_kwargs = {}
    if state.cookies_file.exists():
        ctx_kwargs["storage_state"] = str(state.cookies_file)
    context = await browser.new_context(**ctx_kwargs)
    page = await context.new_page()

    return dc.replace(
        state, playwright=playwright, browser=browser, context=context, page=page
    )


async def _save_storage_state(state: ClientState) -> None:
    if state.page:
        await state.page.context.storage_state(path=str(state.cookies_file))


def _require_page(state: ClientState) -> Page:
    if not state.page:
        raise RuntimeError("Browser page not initialized")
    return state.page


class DiscordLoginError(Exception):
    """Login failed structurally; retrying the automated fetch won't help."""


class TransientLoginError(DiscordLoginError):
    """A stall or unconfirmed state; the retry loop may clear it."""


class CookieExpiredError(DiscordLoginError):
    """The persisted session is gone. Nothing here logs in — Discord's device
    verification, 2FA and CAPTCHA all expect a person — so this surfaces for a
    manual reseed instead."""


def _is_auth_url(url: str) -> bool:
    return "/login" in url or "/register" in url


async def _probe_login_state(
    state: ClientState, *, guild_nav_timeout_ms: int = 20000
) -> bool:
    """Whether the session is signed in, read only from positive signals: a
    rendered guild nav is True and an auth url is False. A stall or an error
    raises `TransientLoginError` instead of returning False, so a slow page load
    can't masquerade as a logged-out session and send a working cookie off to be
    reseeded by hand."""
    page = _require_page(state)
    try:
        await page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
    except PlaywrightError as e:
        raise TransientLoginError(f"navigation to /channels/@me failed: {e}") from e

    if _is_auth_url(page.url):
        return False

    try:
        await page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=guild_nav_timeout_ms,
        )
    except PlaywrightTimeoutError as e:
        # A late redirect to /login can land while we wait on the nav, so recheck
        # the url before calling it a stall.
        if _is_auth_url(page.url):
            return False
        raise TransientLoginError(
            f"guild nav not visible within {guild_nav_timeout_ms}ms (url={page.url})"
        ) from e
    except PlaywrightError as e:
        raise TransientLoginError(f"guild nav probe errored: {e}") from e

    # Guild nav rendered — we're in. The url can still read /channels/@me and lag
    # the render, so don't gate this on it.
    return True


async def reseed_cookie(state: ClientState) -> ClientState:
    """Open a visible browser at Discord's login page and persist the session a
    human signs in with. The only writer of the cookie file.

    Authentication is the human's whole job here: device verification, 2FA and
    CAPTCHA each expect a person, and driving the login form around them means
    tracking Discord's markup for a path that runs about once a quarter. So this
    waits on stdin rather than on any selector, and reads the result through the
    same guild-nav probe every other caller trusts.
    """
    if state.headless:
        raise ValueError(
            "reseed needs a visible browser: build the state headless=False"
        )

    state = await _ensure_browser(state)
    page = _require_page(state)
    # domcontentloaded, as everywhere else here: Discord's `load` waits on the
    # whole SPA. The 120s bound is for this path alone — a cold headed window
    # measured 31s against headless's 10s on 2026-08-15, and the default 30s
    # timed out on a reseed nobody would know how to read as "too slow".
    await page.goto(
        "https://discord.com/login", wait_until="domcontentloaded", timeout=120000
    )

    # input() off the event loop: the browser is a live subprocess this coroutine
    # still owns, and blocking here stops answering it.
    await asyncio.get_running_loop().run_in_executor(
        None, input, "Sign in at the browser window, then press Enter here: "
    )

    # Confirm before writing: an Enter pressed early would otherwise persist a
    # logged-out state over the cookie that was working. A stalled probe raises
    # too, so the write is reached only by a session read as signed in.
    if not await _probe_login_state(state):
        raise DiscordLoginError("not signed in; cookie file left alone")

    await _save_storage_state(state)
    logger.info(f"session saved to {state.cookies_file}")
    return dc.replace(state, logged_in=True)


async def _login(state: ClientState) -> ClientState:
    if state.logged_in:
        return state

    state = await _ensure_browser(state)

    # A stalled probe (slow load) re-probes with a longer wait rather than
    # calling the cookie dead, which would send a working session to reseed. The
    # second stall propagates, and the caller's retry is what clears it.
    try:
        signed_in = await _probe_login_state(state)
    except TransientLoginError as e:
        logger.debug(f"login state indeterminate ({e}); re-probing")
        signed_in = await _probe_login_state(state, guild_nav_timeout_ms=45000)

    if signed_in:
        return dc.replace(state, logged_in=True)

    raise CookieExpiredError(
        "Discord session cookie is expired or missing; reseed it by running"
        " `discord-mcp-reseed` and signing in at the browser it opens"
    )


async def close_client(state: ClientState) -> None:
    # Child before parent: a page/context must not outlive the browser it lives in.
    resources = [
        (state.page, "close"),
        (state.context, "close"),
        (state.browser, "close"),
        (state.playwright, "stop"),
    ]

    # Teardown never fails the caller's read, which has already happened — but a
    # resource that will not close leaks a browser per run, and that is only
    # findable if it says so.
    for resource, action in resources:
        try:
            if resource:
                await getattr(resource, action)()
        except Exception as e:
            logger.warning(f"{action} on {type(resource).__name__} failed: {e}")


async def _extract_rows(
    page: Page, js: str, arg: str | None = None, *, what: str, timeout_ms: int = 30000
) -> list[dict]:
    """The rows `js` extracts, once it has some. Waiting on the extractor's own
    output is what makes the wait mean "the contents are here": Discord renders a
    list's chrome ahead of what goes in it, so waiting on the container's own
    selector returns it empty.

    An extractor that never returns a row is a failed render or a Discord DOM
    change. The empty list it would otherwise hand back reads as a true answer —
    you belong to no guild, this guild has no channel — and gets believed, so
    empty is a failure here rather than a result.
    """
    try:
        await page.wait_for_function(
            f"(a) => ({js})(a).length > 0", arg, timeout=timeout_ms
        )
    except PlaywrightTimeoutError as e:
        raise RuntimeError(
            f"nothing rendered in {what} within {timeout_ms}ms;"
            " the extractor is broken (likely a Discord DOM change)"
        ) from e
    return await page.evaluate(js, arg)


# A guild row is a rail treeitem whose id suffix is the guild's snowflake — the
# numeric test is what drops the rail's own chrome (`home`, `create-join-button`,
# `guild-discover-button`, `app-download-button`). querySelectorAll sees every
# guild regardless of scroll.
_EXTRACT_GUILDS_JS = r"""
    () => {
        const guilds = [];
        const seen = new Set();
        const rows = document.querySelectorAll('[data-list-id="guildsnav"] [role="treeitem"]');
        for (const item of rows) {
            const id = item.getAttribute('data-list-item-id')?.replace('guildsnav___', '');
            if (!id || !/^[0-9]+$/.test(id) || seen.has(id)) continue;
            // A guild row renders as an icon, so its name is the one thing on it
            // written for screen readers. Read that accessible name — the row's
            // text minus every aria-hidden subtree — rather than plain textContent:
            // a guild with no uploaded icon renders its acronym in an aria-hidden
            // node, which textContent joined to the name as "Wrath and Glory 40kWaG4"
            // (measured 2026-08-16, once aria-label had gone from these rows).
            const named = item.cloneNode(true);
            for (const hidden of named.querySelectorAll('[aria-hidden="true"]')) hidden.remove();
            // The unread badge is spelled into that name and takes either end:
            // "Unread messages, Dungeons of Purdue" and "Lets *Actually* Kill The
            // Devil , 2 unread messages" (measured 2026-08-11 over 10 guilds). Left
            // in, it becomes part of the name and changes as messages arrive.
            const name = named.textContent
                .replace(/^unread messages,\s*/i, '')
                .replace(/^\d+\s+(mentions?|notifications?|unread),?\s*/i, '')
                .replace(/\s*,\s*\d+\s+unread\s+messages?$/i, '')
                .replace(/\s+/g, ' ')
                .trim();
            if (name) {
                seen.add(id);
                guilds.push({ id, name });
            }
        }
        return guilds;
    }
"""


async def get_guilds(state: ClientState) -> tuple[ClientState, list[DiscordGuild]]:
    state = await _login(state)
    page = _require_page(state)

    await page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded")

    # The rail's guilds arrive with the gateway's READY payload, about half a
    # second behind its chrome. One payload renders in one commit, so there is no
    # partial rail to catch: measured 2026-08-16, the count steps straight from 0
    # to all 9 on every navigation, including under a 20x CPU throttle that
    # stretched the wait to 78s. A logged-in account is in at least one guild.
    rows = await _extract_rows(page, _EXTRACT_GUILDS_JS, what="the guild rail")
    return state, [DiscordGuild(id=g["id"], name=g["name"]) for g in rows]


# Channel rows are read from their sidebar links: a `/channels/{guild}/{channel}`
# href is what makes a link a channel, and the guild segment is compared rather
# than interpolated into the pattern so the id never reaches a regex.
_EXTRACT_CHANNELS_JS = r"""
    (guildId) => {
        const channels = new Map();
        for (const link of document.querySelectorAll('a[href*="/channels/"]')) {
            const match = link.href.match(/\/channels\/([0-9]+)\/([0-9]+)/);
            if (!match || match[1] !== guildId || channels.has(match[2])) continue;
            // The row's own text runs the channel type and its hover buttons
            // together with the name ("TextannouncementsInvite to Channel",
            // measured 2026-08-11), so read the name node when there is one and
            // fall back to scrubbing the row only when there isn't.
            const nameNode = link.querySelector('[class*="name"]');
            const name = ((nameNode ?? link).textContent || '')
                .replace(/\s+/g, ' ').trim();
            channels.set(match[2], {
                id: match[2],
                name: name || `channel-${match[2]}`,
            });
        }
        return [...channels.values()];
    }
"""


async def get_guild_channels(
    state: ClientState, guild_id: str
) -> tuple[ClientState, list[DiscordChannel]]:
    state = await _login(state)
    page = _require_page(state)

    await page.goto(
        f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
    )

    # A guild you are in shows you at least one channel, so the same rule holds
    # here as for the rail: no row means the sidebar never rendered.
    rows = await _extract_rows(
        page, _EXTRACT_CHANNELS_JS, guild_id, what=f"guild {guild_id}'s channel list"
    )
    logger.debug(f"Found {len(rows)} channels in guild {guild_id}")

    return state, [DiscordChannel(id=ch["id"], name=ch["name"]) for ch in rows]


async def _extract_message_data(
    element, channel_id: str, collected: int
) -> DiscordMessage | None:
    try:
        message_id = (
            await element.get_attribute("id") or f"message-{collected}"
        ).replace("chat-messages-", "")

        content = ""
        for selector in [
            '[class*="messageContent"]',
            '[class*="markup"]',
            ".messageContent",
        ]:
            content_elem = await element.query_selector(selector)
            if content_elem and (text := await content_elem.text_content()):
                content = text.strip()
                break

        author_name = "Unknown"
        for selector in ['[class*="username"]', '[class*="authorName"]', ".username"]:
            author_elem = await element.query_selector(selector)
            if author_elem and (name := await author_elem.text_content()):
                author_name = name.strip()
                break

        timestamp_elem = await element.query_selector("time")
        timestamp_str = (
            await timestamp_elem.get_attribute("datetime") if timestamp_elem else None
        )
        timestamp = (
            datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if timestamp_str
            else datetime.now(timezone.utc)
        )

        attachments = [
            href
            for att in await element.query_selector_all('a[href*="cdn.discordapp.com"]')
            if (href := await att.get_attribute("href"))
        ]

        if not content and not attachments:
            return None

        return DiscordMessage(
            id=message_id,
            content=content,
            author_name=author_name,
            author_id="unknown",
            channel_id=channel_id,
            timestamp=timestamp,
            attachments=attachments,
        )
    except Exception:
        return None


async def get_channel_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    limit: int = 100,
    before: str | None = None,
    after: str | None = None,
) -> tuple[ClientState, list[DiscordMessage]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    await state.page.goto(
        f"https://discord.com/channels/{server_id}/{channel_id}",
        wait_until="domcontentloaded",
    )
    await state.page.wait_for_selector('[data-list-id="chat-messages"]', timeout=15000)

    # Scroll to bottom for newest messages
    await state.page.evaluate("""
        const chat = document.querySelector('[data-list-id="chat-messages"]');
        if (chat) chat.scrollTo(0, chat.scrollHeight);
        window.scrollTo(0, document.body.scrollHeight);
    """)
    await state.page.wait_for_timeout(2000)

    messages = []
    seen_ids = set()

    for attempt in range(10):
        elements = await state.page.query_selector_all(
            '[data-list-id="chat-messages"] [id^="chat-messages-"]'
        )
        if not elements:
            await state.page.keyboard.press("PageUp")
            await state.page.wait_for_timeout(1000)
            continue

        for element in reversed(elements):
            if len(messages) >= limit:
                break
            try:
                message = await _extract_message_data(
                    element, channel_id, len(seen_ids)
                )
                if message and message.id not in seen_ids:
                    if before and message.id >= before:
                        continue
                    if after and message.id <= after:
                        continue
                    seen_ids.add(message.id)
                    messages.append(message)
            except Exception:
                continue

        if len(messages) >= limit or not elements:
            break
        await state.page.keyboard.press("PageUp")
        await state.page.wait_for_timeout(1000)

    return state, sorted(messages, key=lambda m: m.timestamp, reverse=True)[:limit]
