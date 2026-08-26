"""Resolve a Substack link to a feed, and edit feeds.yaml without wrecking it.

feeds.yaml is hand-written and carries a lot of explanatory comments. PyYAML would
delete every one of them on a dump, so edits go through ruamel's round-trip loader.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .clean import clean_html

log = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; mornings/0.1)"

_RSS_LINK = re.compile(
    r"""<link[^>]+type=["']application/rss\+xml["'][^>]*>""", re.IGNORECASE
)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


class FeedError(RuntimeError):
    """Raised when a URL cannot be resolved to a usable feed."""


def _yaml() -> YAML:
    # sequence=4/offset=2 matches the existing file: "- name:" at column 2, its keys
    # at column 4. The defaults reindent every entry and produce a noisy diff.
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


@dataclass
class FeedReport:
    """What we learned about a feed before writing it into the config."""

    name: str
    feed_url: str
    entries: int
    newest: datetime | None
    posts_last_30d: int
    truncated: int
    sampled: int
    warnings: list[str] = field(default_factory=list)

    @property
    def truncated_share(self) -> str:
        if not self.sampled:
            return "unknown"
        return f"{self.truncated}/{self.sampled}"

    def summary_lines(self) -> list[str]:
        newest = self.newest.strftime("%Y-%m-%d") if self.newest else "unknown"
        lines = [
            f"**{self.name}**",
            f"- feed: `{self.feed_url}`",
            f"- {self.entries} entries, newest {newest}",
            f"- {self.posts_last_30d} posts in the last 30 days",
            f"- {self.truncated_share} of recent posts arrive truncated (paywalled)",
        ]
        lines += [f"- ⚠️ {w}" for w in self.warnings]
        return lines


def _get(url: str, client: httpx.Client) -> httpx.Response:
    response = client.get(url)
    response.raise_for_status()
    return response


def _parse_feed(content: bytes) -> feedparser.FeedParserDict | None:
    parsed = feedparser.parse(content)
    if parsed.entries and (parsed.feed.get("title") or "").strip():
        return parsed
    return None


def resolve_feed(url: str, client: httpx.Client | None = None) -> tuple[str, object]:
    """Return (feed_url, parsed_feed) for any publication, post, or feed URL.

    Order matters, and each step is here because a real URL shape needs it:
      1. The URL may already be a feed. Appending /feed to it yields /feed/feed.
      2. RSS autodiscovery in the page <head>. Works for homepages, post URLs and
         custom domains alike, and returns a relative href to join against the origin.
      3. <origin>/feed, which covers non-Substack blogs whose pages omit the link tag.
    """
    owns_client = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        try:
            response = _get(url, client)
        except Exception as exc:
            raise FeedError(f"could not fetch {url} ({type(exc).__name__})") from exc

        direct = _parse_feed(response.content)
        if direct is not None:
            return str(response.url), direct

        match = _RSS_LINK.search(response.text)
        if match:
            href = _HREF.search(match.group(0))
            if href:
                candidate = urljoin(str(response.url), href.group(1))
                try:
                    parsed = _parse_feed(_get(candidate, client).content)
                    if parsed is not None:
                        return candidate, parsed
                except Exception:
                    log.debug("autodiscovered feed %s did not parse", candidate)

        origin = urlparse(str(response.url))
        fallback = f"{origin.scheme}://{origin.netloc}/feed"
        try:
            parsed = _parse_feed(_get(fallback, client).content)
            if parsed is not None:
                return fallback, parsed
        except Exception:
            log.debug("fallback feed %s did not parse", fallback)

        raise FeedError(f"no usable RSS feed found for {url}")
    finally:
        if owns_client:
            client.close()


def _entry_html(entry: object) -> str:
    content = getattr(entry, "content", None)
    if content:
        best = max(content, key=lambda c: len(c.get("value") or ""))
        if best.get("value"):
            return best["value"]
    return getattr(entry, "summary", "") or ""


def inspect_feed(url: str, sample: int = 10) -> FeedReport:
    """Resolve a URL and report what the feed actually looks like."""
    feed_url, parsed = resolve_feed(url)
    entries = list(parsed.entries)

    dates: list[datetime] = []
    for entry in entries:
        for attr in ("published_parsed", "updated_parsed"):
            value = getattr(entry, attr, None)
            if value:
                dates.append(datetime(*value[:6], tzinfo=UTC))
                break

    cutoff = datetime.now(UTC) - timedelta(days=30)
    truncated = 0
    sampled = 0
    for entry in entries[:sample]:
        html = _entry_html(entry)
        if not html:
            continue
        sampled += 1
        # Reuse the pipeline's own detection rather than re-deriving it here.
        _, is_truncated, _ = clean_html(html, with_images=False)
        truncated += bool(is_truncated)

    report = FeedReport(
        name=(parsed.feed.get("title") or "Untitled").strip(),
        feed_url=feed_url,
        entries=len(entries),
        newest=max(dates) if dates else None,
        posts_last_30d=sum(1 for d in dates if d >= cutoff),
        truncated=truncated,
        sampled=sampled,
    )
    if report.sampled and report.truncated / report.sampled >= 0.5:
        report.warnings.append(
            "Most posts are paywalled for non-subscribers, so they will show as "
            "previews on the cover rather than full chapters. Add a private feed "
            "URL if you subscribe."
        )
    if report.posts_last_30d == 0:
        report.warnings.append("Nothing published in the last 30 days.")
    return report


# --------------------------------------------------------------------- editing


def _load(path: Path) -> tuple[object, YAML]:
    yaml = _yaml()
    return yaml.load(path.read_text(encoding="utf-8")), yaml


def _save(path: Path, data: object, yaml: YAML) -> None:
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    text = buffer.getvalue()
    # Appending to the publications list swallows the blank line that separated it
    # from the settings block. Put it back so the file keeps its shape.
    text = re.sub(r"\n(?<!\n\n)settings:", "\n\nsettings:", text, count=1)
    path.write_text(text, encoding="utf-8")


def _publications(data: object) -> list:
    pubs = data.get("publications")
    if pubs is None:
        raise FeedError("feeds.yaml has no 'publications' list")
    return pubs


def find_publication(data: object, name: str) -> dict | None:
    """Match on name, case-insensitively, so the issue form can be forgiving."""
    wanted = name.strip().casefold()
    for entry in _publications(data):
        if str(entry.get("name", "")).strip().casefold() == wanted:
            return entry
    return None


def add_publication(path: Path, url: str, name: str | None = None) -> tuple[str, FeedReport]:
    report = inspect_feed(url)
    publication_name = (name or report.name).strip()

    data, yaml = _load(path)
    if find_publication(data, publication_name) is not None:
        return f"**{publication_name}** is already in the list. Nothing changed.", report

    for entry in _publications(data):
        if str(entry.get("url", "")).strip() == report.feed_url:
            return (
                f"That feed is already in the list as **{entry.get('name')}**. "
                "Nothing changed."
            ), report

    from ruamel.yaml.comments import CommentedMap

    # Quoted to match every other entry in the hand-written file.
    new = CommentedMap()
    new["name"] = DoubleQuotedScalarString(publication_name)
    new["type"] = "public"
    new["url"] = DoubleQuotedScalarString(report.feed_url)
    _publications(data).append(new)
    _save(path, data, yaml)
    return f"Added **{publication_name}**.", report


def remove_publication(path: Path, name: str) -> str:
    data, yaml = _load(path)
    entry = find_publication(data, name)
    if entry is None:
        return f"No publication called **{name}**. Nothing changed."
    pubs = _publications(data)
    pubs.remove(entry)
    _save(path, data, yaml)
    return f"Removed **{entry.get('name')}**."


def set_enabled(path: Path, name: str, enabled: bool) -> str:
    """Pause or resume without discarding the entry's filters."""
    data, yaml = _load(path)
    entry = find_publication(data, name)
    if entry is None:
        return f"No publication called **{name}**. Nothing changed."

    if enabled:
        entry.pop("enabled", None)
    elif "enabled" in entry:
        entry["enabled"] = False
    else:
        # Insert next to the name rather than appending. A trailing blank line is
        # attached to the entry's last key, so appending would put "enabled" after
        # that blank and leave a gap in the middle of the block.
        entry.insert(1, "enabled", False)
    _save(path, data, yaml)
    verb = "Resumed" if enabled else "Paused"
    return f"{verb} **{entry.get('name')}**."


def list_publications(path: Path) -> str:
    data, _ = _load(path)
    lines = []
    for entry in _publications(data):
        state = "" if entry.get("enabled", True) else "  (paused)"
        lines.append(f"- {entry.get('name')} [{entry.get('type')}]{state}")
    return "\n".join(lines) or "No publications configured."


# ----------------------------------------------------------------- issue forms

VALID_ACTIONS = ("add", "remove", "pause", "resume")


def parse_issue_form(body: str) -> tuple[str, str]:
    """Pull (action, target) out of a GitHub issue-form body.

    Issue forms render as "### <label>" followed by the value, and an empty optional
    field renders as the literal "_No response_".
    """
    fields: dict[str, str] = {}
    for section in re.split(r"^###\s+", body or "", flags=re.MULTILINE):
        if not section.strip():
            continue
        label, _, value = section.partition("\n")
        fields[label.strip().casefold()] = value.strip()

    action = fields.get("action", "").casefold()
    target = next(
        (v for k, v in fields.items() if k.startswith("substack link")), ""
    ).strip()

    if action not in VALID_ACTIONS:
        raise FeedError(f"unrecognised action: {action or '(none)'}")
    if not target or target == "_No response_":
        raise FeedError("no link or publication name was given")
    return action, target


def apply_issue(path: Path, body: str) -> str:
    """Run whatever an issue form asked for, returning markdown for the reply."""
    action, target = parse_issue_form(body)
    if action == "add":
        message, report = add_publication(path, target)
        return message + "\n\n" + "\n".join(report.summary_lines())
    if action == "remove":
        return remove_publication(path, target)
    return set_enabled(path, target, enabled=action == "resume")
