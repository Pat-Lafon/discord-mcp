import asyncio
import dataclasses as dc
import pathlib as pl
from datetime import UTC, datetime

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
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
class ThreadRef:
    id: str
    name: str


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


# Login state, as positive signals. A timeout/error is Indeterminate — never
# folded into LoggedOut, so a slow page load can't masquerade as a logged-out
# session and force the fragile credential form when the cookies are still good.
class LoginState: ...


@dc.dataclass(frozen=True)
class LoggedIn(LoginState): ...


@dc.dataclass(frozen=True)
class LoggedOut(LoginState): ...


@dc.dataclass(frozen=True)
class Indeterminate(LoginState):
    reason: str


class DiscordLoginError(Exception):
    """Login failed structurally; retrying the automated fetch won't help."""


class TransientLoginError(DiscordLoginError):
    """A stall or unconfirmed state; the retry loop may clear it."""


class CookieExpiredError(DiscordLoginError):
    """The persisted session is gone. The headless job won't relogin — a fresh
    login trips Discord's device verification with no human to clear it — so this
    surfaces for a headed reseed instead."""


def _is_auth_url(url: str) -> bool:
    return "/login" in url or "/register" in url


async def _probe_login_state(
    state: ClientState, *, guild_nav_timeout_ms: int = 20000
) -> LoginState:
    if not state.page:
        return Indeterminate("browser page not initialized")
    try:
        await state.page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
    except (PlaywrightError, PlaywrightTimeoutError) as e:
        return Indeterminate(f"navigation to /channels/@me failed: {e}")

    if _is_auth_url(state.page.url):
        return LoggedOut()

    try:
        await state.page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=guild_nav_timeout_ms,
        )
    except PlaywrightTimeoutError:
        # A late redirect to /login can land while we wait on the nav, so recheck
        # the url before calling it indeterminate.
        if _is_auth_url(state.page.url):
            return LoggedOut()
        return Indeterminate(
            f"guild nav not visible within {guild_nav_timeout_ms}ms"
            f" (url={state.page.url})"
        )
    except PlaywrightError as e:
        return Indeterminate(f"guild nav probe errored: {e}")

    # Guild nav rendered — we're in. The url can still read /channels/@me and lag
    # the render, so don't gate LoggedIn on it.
    return LoggedIn()


_EMAIL_FIELD = 'input[name="email"]'


async def _perform_credential_login(state: ClientState) -> ClientState:
    page = _require_page(state)

    # The fatal DiscordLoginError (a Discord DOM change) isn't a PlaywrightError,
    # so it escapes this handler; any raw Playwright failure in the
    # goto/fill/click/wait path is a transient stall, not a structural block.
    try:
        await page.goto("https://discord.com/login")
        try:
            await page.wait_for_selector(_EMAIL_FIELD, state="visible", timeout=45000)
        except PlaywrightTimeoutError:
            raise DiscordLoginError("login form did not appear") from None

        await page.fill(_EMAIL_FIELD, state.email)
        await page.fill('input[name="password"]', state.password)
        await page.click('button[type="submit"]')

        await page.wait_for_function(
            "() => !window.location.href.includes('/login')", timeout=60000
        )
    except (PlaywrightError, PlaywrightTimeoutError) as e:
        raise TransientLoginError(f"login did not complete: {e}") from e

    probe = await _probe_login_state(state, guild_nav_timeout_ms=30000)
    if not isinstance(probe, LoggedIn):
        raise TransientLoginError(f"login submitted but state unconfirmed: {probe}")

    state = dc.replace(state, logged_in=True)
    await _save_storage_state(state)
    return state


async def _login(state: ClientState) -> ClientState:
    if state.logged_in:
        return state

    state = await _ensure_browser(state)

    # An indeterminate probe (slow load) re-probes with a longer wait rather than
    # diving into the credential form, whose email field would just time out too.
    probe = await _probe_login_state(state)
    if isinstance(probe, Indeterminate):
        logger.debug(f"login state indeterminate ({probe.reason}); re-probing")
        probe = await _probe_login_state(state, guild_nav_timeout_ms=45000)

    if isinstance(probe, LoggedIn):
        return dc.replace(state, logged_in=True)
    if isinstance(probe, Indeterminate):
        raise TransientLoginError(probe.reason)

    # LoggedOut: the cookie is dead. Only a headed run reseeds it (see
    # CookieExpiredError); the headless job surfaces the prompt instead.
    if state.headless:
        raise CookieExpiredError(
            "Discord session cookie is expired or missing; rerun headed"
            " (DISCORD_HEADLESS=false) to log in and reseed it"
        )
    return await _perform_credential_login(state)


async def close_client(state: ClientState) -> None:
    # Child before parent: a page/context must not outlive the browser it lives in.
    resources = [
        (state.page, "close"),
        (state.context, "close"),
        (state.browser, "close"),
        (state.playwright, "stop"),
    ]

    for resource, action in resources:
        try:
            if resource:
                await getattr(resource, action)()
        except Exception:
            pass


async def get_guilds(state: ClientState) -> tuple[ClientState, list[DiscordGuild]]:
    state = await _login(state)
    page = _require_page(state)

    await page.goto(
        "https://discord.com/channels/@me", wait_until="domcontentloaded"
    )

    # The guild rail loads in one jump *after* the non-guild chrome renders, so
    # waiting on a treeitem fires too early and extracts nothing — poll instead,
    # accepting the result once the guild count is positive and stops changing.
    # querySelectorAll sees every guild regardless of scroll.
    extract = """
        () => {
            const guilds = [];
            const seen = new Set();
            const rows = document.querySelectorAll('[data-list-id="guildsnav"] [role="treeitem"]');
            for (const item of rows) {
                const id = item.getAttribute('data-list-item-id')?.replace('guildsnav___', '');
                if (!id || !/^[0-9]+$/.test(id) || seen.has(id)) continue;
                const name = (item.getAttribute('aria-label') || item.textContent || '')
                    .replace(/^\\d+\\s+(mentions?|notifications?|unread),?\\s*/i, '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                if (name) {
                    seen.add(id);
                    guilds.push({ id, name });
                }
            }
            return guilds;
        }
    """
    prev = None
    for _ in range(25):  # ~15s ceiling at 600ms/poll
        guilds_data = await page.evaluate(extract)
        if guilds_data and len(guilds_data) == prev:
            break
        prev = len(guilds_data)
        await page.wait_for_timeout(600)
    else:
        return state, []

    guilds = [
        DiscordGuild(id=g["id"], name=g["name"], icon=None) for g in guilds_data
    ]

    return state, guilds


async def get_guild_channels(
    state: ClientState, guild_id: str
) -> tuple[ClientState, list[DiscordChannel]]:
    state = await _login(state)
    page = _require_page(state)

    await page.goto(
        f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
    )
    await page.wait_for_timeout(3000)

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

    logger.debug("Getting original channels")
    original_channels = await page.evaluate(extract_channels_js())
    logger.debug(f"Found {len(original_channels)} original channels")

    # The initial sidebar scrape can miss channels; opening Browse Channels
    # surfaces the rest, deduped against the first set below.
    browse_channels = []
    try:
        browse_element = await page.query_selector(
            '*:has-text("Browse Channels")'
        )
        if browse_element and await browse_element.is_visible():
            await browse_element.click()
            await page.wait_for_timeout(5000)
            logger.debug("Clicked Browse Channels")

            # Scroll all scrollable elements to load hidden channels
            await page.evaluate("""
                Array.from(document.querySelectorAll('*'))
                    .filter(el => el.scrollHeight > el.clientHeight + 5)
                    .forEach(el => el.scrollTop = el.scrollHeight)
            """)
            await page.wait_for_timeout(3000)

            browse_channels = await page.evaluate(extract_channels_js())
            logger.debug(f"Found {len(browse_channels)} browse channels")
    except Exception as e:
        logger.debug(f"Browse Channels failed: {e}")

    all_channels = {}
    final_channels = []

    for ch in original_channels:
        all_channels[ch["id"]] = ch
        final_channels.append(ch)

    for ch in browse_channels:
        if ch["id"] not in all_channels:
            final_channels.append(ch)

    logger.debug(f"Total unique channels: {len(final_channels)}")

    channels = [
        DiscordChannel(id=ch["id"], name=ch["name"], type=0, guild_id=guild_id)
        for ch in final_channels
    ]

    return state, channels


# Returns None for a row with no content and no attachments (a system/embed row
# we skip) — a legitimate empty, distinct from a parse failure. Errors are NOT
# swallowed here: they propagate so the caller can count them and tell "one bad
# row" apart from "the extractor is broken" (see get_channel_messages).
async def _extract_message_data(
    element, channel_id: str, collected: int
) -> DiscordMessage | None:
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
        else datetime.now(UTC)
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


async def _open_channel(
    state: ClientState, server_id: str, channel_id: str
) -> tuple[ClientState, Page]:
    """Navigate to a channel (or thread — a thread is addressed like a channel)
    and wait for its message list. Headless renders are flaky: the chat list
    usually appears in ~2s but sometimes stalls indefinitely, so wait generously
    and reload once before giving up."""
    state = await _login(state)
    page = _require_page(state)
    await page.goto(
        f"https://discord.com/channels/{server_id}/{channel_id}",
        wait_until="domcontentloaded",
    )
    try:
        await page.wait_for_selector('[data-list-id="chat-messages"]', timeout=60000)
    except PlaywrightTimeoutError:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector('[data-list-id="chat-messages"]', timeout=60000)
    return state, page


async def get_channel_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    limit: int = 100,
    before: str | None = None,
    after: str | None = None,
) -> tuple[ClientState, list[DiscordMessage]]:
    state, page = await _open_channel(state, server_id, channel_id)

    # Scroll to bottom for newest messages
    await page.evaluate("""
        const chat = document.querySelector('[data-list-id="chat-messages"]');
        if (chat) chat.scrollTo(0, chat.scrollHeight);
        window.scrollTo(0, document.body.scrollHeight);
    """)
    await page.wait_for_timeout(2000)

    messages = []
    seen_ids = set()
    rows_seen = False
    extract_errors = 0

    for _ in range(10):
        elements = await page.query_selector_all(
            '[data-list-id="chat-messages"] [id^="chat-messages-"]'
        )
        if not elements:
            await page.keyboard.press("PageUp")
            await page.wait_for_timeout(1000)
            continue
        rows_seen = True

        for element in reversed(elements):
            if len(messages) >= limit:
                break
            # Tolerate a single malformed row, but count the failure — a broken
            # extractor (DOM change) trips every row and is caught by the canary.
            try:
                message = await _extract_message_data(
                    element, channel_id, len(seen_ids)
                )
            except Exception:
                extract_errors += 1
                continue
            if message and message.id not in seen_ids:
                if before and message.id >= before:
                    continue
                if after and message.id <= after:
                    continue
                seen_ids.add(message.id)
                messages.append(message)

        if len(messages) >= limit:
            break
        await page.keyboard.press("PageUp")
        await page.wait_for_timeout(1000)

    # Rows were present but every extraction attempt threw: the extractor is
    # broken, not the channel empty. Fail loud rather than report zero messages.
    if rows_seen and not messages and extract_errors:
        raise RuntimeError(
            f"channel {channel_id}: saw message rows but extracted 0 messages"
            f" ({extract_errors} extraction errors); message extractor is broken"
            " (likely a Discord DOM change)"
        )

    return state, sorted(messages, key=lambda m: m.timestamp, reverse=True)[:limit]


# Discord renders a channel's active threads in the left sidebar grouped in a
# ul[role="group"] labeled "{channelName} threads" — that IS Discord's thread
# index, so we read it (selectors below) instead of inferring threads from
# creation markers in the message feed. We match the group by that label rather
# than walking up from the channel row, because the row's nearest <li> excludes
# the group; the label scopes to this channel even with several expanded.
_THREAD_SCRAPE_JS = r"""
    (channelId) => {
        const parent = document.querySelector(
            `[data-list-item-id="channels___${channelId}"]`);
        const name = (parent.getAttribute('aria-label') || '').split(' (')[0];
        const grp = document.querySelector(
            `ul[role="group"][aria-label="${name} threads"]`);
        if (!grp) return [];  // no group -> channel has no active threads
        const out = [];
        for (const el of grp.querySelectorAll(
                '[data-list-item-id^="channels___"][aria-label$="(thread)"]')) {
            out.push({
                id: el.getAttribute('data-list-item-id').replace('channels___', ''),
                name: (el.getAttribute('aria-label') || '')
                    .replace(/\s*\(thread\)$/, '').trim(),
            });
        }
        return out;
    }
"""


async def get_channel_threads(
    state: ClientState, server_id: str, channel_id: str
) -> tuple[ClientState, list[ThreadRef]]:
    """Discover a channel's active threads from Discord's own sidebar thread
    index (see `_THREAD_SCRAPE_JS`), read after navigating to the channel so it
    is the selected one whose thread group is rendered. Unlike the old
    feed-marker scrape, this finds a thread regardless of when it was created.

    Boundary: the sidebar lists *active* (non-archived) threads only. A thread
    that had activity in the review window but has since auto-archived won't
    appear — reading archived threads means driving the header Threads popout's
    search/scroll, which exposes no stable thread ids. A thread is read like any
    channel via its id."""
    state, page = await _open_channel(state, server_id, channel_id)
    # We just navigated here, so this channel's own sidebar row must render. If it
    # never does, the page is broken (failed load or Discord DOM change) — fail
    # loud rather than report the channel as thread-free and silently drop every
    # thread under it.
    try:
        await page.wait_for_selector(
            f'[data-list-item-id="channels___{channel_id}"]', timeout=15000
        )
    except PlaywrightTimeoutError as e:
        raise RuntimeError(
            f"channel {channel_id} sidebar row never rendered; thread scrape is"
            " broken (failed load or Discord DOM change)"
        ) from e

    threads = [
        ThreadRef(id=row["id"], name=row["name"] or f"thread-{row['id']}")
        for row in await page.evaluate(_THREAD_SCRAPE_JS, channel_id)
    ]
    logger.debug(f"Discovered {len(threads)} thread(s) under channel {channel_id}")
    return state, threads


async def send_message(
    state: ClientState, server_id: str, channel_id: str, content: str
) -> tuple[ClientState, str]:
    state = await _login(state)
    page = _require_page(state)

    await page.goto(
        f"https://discord.com/channels/{server_id}/{channel_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_selector('[data-slate-editor="true"]', timeout=10000)

    message_input = await page.query_selector('[data-slate-editor="true"]')
    if not message_input:
        raise RuntimeError("Could not find message input")

    await message_input.fill(content)
    await page.keyboard.press("Enter")
    await asyncio.sleep(1)

    return state, f"sent-{int(datetime.now().timestamp())}"
