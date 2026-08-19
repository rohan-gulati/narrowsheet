"""Pull posts from configured sources. One bad source must never kill the run."""

from __future__ import annotations

import concurrent.futures
import contextlib
import email
import email.utils
import imaplib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message

import feedparser
import httpx

from .config import Config, Publication, Settings, register_secret, resolve_feed_url

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
USER_AGENT = "mornings/0.1 (+https://github.com/rohan-gulati/narrowsheet)"
MAX_WORKERS = 8
IMAP_HOST = "imap.gmail.com"


@dataclass
class Post:
    guid: str
    publication: str
    title: str
    published: datetime
    html: str
    link: str


def _entry_html(entry: object) -> str:
    """Substack puts the full post in content[]; summary is the fallback."""
    content = getattr(entry, "content", None)
    if content:
        best = max(content, key=lambda c: len(c.get("value") or ""))
        if best.get("value"):
            return best["value"]
    return getattr(entry, "summary", "") or ""


def _entry_published(entry: object) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


def excluded_title_pattern(title: str, patterns: tuple[str, ...]) -> str | None:
    """Return the pattern that excludes this title, or None.

    Plain case-insensitive substring matching, not regex — see Publication.exclude_titles.
    """
    lowered = title.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            return pattern
    return None


def _fetch_feed(pub: Publication, url: str, client: httpx.Client) -> list[Post]:
    response = client.get(url)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    posts: list[Post] = []
    for entry in parsed.entries:
        published = _entry_published(entry)
        if published is None:
            log.debug("%s: entry %r has no date; skipping", pub.name, getattr(entry, "title", "?"))
            continue
        link = getattr(entry, "link", "") or ""
        guid = getattr(entry, "id", "") or link
        if not guid:
            continue
        title = (getattr(entry, "title", "") or "Untitled").strip()
        matched = excluded_title_pattern(title, pub.exclude_titles)
        if matched:
            log.info("%s: skipped %r (title matches %r)", pub.name, title[:70], matched)
            continue
        posts.append(
            Post(
                guid=guid,
                publication=pub.name,
                title=title,
                published=published,
                html=_entry_html(entry),
                link=link,
            )
        )
    return posts


def _fetch_one(pub: Publication) -> list[Post]:
    url = resolve_feed_url(pub)
    if not url:
        return []
    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            posts = _fetch_feed(pub, url, client)
    except Exception:
        # Logged through the redacting formatter, so a private feed token cannot leak
        # out of an httpx exception message.
        log.exception("%s: fetch failed; continuing without it", pub.name)
        return []
    log.info("%s: %d entries", pub.name, len(posts))
    return posts


# --------------------------------------------------------------------------------
# IMAP source, for publications that only arrive by email
# --------------------------------------------------------------------------------


def _decoded(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_part(message: Message) -> str:
    """Pull the HTML body out of a multipart message, falling back to plain text."""
    html = ""
    plain = ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html" and not html:
            html = text
        elif part.get_content_type() == "text/plain" and not plain:
            plain = text
    if html:
        return html
    # A text-only newsletter still beats nothing; wrap it so the sanitizer sees blocks.
    return "".join(f"<p>{line}</p>" for line in plain.splitlines() if line.strip())


def _matches(sender: str, publication: Publication) -> bool:
    pattern = (publication.match_from or "").lower().strip()
    return bool(pattern) and pattern in sender.lower()


def _post_from_message(raw: bytes, publications: list[Publication]) -> Post | None:
    message = email.message_from_bytes(raw)
    sender = _decoded(message.get("From"))
    publication = next((p for p in publications if _matches(sender, p)), None)
    if publication is None:
        return None

    try:
        published = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        published = None
    if published is None:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)

    html = _html_part(message)
    if not html.strip():
        return None

    guid = message.get("Message-ID") or f"{publication.name}:{published.isoformat()}"
    return Post(
        guid=guid.strip(),
        publication=publication.name,
        title=_decoded(message.get("Subject")) or "Untitled",
        published=published.astimezone(UTC),
        html=html,
        link="",
    )


def fetch_email_posts(publications: list[Publication], settings: Settings) -> list[Post]:
    """Read a Gmail label over IMAP and match senders to publications.

    One connection serves every email publication, so this runs sequentially rather
    than joining the feed thread pool.
    """
    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_PASS")
    if not (user and password):
        log.warning("IMAP_USER/IMAP_PASS are unset; skipping %d email publication(s)",
                    len(publications))
        return []
    register_secret(password)

    since = (datetime.now(UTC) - timedelta(hours=settings.lookback_hours)).strftime("%d-%b-%Y")
    posts: list[Post] = []
    connection: imaplib.IMAP4_SSL | None = None
    try:
        connection = imaplib.IMAP4_SSL(IMAP_HOST, timeout=TIMEOUT_SECONDS)
        connection.login(user, password)
        status, _ = connection.select(f'"{settings.imap_label}"', readonly=True)
        if status != "OK":
            log.error("IMAP label %r could not be opened", settings.imap_label)
            return []

        status, data = connection.search(None, "SINCE", since)
        if status != "OK":
            log.error("IMAP search failed for label %r", settings.imap_label)
            return []

        message_ids = data[0].split()
        log.info("IMAP: %d message(s) in %r since %s", len(message_ids),
                 settings.imap_label, since)
        for message_id in message_ids:
            try:
                status, payload = connection.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                post = _post_from_message(payload[0][1], publications)
            except Exception:
                log.exception("could not read one message; continuing")
                continue
            if post:
                posts.append(post)
    except Exception:
        # Logged through the redacting formatter so the app password cannot leak.
        log.exception("IMAP fetch failed; continuing without email publications")
        return posts
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
            with contextlib.suppress(Exception):
                connection.logout()

    log.info("IMAP: matched %d post(s) to configured publications", len(posts))
    return posts


def fetch_all(config: Config) -> list[Post]:
    """Fetch every configured source. A failing source is logged, never fatal."""
    feed_pubs = [p for p in config.publications if p.type in ("public", "private")]
    email_pubs = [p for p in config.publications if p.type == "email"]

    posts: list[Post] = []
    if feed_pubs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for result in pool.map(_fetch_one, feed_pubs):
                posts.extend(result)
    if email_pubs:
        posts.extend(fetch_email_posts(email_pubs, config.settings))
    return posts


def select_recent(
    posts: list[Post],
    settings: Settings,
    seen: set[str],
    now: datetime | None = None,
) -> list[Post]:
    """Keep posts inside the lookback window that we have not already sent."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=settings.lookback_hours)
    kept: list[Post] = []
    for post in posts:
        if post.published < cutoff:
            continue
        if post.guid in seen:
            log.debug("already sent: %s", post.title)
            continue
        kept.append(post)
    kept.sort(key=lambda p: p.published)
    return kept


def fetch_single(url: str) -> Post:
    """Fetch one post page directly, for `preview`."""
    with httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    return Post(
        guid=url,
        publication="preview",
        title=url,
        published=datetime.now(UTC),
        html=response.text,
        link=url,
    )
