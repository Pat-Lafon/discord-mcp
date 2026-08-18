import asyncio
import enum
import pathlib as pl
from datetime import UTC, datetime, timedelta
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
    channel_id: str
    timestamp: datetime
    attachments: list[str]


class StopReason(enum.Enum):
    """Which of paging's three exits it took — the caller's ground for trusting
    the read covers the window it asked for.

    Each is derived from something the feed rendered, so STALLED is left holding
    only the case where paging stopped and the feed never said why: Discord
    served no more history and no beginning came into view.
    """

    WINDOW_EDGE = "window-edge"  # a message older than `since` was rendered
    FEED_EXHAUSTED = "feed-exhausted"  # the feed's opening banner was rendered
    STALLED = "stalled"  # three passes surfaced no new row

    @property
    def covers_window(self) -> bool:
        """Whether the read reaches the window's start, so the caller may
        advance a watermark past it."""
        return self in (StopReason.WINDOW_EDGE, StopReason.FEED_EXHAUSTED)


@dc.dataclass(frozen=True)
class WindowRead:
    """A channel read plus why paging stopped."""

    messages: list[DiscordMessage]
    stop: StopReason


# A thread is addressed exactly like a channel — same id space, same url shape,
# same reader (`_open_channel`) — so `get_channel_threads` returns these too.
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


# Login state, as positive signals. A timeout/error is Indeterminate — never
# folded into LoggedOut, so a slow page load can't masquerade as a logged-out
# session and send a working cookie off to be reseeded by hand.
class LoginState:
    # A probe is formatted into the error a reseed shows its human, so the
    # field-less variants name themselves. Indeterminate's dataclass repr wins.
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class LoggedIn(LoginState): ...


class LoggedOut(LoginState): ...


@dc.dataclass(frozen=True)
class Indeterminate(LoginState):
    reason: str


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
) -> LoginState:
    page = _require_page(state)
    try:
        await page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
    except PlaywrightError as e:
        return Indeterminate(f"navigation to /channels/@me failed: {e}")

    if _is_auth_url(page.url):
        return LoggedOut()

    try:
        await page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=guild_nav_timeout_ms,
        )
    except PlaywrightTimeoutError:
        # A late redirect to /login can land while we wait on the nav, so recheck
        # the url before calling it indeterminate.
        if _is_auth_url(page.url):
            return LoggedOut()
        return Indeterminate(
            f"guild nav not visible within {guild_nav_timeout_ms}ms (url={page.url})"
        )
    except PlaywrightError as e:
        return Indeterminate(f"guild nav probe errored: {e}")

    # Guild nav rendered — we're in. The url can still read /channels/@me and lag
    # the render, so don't gate LoggedIn on it.
    return LoggedIn()


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
    # logged-out state over the cookie that was working.
    probe = await _probe_login_state(state)
    if not isinstance(probe, LoggedIn):
        raise DiscordLoginError(f"not signed in ({probe}); cookie file left alone")

    await _save_storage_state(state)
    logger.info(f"session saved to {state.cookies_file}")
    return dc.replace(state, logged_in=True)


async def _login(state: ClientState) -> ClientState:
    if state.logged_in:
        return state

    state = await _ensure_browser(state)

    # An indeterminate probe (slow load) re-probes with a longer wait rather than
    # calling the cookie dead, which would send a working session to reseed.
    probe = await _probe_login_state(state)
    if isinstance(probe, Indeterminate):
        logger.debug(f"login state indeterminate ({probe.reason}); re-probing")
        probe = await _probe_login_state(state, guild_nav_timeout_ms=45000)

    if isinstance(probe, LoggedIn):
        return dc.replace(state, logged_in=True)
    if isinstance(probe, Indeterminate):
        raise TransientLoginError(probe.reason)

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

    # The rail's chrome renders about half a second ahead of the guilds, which
    # arrive with the gateway's READY payload, so waiting on `[role="treeitem"]`
    # returns a rail holding no guild at all. Waiting on the extractor's own output
    # is what makes the wait mean "the guilds are here". One payload renders in one
    # commit, so there is no partial rail to catch: measured 2026-08-16, the count
    # steps straight from 0 to all 9 on every navigation, including under a 20x CPU
    # throttle that stretched the wait to 78s.
    try:
        await page.wait_for_function(
            f"() => ({_EXTRACT_GUILDS_JS})().length > 0", timeout=30000
        )
    except PlaywrightTimeoutError as e:
        # A logged-in account is in at least one guild, so an empty rail is a failed
        # render or a Discord DOM change. Returning zero guilds would read as "you
        # belong to none" and be believed.
        raise RuntimeError(
            "no guild rendered in the guild rail within 30s;"
            " guild extractor is broken (likely a Discord DOM change)"
        ) from e

    guilds_data = await page.evaluate(_EXTRACT_GUILDS_JS)
    return state, [DiscordGuild(id=g["id"], name=g["name"]) for g in guilds_data]


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
            let name = (nameNode ?? link).textContent?.trim() || '';
            name = name.replace(/^[^a-zA-Z0-9#-_]+/, '').trim();
            name = name.replace(/\s+/g, ' ').trim();
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
    await page.wait_for_timeout(3000)

    channels = await page.evaluate(_EXTRACT_CHANNELS_JS, guild_id)
    logger.debug(f"Found {len(channels)} channels in guild {guild_id}")

    return state, [DiscordChannel(id=ch["id"], name=ch["name"]) for ch in channels]


# Every rendered row, read in one pass over the document: per-row would be a
# round trip each, and the paging loop re-reads every rendered row every pass.
# Discord hashes its class names (`markup__75297`), so selectors match a
# substring.
#
# A reply row nests a preview of the quoted message — author name and text —
# ahead of its own, in nodes class-identical to the real ones (measured
# 2026-08-11 over 436 rows), so only the `repliedMessage` ancestor tells them
# apart. Every lookup below ignores that subtree, or a reply reports the message
# it quotes in place of itself and hands that author to the continuations under
# it.
#
# A row that throws, or that names no author and says nothing, comes back as
# null rather than losing the pass — one malformed row is tolerated while a DOM
# change that trips every row still reaches the caller's canary.
_EXTRACT_ROWS_JS = """
(channelId) => {
  const own = (el) => !el.closest('[class*="repliedMessage"]');
  // Discord renders every emoji as an <img> whose alt carries what was typed —
  // the character for a unicode emoji, `:name:` for a custom one — so
  // textContent alone drops it (measured 2026-08-11: 9 messages lost a
  // mid-sentence emoji, 3 emoji-only messages read as empty).
  //
  // Chrome that Discord nests *inside* the content node is dropped first, or it
  // reads as message text: a `timestamp` span holds the `(edited)` marker plus
  // the same instant spelled out for screen readers, and `hiddenVisually` spans
  // hold separators meant only to be read aloud (measured 2026-08-11 over 57
  // content nodes: 2 ended in "(edited)Sunday, July 19, 2026 at 8:06 PM", 5
  // carried a stray comma). Both are addressed to a screen reader or duplicate
  // what the row's id already dates, so nothing is lost by removing them.
  const readText = (el) => {
    const clone = el.cloneNode(true);
    for (const chrome of clone.querySelectorAll(
        '[class*="timestamp"], [class*="hiddenVisually"]')) {
      chrome.remove();
    }
    for (const img of clone.querySelectorAll('img[alt]')) {
      img.replaceWith(img.getAttribute('alt'));
    }
    return clone.textContent.trim();
  };
  // First own match holding text. Scanning past the empty ones is what reads a
  // forward: Discord leaves the row's own content node empty and puts the
  // forwarded body in a second one below it (6 such rows, 2026-08-11).
  const firstNode = (root, selector) =>
    [...root.querySelectorAll(selector)].find((el) => own(el) && readText(el));
  const textOf = (root, selector) => {
    const el = firstNode(root, selector);
    return el ? readText(el) : "";
  };

  // A poll's question and options are their own component, outside the content
  // node, so without this a poll row's text reads as empty.
  const pollOf = (row) => {
    const box = row.querySelector('[class*="pollContainer"]');
    if (!box) return null;
    return {
      question: textOf(box, 'h4'),
      options: [...box.querySelectorAll('li[class*="answer__"]')].map((li) => ({
        label: textOf(li, '[class*="label__"]'),
        votes: textOf(li, '[class*="votesData"] [class*="text__"]'),
      })),
    };
  };

  // `chat-messages-{channelId}-{messageId}` — the trailing segment is the
  // snowflake the caller dates the row from. A thread's opening banner is the
  // one rendered row that is no message, and Discord gives it the thread's own
  // id there; dropped here, since that id dates it to the thread's creation.
  const rows = [...document.querySelectorAll(
      '[data-list-id="chat-messages"] [id^="chat-messages-"]')]
      .filter((row) => row.id.split('-').pop() !== channelId);
  return rows.map((row) => {
    try {
      const contentEl = firstNode(row, '[class*="markup"]');
      // Discord marks a forward with a quote spine sitting *beside* the
      // snapshot's content rather than wrapping it, so the snapshot's root is
      // that spine's parent. A comment the author added sits in a content node
      // of its own ahead of the snapshot, so it is read first and goes
      // unmarked. `quote__` is specific to this spine — a markdown blockquote
      // is `blockquoteContainer__`/`blockquoteDivider__` (2026-08-11).
      const forwarded = !!contentEl
          && [...row.querySelectorAll('[class*="quote__"]')]
              .some((spine) => spine.parentElement.contains(contentEl));
      // A link or video posted with no text puts the URL nowhere but the
      // unfurl's anchor. Attachments already carry the cdn links, so excluding
      // them here keeps such a row from reporting its file twice.
      const link = [...row.querySelectorAll(
          'a[href^="http"]:not([href*="cdn.discordapp.com"])')].find(own);
      // Discord writes a username node only on the first row of a run, but
      // every continuation labels itself with that row's username node by id,
      // so authorship is read from the row rather than inferred from the
      // nearest name above it. Measured 2026-08-12 over 104 continuation rows:
      // every one carried a `message-username-*` idref and every idref
      // resolved, including across a pass where virtualization evicted 50 rows
      // — Discord evicts whole groups, so a continuation is never rendered
      // without its head. A reply row labels its article with the message it
      // quotes, so the prefix is matched rather than the first idref taken.
      const usernameRef = [...row.querySelectorAll('[aria-labelledby]')]
          .flatMap((el) => (el.getAttribute('aria-labelledby') || '').split(' '))
          .find((ref) => ref.startsWith('message-username-'));
      const named = usernameRef ? document.getElementById(usernameRef) : null;
      const authorName =
          textOf(row, '[class*="username"]') || (named ? readText(named) : "");
      const poll = pollOf(row);
      const content = contentEl ? readText(contentEl) : "";
      const attachments = [...row.querySelectorAll(
          'a[href*="cdn.discordapp.com"]')].filter(own)
          .map((a) => a.getAttribute('href'));
      // A row names someone and says something. Neither failed once across the
      // 250 rows of these feeds measured 2026-08-12, so a row that reads as
      // neither is a selector that stopped matching rather than a kind of row
      // — it joins the malformed ones the caller's canary counts.
      if (!authorName) return null;
      if (!content && !poll && !link && !attachments.length) return null;
      return {
        id: row.id.split('-').pop(),
        authorName: authorName,
        content: content,
        forwarded: forwarded,
        poll: poll,
        link: link ? link.getAttribute('href') : null,
        attachments: attachments,
      };
    } catch (e) {
      return null;
    }
  });
}
"""


def _content(raw: dict) -> str:
    """What the message says, as one string. A poll and a bare link say it
    outside the content node, so each is rendered here rather than carried as
    its own field — `content` is the whole contract every consumer reads."""
    body = raw["content"]
    if not body and (poll := raw["poll"]):
        body = "\n".join(
            [f"**Poll: {poll['question']}**"]
            + [f"- {o['label']} — {o['votes']}" for o in poll["options"]]
        )
    if not body and raw["link"]:
        body = raw["link"]
    if body and raw["forwarded"]:
        body = f"_Forwarded:_\n{body}"
    return body


# Discord ids are snowflakes: the high 42 bits are milliseconds since
# 2015-01-01, so an id carries its own post time. Dating a row from its id
# rather than its `<time>` element leaves nothing to guess at — a row cannot
# exist without an id, where a missing datetime would date it to now and so
# read as inside every window.
_SNOWFLAKE_EPOCH_MS = 1420070400000
_DISCORD_EPOCH = datetime.fromtimestamp(_SNOWFLAKE_EPOCH_MS / 1000, UTC)


def _posted_at(message_id: str) -> datetime:
    since_epoch_ms = int(message_id) >> 22
    posted = datetime.fromtimestamp((since_epoch_ms + _SNOWFLAKE_EPOCH_MS) / 1000, UTC)
    # Two ways this can be a date rather than a post time, both of which would
    # otherwise read as plausible and move the window boundary every caller
    # reads. An id with no timestamp bits set is no snowflake — it decodes to
    # the epoch instant itself, which reads as older than any window and would
    # end paging on the spot. A decode landing outside Discord's lifetime means
    # the shift or epoch below is wrong.
    if since_epoch_ms <= 0 or not (
        _DISCORD_EPOCH <= posted <= datetime.now(UTC) + timedelta(minutes=5)
    ):
        raise ValueError(
            f"id {message_id} decodes to {posted.isoformat()}, which is no post"
            " time; snowflake decoding is wrong or this id is not a message id"
        )
    return posted


def _message(raw: dict, channel_id: str) -> DiscordMessage:
    """The message a row amounts to. Every field is the row's own, so the
    extraction a pass returns is the whole message."""
    return DiscordMessage(
        id=raw["id"],
        content=_content(raw),
        author_name=raw["authorName"],
        channel_id=channel_id,
        timestamp=_posted_at(raw["id"]),
        attachments=raw["attachments"],
    )


async def _open_channel(
    state: ClientState, server_id: str, channel_id: str
) -> tuple[ClientState, Page]:
    """Navigate to a channel (or thread — a thread is addressed like a channel)
    and wait for its message list. Both steps are flaky, so both get the same
    generous bound and one retry: the chat list usually appears in ~2s but
    sometimes stalls indefinitely, and the navigation itself is what timed out on
    3 of the 22 daily runs to 2026-08-16 — the bare 30s default against a 16s
    cold start. Re-navigating rather than reloading is what recovers a `goto`
    that left the page nowhere."""
    state = await _login(state)
    page = _require_page(state)
    url = f"https://discord.com/channels/{server_id}/{channel_id}"

    async def open_once() -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('[data-list-id="chat-messages"]', timeout=60000)

    try:
        await open_once()
    except PlaywrightTimeoutError:
        await open_once()
    return state, page


async def get_channel_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    since: datetime,
) -> tuple[ClientState, WindowRead]:
    """Read a channel's (or thread's) messages back to `since`, newest first.

    `since` is the only bound on the read: one message older than the window
    ends the loop, so a quiet channel costs one pass and a busy one pays for
    every message its caller asked to see. A row cap would buy wall-clock by
    cutting the *oldest* rows read — the ones nearest `since`, which a watermark
    caller never revisits.
    """
    state, page = await _open_channel(state, server_id, channel_id)

    # Scroll to bottom for newest messages
    await page.evaluate("""
        const chat = document.querySelector('[data-list-id="chat-messages"]');
        if (chat) chat.scrollTo(0, chat.scrollHeight);
        window.scrollTo(0, document.body.scrollHeight);
    """)
    await page.wait_for_timeout(2000)

    seen: dict[str, dict] = {}
    reached_since = False
    stalled_passes = 0
    stop = StopReason.STALLED

    # Discord loads older history only when the feed's scroller reaches its top,
    # and each chunk prepends content that pushes the viewport back down, so the
    # scroller is jumped to 0 each pass. A keyboard PageUp moves one viewport
    # within already-rendered rows and loads nothing (measured 2026-07-30).
    #
    # The gesture reports which case it found, so a feed whose scroller this no
    # longer finds is told apart from one it simply cannot move.
    scroll_to_top = """
        () => {
          const list = document.querySelector('[data-list-id="chat-messages"]');
          if (!list) return "no-list";
          let s = list;
          while (s && s.scrollHeight <= s.clientHeight + 1) s = s.parentElement;
          if (!s) return "not-scrollable";
          s.scrollTo(0, 0);
          return "scrolled";
        }
    """

    # Exhaustion is a fact about content, so it is read from the one row that
    # states it: a feed's opening banner — "Welcome to #channel!", or a thread's
    # starter — which Discord gives the feed's own id in the
    # `chat-messages-{feed}-{row}` slot, and which the extractor drops for that
    # reason. Rendered, nothing older exists. Geometry answers a different
    # question and cannot stand in: a loaded feed taller than its pane scrolls
    # exactly like a stalled one, which is every feed here — measured
    # 2026-08-16, `#ravos` brought its first message into view on pass 6 of 9
    # with the scroller still reporting scrollable, then paged three more times
    # to conclude it had stalled. Probed by id because Discord's class names are
    # content-hashed and churn.
    at_beginning = """
        (channelId) => !!document.getElementById(
            `chat-messages-${channelId}-${channelId}`)
    """

    # Bounded by progress, not a fixed pass count, which would silently truncate
    # the old end of any window needing more passes than the cap.
    while stalled_passes < 3:
        extracted = await page.evaluate(_EXTRACT_ROWS_JS, channel_id)
        # The per-row catch is there to tolerate one malformed row. Past that,
        # the extractor is broken against a changed DOM, and the tell is a read
        # that comes back short rather than empty — short reads as a quiet
        # channel, which is the shape of an ordinary day here.
        nulls = sum(row is None for row in extracted)
        if nulls > max(1, len(extracted) // 10):
            raise RuntimeError(
                f"channel {channel_id}: {nulls} of {len(extracted)} rendered rows"
                " failed to extract; message extractor is broken (likely a"
                " Discord DOM change)"
            )
        logger.debug(
            f"pass: {len(extracted)} rows rendered ({nulls} unreadable),"
            f" {len(seen)} unique so far, stalled={stalled_passes}"
        )
        known_before_pass = len(seen)
        for raw in extracted:
            if raw is None:
                continue
            # Rows walk backward in time, so one past the edge means the window
            # is covered.
            if _posted_at(raw["id"]) < since:
                reached_since = True
            # Every field belongs to the row itself, so whichever pass last
            # rendered it read the whole story and simply overwrites — an edit,
            # or a component that mounted late, is not masked by the first
            # thing this row was ever seen to say.
            seen[raw["id"]] = raw

        if reached_since:
            stop = StopReason.WINDOW_EDGE
            break
        if await page.evaluate(at_beginning, channel_id):
            # The feed's first message is rendered, so `seen` already holds the
            # whole feed and the window's start is above anything that exists.
            stop = StopReason.FEED_EXHAUSTED
            break
        stalled_passes = 0 if len(seen) > known_before_pass else stalled_passes + 1
        scrolled = await page.evaluate(scroll_to_top)
        if scrolled == "no-list":
            raise RuntimeError(
                f"channel {channel_id}: message list vanished mid-page; paging"
                " is broken (failed load or Discord DOM change)"
            )
        if scrolled == "not-scrollable":
            # Every row the feed has fits one viewport, so there is no older
            # history to load and the read is whole, not merely stalled.
            stop = StopReason.FEED_EXHAUSTED
            break
        await page.wait_for_timeout(1000)

    messages = [_message(raw, channel_id) for raw in seen.values()]

    return state, WindowRead(
        messages=sorted(messages, key=lambda m: m.timestamp, reverse=True),
        stop=stop,
    )


# Discord renders a channel's active threads in the left sidebar grouped in a
# ul[role="group"] labeled "{channelName} threads" — that IS Discord's thread
# index, so we read it (selectors below) instead of inferring threads from
# creation markers in the message feed. We match the group by that label rather
# than walking up from the channel row, because the row's nearest <li> excludes
# the group; the label scopes to this channel even with several expanded.
#
# A row's label reads "{name} ({type})" with any state appended after it, comma
# separated — "the-furious-five (text channel), Private Channel (locked)",
# "Philbertia-Voice (voice channel), unread" (measured 2026-08-15 over 27 rows).
# So a name is everything before the first " (", and membership in the group is
# what makes a row a thread: a label suffix would stop matching the moment the
# thread had unread messages, which is the only time its rows are worth reading.
_THREAD_SCRAPE_JS = r"""
    (channelId) => {
        const parent = document.querySelector(
            `[data-list-item-id="channels___${channelId}"]`);
        const name = (parent.getAttribute('aria-label') || '').split(' (')[0];
        const grp = document.querySelector(
            `ul[role="group"][aria-label="${name} threads"]`);
        if (!grp) return [];  // no group -> channel has no active threads
        const out = [];
        for (const el of grp.querySelectorAll('[data-list-item-id^="channels___"]')) {
            out.push({
                id: el.getAttribute('data-list-item-id').replace('channels___', ''),
                name: (el.getAttribute('aria-label') || '').split(' (')[0].trim(),
            });
        }
        return out;
    }
"""


async def get_channel_threads(
    state: ClientState, server_id: str, channel_id: str
) -> tuple[ClientState, list[DiscordChannel]]:
    """Discover a channel's active threads from Discord's own sidebar thread
    index (see `_THREAD_SCRAPE_JS`), read after navigating to the channel so it
    is the selected one whose thread group is rendered. A thread is found
    regardless of when it was created.

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
        DiscordChannel(id=row["id"], name=row["name"] or f"thread-{row['id']}")
        for row in await page.evaluate(_THREAD_SCRAPE_JS, channel_id)
    ]
    logger.debug(f"Discovered {len(threads)} thread(s) under channel {channel_id}")
    return state, threads
