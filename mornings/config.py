"""Configuration loading, plus the secret redaction that keeps feed tokens out of logs."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SOURCE_TYPES = ("public", "private", "email")

# Substack private feeds look like https://<pub>.substack.com/feed/private/<token>.
_PRIVATE_FEED_RE = re.compile(r"(/feed/private/)[^/\s\"'<>]+")

# Values registered here are scrubbed from every log record and exception message.
_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mark a value as sensitive so it is never rendered into a log line."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Replace private-feed tokens and registered secrets with ``***``."""
    out = _PRIVATE_FEED_RE.sub(r"\1***", text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(secret, "***")
    return out


class RedactingFormatter(logging.Formatter):
    """Redact the fully rendered record, tracebacks included.

    Doing this at the formatter rather than at each call site is deliberate: a token
    embedded in a URL can surface from inside httpx or imaplib, where we never touch
    the message ourselves.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Publication:
    name: str
    type: str
    url: str | None = None
    url_env: str | None = None
    match_from: str | None = None
    # Pausing keeps the entry and its filters in the file instead of deleting it.
    enabled: bool = True
    # Case-insensitive substrings; a post whose title contains any of them is skipped.
    # Deliberately substrings and not regexes: the most useful pattern here is " | "
    # (Substack's guest-interview title convention), and as a regex the pipe is an
    # alternation metacharacter that would quietly match every space instead.
    exclude_titles: tuple[str, ...] = ()
    # Posts whose cleaned body falls under this are skipped entirely. Catches
    # podcast show-note stubs, whose real content is audio the sanitizer strips.
    min_words: int | None = None


@dataclass(frozen=True)
class Settings:
    lookback_hours: int = 26
    max_words_per_issue: int = 25000
    skip_if_empty: bool = True
    imap_label: str = "kindle"
    # An issue is only sent once this many posts have piled up. Below it the run
    # holds: nothing is built, sent or recorded, so the same posts are picked up
    # again next time and the pile grows. A daily cron with a threshold gives a
    # magazine rhythm without pinning it to a fixed publishing day.
    min_posts_per_issue: int = 1
    # ...unless the oldest waiting post has been held this long, at which point the
    # issue goes out short rather than leaving a post to rot in the queue.
    max_hold_days: int = 14


@dataclass(frozen=True)
class Config:
    publications: list[Publication]
    settings: Settings


def _publication(raw: dict[str, Any], index: int) -> Publication:
    name = raw.get("name")
    if not name:
        raise ValueError(f"publications[{index}]: 'name' is required")
    kind = raw.get("type")
    if kind not in SOURCE_TYPES:
        raise ValueError(f"{name}: 'type' must be one of {SOURCE_TYPES}, got {kind!r}")
    if kind == "public" and not raw.get("url"):
        raise ValueError(f"{name}: public publications need a 'url'")
    if kind == "private" and not raw.get("url_env"):
        raise ValueError(f"{name}: private publications need a 'url_env', never an inline url")
    if kind == "private" and raw.get("url"):
        raise ValueError(f"{name}: private feed URLs are credentials; use 'url_env' instead")
    if kind == "email" and not raw.get("match_from"):
        raise ValueError(f"{name}: email publications need a 'match_from' sender pattern")

    raw_excludes = raw.get("exclude_titles") or []
    if isinstance(raw_excludes, str):
        raise ValueError(f"{name}: 'exclude_titles' must be a list, not a single string")
    excludes = tuple(str(pattern) for pattern in raw_excludes if str(pattern).strip())

    min_words = raw.get("min_words")
    if min_words is not None:
        try:
            min_words = int(min_words)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: 'min_words' must be a whole number") from exc
        if min_words < 0:
            raise ValueError(f"{name}: 'min_words' cannot be negative")

    return Publication(
        name=name,
        type=kind,
        enabled=bool(raw.get("enabled", True)),
        url=raw.get("url"),
        url_env=raw.get("url_env"),
        match_from=raw.get("match_from"),
        exclude_titles=excludes,
        min_words=min_words,
    )


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_pubs = data.get("publications") or []
    if not isinstance(raw_pubs, list):
        raise ValueError("'publications' must be a list")
    publications = [_publication(p, i) for i, p in enumerate(raw_pubs)]

    raw_settings = data.get("settings") or {}
    known = {f: raw_settings[f] for f in Settings.__dataclass_fields__ if f in raw_settings}
    settings = Settings(**known)
    _check_hold_window(settings)
    return Config(publications=publications, settings=settings)


def _check_hold_window(settings: Settings) -> None:
    """Warn when a held post would age out of the lookback window before it ships.

    select_recent drops anything published before now - lookback_hours, and
    state/seen.json only prevents duplicates -- it never recovers a miss. So a post
    held longer than the window is lost silently, and only shows up as an absence
    weeks later. The window has to outlast the hold ceiling.
    """
    if settings.min_posts_per_issue <= 1:
        return
    needed = settings.max_hold_days * 24
    if settings.lookback_hours < needed:
        logging.getLogger(__name__).warning(
            "lookback_hours is %d but posts can be held for %d days (%dh); posts held "
            "longer than the window will be dropped, not delayed. Raise lookback_hours "
            "to at least %d.",
            settings.lookback_hours,
            settings.max_hold_days,
            needed,
            needed,
        )


def resolve_feed_url(pub: Publication) -> str | None:
    """Return the fetchable URL for a feed publication, registering private ones as secrets."""
    if pub.type == "public":
        return pub.url
    if pub.type == "private":
        url = os.environ.get(pub.url_env or "")
        if not url:
            logging.getLogger(__name__).warning(
                "%s: env var %s is unset; skipping this publication", pub.name, pub.url_env
            )
            return None
        register_secret(url)
        token = url.rsplit("/feed/private/", 1)[-1] if "/feed/private/" in url else None
        register_secret(token)
        return url
    return None
