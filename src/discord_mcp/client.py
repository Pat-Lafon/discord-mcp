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
    """Which of paging's four exits it took — the caller's ground for trusting
    the read covers the window it asked for.

    STALLED is the one that stays ambiguous: three passes surfacing no new row
    is what a feed whose loaded history ends above the window looks like, and
    also what a broken scroll gesture looks like. Every other exit is derived,
    not inferred.
    """

    WINDOW_EDGE = "window-edge"  # a message older than `since` was rendered
    FEED_EXHAUSTED = "feed-exhausted"  # feed fits its viewport; nothing above it
    LIMIT = "limit"  # `limit` reached with older messages still unread
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
class LoginState:
    # A probe is formatted into the error a headed reseed shows its human, so the
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
    """The persisted session is gone. The headless job won't relogin — a fresh
    login trips Discord's device verification with no human to clear it — so this
    surfaces for a headed reseed instead."""


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
    except PlaywrightError as e:
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
    page = _require_page(state)

    await page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded")

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
                // A guild row renders as an icon, so its only text is the label
                // written for screen readers — the name with any unread badge
                // spelled into it. Measured 2026-08-11 over 10 guilds, the badge
                // takes either end: "Unread messages, Dungeons of Purdue" and
                // "Lets *Actually* Kill The Devil , 2 unread messages". Left in,
                // it becomes part of the name and changes as messages arrive.
                const name = (item.getAttribute('aria-label') || item.textContent || '')
                    .replace(/^unread messages,\\s*/i, '')
                    .replace(/^\\d+\\s+(mentions?|notifications?|unread),?\\s*/i, '')
                    .replace(/\\s*,\\s*\\d+\\s+unread\\s+messages?$/i, '')
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
            return state, [
                DiscordGuild(id=g["id"], name=g["name"]) for g in guilds_data
            ]
        prev = len(guilds_data)
        await page.wait_for_timeout(600)

    # A logged-in account is in at least one guild, so a rail that never settles
    # on a positive count is a failed render or a Discord DOM change. Reporting
    # zero guilds would read as "you belong to none" and be believed.
    raise RuntimeError(
        f"guild rail never settled ({prev} guild(s) on the last of 25 polls);"
        " guild extractor is broken (likely a Discord DOM change)"
    )


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

    logger.debug("Getting original channels")
    original_channels = await page.evaluate(_EXTRACT_CHANNELS_JS, guild_id)
    logger.debug(f"Found {len(original_channels)} original channels")

    # The initial sidebar scrape can miss channels; opening Browse Channels
    # surfaces the rest, deduped against the first set below.
    browse_channels = []
    try:
        browse_element = await page.query_selector('*:has-text("Browse Channels")')
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

            browse_channels = await page.evaluate(_EXTRACT_CHANNELS_JS, guild_id)
            logger.debug(f"Found {len(browse_channels)} browse channels")
    except Exception as e:
        # Enrichment, so a failure still returns the sidebar's channels — but at
        # warning level, or a Browse Channels that quietly stops working shows
        # up only as channels missing from the result.
        logger.warning(f"Browse Channels failed: {e}")

    by_id: dict[str, dict] = {}
    for ch in [*original_channels, *browse_channels]:
        by_id.setdefault(ch["id"], ch)
    logger.debug(f"Total unique channels: {len(by_id)}")

    return state, [
        DiscordChannel(id=ch["id"], name=ch["name"]) for ch in by_id.values()
    ]


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
# A row that throws comes back as null rather than losing the pass, so one
# malformed row is tolerated while a DOM change that trips every row still
# reaches the caller's canary.
_EXTRACT_ROWS_JS = """
() => {
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

  const rows = document.querySelectorAll(
      '[data-list-id="chat-messages"] [id^="chat-messages-"]');
  return [...rows].map((row) => {
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
      return {
        // `chat-messages-{channelId}-{messageId}` — the trailing segment is the
        // snowflake the caller dates the row from. A thread's opening banner
        // carries the thread's own id there instead of a message's.
        id: row.id.split('-').pop(),
        authorName: textOf(row, '[class*="username"]') || null,
        content: contentEl ? readText(contentEl) : "",
        forwarded: forwarded,
        poll: pollOf(row),
        link: link ? link.getAttribute('href') : null,
        attachments: [...row.querySelectorAll('a[href*="cdn.discordapp.com"]')]
            .filter(own).map(a => a.getAttribute('href')),
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


# Read whole, a row names its author and says something. Neither placeholder is
# a value any Discord row produces, so a degraded row reads as degraded in the
# report rather than as an ordinary message from a user named "".
_UNKNOWN_AUTHOR = "(unknown author)"
_UNREADABLE_CONTENT = "(row rendered but no text, attachment, poll or link was read)"


@dc.dataclass(frozen=True)
class _Obs:
    """A row as the passes so far have read it.

    `content` and `attachments` belong to the row itself, so whichever pass last
    rendered it read the whole story and simply overwrites. `author_name` is the
    one field that grows across passes: Discord writes a name only on the first
    row of a run, and that row may sit above what has loaded, so an early pass
    can have no name to carry down where a later one does.
    """

    author_name: str | None
    content: str
    attachments: list[str]

    def degradation(self) -> str | None:
        """What paging failed to read, or None for a row read whole. The two
        want different fixes: no author means paging stopped below the row
        naming that run, no content means the selectors missed text on the
        page."""
        if self.author_name is None:
            return "no author"
        if not self.content and not self.attachments:
            return "no content"
        return None


def _message(message_id: str, obs: _Obs, channel_id: str) -> DiscordMessage:
    """The message a row amounts to. A row read only in part still reports —
    standing in for what wasn't read — because a reader of the report is the
    only one positioned to notice a gap, and a row withheld from them is a gap
    nothing shows."""
    return DiscordMessage(
        id=message_id,
        content=obs.content or ("" if obs.attachments else _UNREADABLE_CONTENT),
        author_name=obs.author_name or _UNKNOWN_AUTHOR,
        channel_id=channel_id,
        timestamp=_posted_at(message_id),
        attachments=obs.attachments,
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
    since: datetime,
    limit: int = 100,
) -> tuple[ClientState, WindowRead]:
    """Read a channel's (or thread's) messages back to `since`, newest first.

    `since` bounds the paging itself, not just the result: one message older
    than the window ends the loop, so a quiet channel costs one pass. `limit`
    caps what comes back and stops paging early, standing in for the window on
    a feed busy enough to make covering it unaffordable.
    """
    state, page = await _open_channel(state, server_id, channel_id)

    # Scroll to bottom for newest messages
    await page.evaluate("""
        const chat = document.querySelector('[data-list-id="chat-messages"]');
        if (chat) chat.scrollTo(0, chat.scrollHeight);
        window.scrollTo(0, document.body.scrollHeight);
    """)
    await page.wait_for_timeout(2000)

    seen: dict[str, _Obs] = {}
    reached_since = False
    stalled_passes = 0
    stop = StopReason.STALLED

    # Discord loads older history only when the feed's scroller reaches its top,
    # and each chunk prepends content that pushes the viewport back down, so the
    # scroller is jumped to 0 each pass. A keyboard PageUp moves one viewport
    # within already-rendered rows and loads nothing (measured 2026-07-30).
    #
    # The gesture reports which case it found, so a feed too short to scroll is
    # told apart from one whose scroller this no longer finds — the first has no
    # history left to load, the second loads none because it is broken.
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

    # Bounded by progress, not a fixed pass count, which would silently truncate
    # the old end of any window needing more passes than the cap.
    while stalled_passes < 3:
        extracted = await page.evaluate(_EXTRACT_ROWS_JS)
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
        # Oldest first: Discord omits the username header on a run of messages
        # from one author, so an unnamed row takes the nearest name above it.
        # Carried over rendered rows, since an unreadable row (a poll, measured
        # 2026-08-11) can be a run's only named one.
        carried: str | None = None
        for raw in extracted:
            if raw is None:
                continue
            author = raw["authorName"] or carried
            carried = author
            # A thread's opening banner is the one rendered row that is no
            # message; Discord gives it the thread's own id. Dropped before the
            # window check, since that id dates it to the thread's creation.
            if raw["id"] == channel_id:
                continue
            # Rows walk backward in time, so one past the edge means the window
            # is covered — asked of every row, readable or not.
            if _posted_at(raw["id"]) < since:
                reached_since = True
            # Only the author carries across passes; see `_Obs`. Overwriting the
            # other two keeps an edit, or a component that mounted late, from
            # being masked by the first thing this row was ever seen to say.
            prior = seen.get(raw["id"])
            seen[raw["id"]] = _Obs(
                author_name=author or (prior.author_name if prior else None),
                content=_content(raw),
                attachments=raw["attachments"],
            )

        if reached_since:
            stop = StopReason.WINDOW_EDGE
            break
        if len(seen) >= limit:
            stop = StopReason.LIMIT
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

    messages = [_message(i, o, channel_id) for i, o in seen.items()]

    # A row read only in part still reports (see `_message`), so this names the
    # rows to look at rather than deciding anything — the report already carries
    # the gap to whoever reads it.
    degraded = {
        i: why
        for i, o in sorted(seen.items())
        if (why := o.degradation()) and _posted_at(i) >= since
    }
    if degraded:
        logger.warning(
            f"channel {channel_id}: {len(degraded)} row(s) in the window read"
            " only in part, reported with a placeholder:"
            f" {', '.join(f'{i} ({why})' for i, why in [*degraded.items()][:5])}"
        )

    return state, WindowRead(
        messages=sorted(messages, key=lambda m: m.timestamp, reverse=True)[:limit],
        stop=stop,
    )


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
