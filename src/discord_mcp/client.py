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
    type: int
    guild_id: str | None


@dc.dataclass(frozen=True)
class DiscordGuild:
    id: str
    name: str
    icon: str | None = None


@dc.dataclass(frozen=True)
class ClientState:
    email: str
    password: str
    headless: bool = True
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    logged_in: bool = False
    cookies_file: pl.Path = dc.field(
        default_factory=lambda: pl.Path.home() / ".discord_mcp_cookies.json"
    )


def create_client_state(
    email: str, password: str, headless: bool = True
) -> ClientState:
    return ClientState(email=email, password=password, headless=headless)


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


async def get_guilds(state: ClientState) -> tuple[ClientState, list[DiscordGuild]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    logger.debug("Starting guild detection process")
    await state.page.goto(
        "https://discord.com/channels/@me", wait_until="domcontentloaded"
    )
    logger.debug(f"Navigated to Discord, current URL: {state.page.url}")

    # Wait for Discord to fully load guilds with text content
    try:
        await state.page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=15000,
        )
        await state.page.wait_for_timeout(5000)

        # Scroll guild navigation to load all guilds
        await state.page.evaluate("""
            () => {
                const guildNav = document.querySelector('[data-list-id="guildsnav"]');
                const container = guildNav?.closest('[class*="guilds"]') || guildNav?.parentElement;
                if (container) {
                    container.scrollTop = 0;
                    return new Promise(resolve => {
                        let scrolls = 0;
                        const interval = setInterval(() => {
                            container.scrollBy(0, 100);
                            if (++scrolls >= 20 || container.scrollTop + container.clientHeight >= container.scrollHeight - 10) {
                                clearInterval(interval);
                                resolve();
                            }
                        }, 100);
                    });
                }
            }
        """)
        await state.page.wait_for_timeout(2000)
    except Exception:
        pass

    # Extract guild information from navigation elements
    guilds_data = await state.page.evaluate("""
        () => {
            const guilds = [];
            const treeItems = document.querySelectorAll('[data-list-id="guildsnav"] [role="treeitem"]');
            
            treeItems.forEach(item => {
                const listItemId = item.getAttribute('data-list-item-id');
                if (listItemId?.startsWith('guildsnav___') && listItemId !== 'guildsnav___home') {
                    const guildId = listItemId.replace('guildsnav___', '');
                    if (/^[0-9]+$/.test(guildId)) {
                        // Extract guild name from tree item text
                        let guildName = null;
                        const textElements = item.querySelectorAll('*');
                        for (let elem of textElements) {
                            const text = elem.textContent?.trim();
                            if (text && text.length > 2 && text.length < 100 && 
                                !text.includes('notification') && !text.includes('unread') &&
                                !text.match(/^\\d+$/)) {
                                guildName = text;
                                break;
                            }
                        }
                        
                        if (!guildName) {
                            const fullText = item.textContent?.trim();
                            if (fullText) {
                                guildName = fullText.replace(/^\\d+\\s+mentions?,\\s*/, '').replace(/\\s+/g, ' ').trim();
                            }
                        }
                        
                        // Clean up mention prefixes
                        if (guildName) {
                            guildName = guildName.replace(/^\\d+\\s+mentions?,\\s*/, '').trim();
                        }
                        
                        if (guildName && !guilds.some(g => g.id === guildId)) {
                            guilds.push({ id: guildId, name: guildName });
                        }
                    }
                }
            });
            
            return guilds;
        }
    """)

    # Convert JavaScript results to DiscordGuild objects
    guilds = [
        DiscordGuild(id=guild_data["id"], name=guild_data["name"], icon=None)
        for guild_data in guilds_data
    ]

    return state, guilds


async def get_guild_channels(
    state: ClientState, guild_id: str
) -> tuple[ClientState, list[DiscordChannel]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    await state.page.goto(
        f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
    )
    await state.page.wait_for_timeout(3000)

    # Helper function to extract channels
    def extract_channels_js() -> str:
        return f"""
            (() => {{
                const channels = [];
                const seenIds = new Set();
                const links = document.querySelectorAll('a[href*="/channels/"]');
                
                links.forEach(link => {{
                    const match = link.href.match(/\\/channels\\/{guild_id}\\/([0-9]+)/);
                    if (match) {{
                        const channelId = match[1];
                        if (!seenIds.has(channelId)) {{
                            seenIds.add(channelId);
                            let name = link.textContent?.trim() || '';
                            name = name.replace(/^[^a-zA-Z0-9#-_]+/, '').trim();
                            name = name.replace(/\\s+/g, ' ').trim();
                            channels.push({{
                                id: channelId,
                                name: name || `channel-${{channelId}}`,
                                href: link.href
                            }});
                        }}
                    }}
                }});
                return channels;
            }})()
        """

    # Step 1: Get original channels
    logger.debug("Getting original channels")
    original_channels = await state.page.evaluate(extract_channels_js())
    logger.debug(f"Found {len(original_channels)} original channels")

    # Step 2: Click Browse Channels and get additional channels
    browse_channels = []
    try:
        browse_element = await state.page.query_selector(
            '*:has-text("Browse Channels")'
        )
        if browse_element and await browse_element.is_visible():
            await browse_element.click()
            await state.page.wait_for_timeout(5000)
            logger.debug("Clicked Browse Channels")

            # Scroll all scrollable elements to load hidden channels
            await state.page.evaluate("""
                Array.from(document.querySelectorAll('*'))
                    .filter(el => el.scrollHeight > el.clientHeight + 5)
                    .forEach(el => el.scrollTop = el.scrollHeight)
            """)
            await state.page.wait_for_timeout(3000)

            browse_channels = await state.page.evaluate(extract_channels_js())
            logger.debug(f"Found {len(browse_channels)} browse channels")
    except Exception as e:
        logger.debug(f"Browse Channels failed: {e}")

    # Step 3: Combine channels (original first, then new browse channels)
    all_channels = {}
    final_channels = []

    # Add original channels first
    for ch in original_channels:
        all_channels[ch["id"]] = ch
        final_channels.append(ch)

    # Add new browse channels
    for ch in browse_channels:
        if ch["id"] not in all_channels:
            final_channels.append(ch)

    logger.debug(f"Total unique channels: {len(final_channels)}")

    channels = [
        DiscordChannel(id=ch["id"], name=ch["name"], type=0, guild_id=guild_id)
        for ch in final_channels
    ]

    return state, channels


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
