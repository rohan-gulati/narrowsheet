"""Pull posts from configured sources. One bad source must never kill the run."""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import feedparser
import httpx

from .config import Config, Publication, Settings, resolve_feed_url

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
USER_AGENT = "mornings/0.1 (+https://github.com/rohan-gulati/narrowsheet)"
MAX_WORKERS = 8


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
        posts.append(
            Post(
                guid=guid,
                publication=pub.name,
                title=(getattr(entry, "title", "") or "Untitled").strip(),
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


def fetch_all(config: Config) -> list[Post]:
    """Fetch every feed-backed publication concurrently."""
    feed_pubs = [p for p in config.publications if p.type in ("public", "private")]
    email_pubs = [p for p in config.publications if p.type == "email"]
    if email_pubs:
        log.info(
            "%d email publication(s) configured; IMAP delivery is not wired up yet",
            len(email_pubs),
        )

    posts: list[Post] = []
    if not feed_pubs:
        return posts
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(_fetch_one, feed_pubs):
            posts.extend(result)
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
