"""Reduce Substack post HTML to clean, e-ink-friendly XHTML.

The sanitizer is an allowlist. Three tiers, because "drop everything else and unwrap
children" is wrong for icon widgets: unwrapping an <svg> strips the wrapper but leaves
its <path> guts behind as stray nodes.
"""

from __future__ import annotations

import concurrent.futures
import io
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

from .fetch import Post

log = logging.getLogger(__name__)

KEEP_TAGS = frozenset(
    {
        "h1", "h2", "h3", "h4", "p", "blockquote", "ul", "ol", "li", "pre", "code",
        "img", "a", "em", "strong", "hr", "figure", "figcaption",
        # A kept <table> is meaningless without its row/cell structure.
        "table", "thead", "tbody", "tr", "th", "td",
    }
)

# Removed with their subtree. <picture> is deliberately absent: it wraps the <img>
# we want, so it gets unwrapped instead.
DROP_WITH_CONTENTS = frozenset(
    {
        "script", "style", "svg", "button", "form", "input", "textarea", "select",
        "iframe", "video", "audio", "source", "noscript", "object", "embed", "canvas",
        "map", "area", "link", "meta",
    }
)

# Attributes survive only where they carry meaning. This one rule kills inline styles,
# class names, width/height, srcset, data-*, align, bgcolor and <font> attributes.
ALLOWED_ATTRS = {"a": frozenset({"href"}), "img": frozenset({"src", "alt"})}

CHROME_CLASS_PATTERNS = (
    "subscribe", "subscription-widget", "button-wrapper", "footnote", "poll",
    "comments", "share", "restack", "image-link-expand", "icon-container", "paywall",
    "unsubscribe", "email-footer", "preamble", "digest-", "post-ufi", "like-button",
    "embedded-post",
)

CHROME_LINK_TEXT = (
    "subscribe now", "read on substack", "view on substack", "view this post on the web",
    "leave a comment", "post a comment", "share this post", "view comments",
    "upgrade to paid", "upgrade your subscription", "unsubscribe",
    "manage your subscription", "give a gift subscription", "refer a friend",
)

# Matched as prefixes, for CTAs that trail a publication name.
CHROME_LINK_PREFIXES = ("get more from", "share ", "restack ")

CHROME_HREF_PATTERNS = (
    "/subscribe", "/account", "action=unsubscribe", "open.substack.com/o/",
    "substack.com/app-link", "/comments", "utm_campaign=post_embed",
    "utm_campaign=email-half-magic-comments",
)

PAYWALL_TEXT = (
    "this post is for paid subscribers",
    "this post is for paying subscribers",
    "this post is for free subscribers",
    "keep reading with a 7-day free trial",
    "subscribe to keep reading",
    "this is a preview of a paid post",
)

READ_MORE_TEXT = ("read more", "continue reading", "read full story", "view entire episode")

TRACKING_SRC_PATTERNS = ("open.substack.com/o/", "/open?token=", "px.substack.com", "/pixel")

# Substack article containers, used when preview fetches a whole web page.
ARTICLE_SELECTORS = ("div.available-content", "div.body.markup", "article", "div.post-content")

MIN_IMAGE_PX = 100
MAX_IMAGE_EDGE = 1200
JPEG_QUALITY = 80
IMAGE_TIMEOUT = 20
WORDS_PER_MINUTE = 200


@dataclass
class EmbeddedImage:
    file_name: str
    data: bytes
    media_type: str = "image/jpeg"


@dataclass
class CleanedPost:
    post: Post
    html: str
    word_count: int
    truncated: bool
    images: list[EmbeddedImage] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.post.publication} — {self.post.title}"

    @property
    def read_minutes(self) -> int:
        return max(1, round(self.word_count / WORDS_PER_MINUTE))


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _alive(tag: Tag) -> bool:
    return not getattr(tag, "decomposed", False) and tag.parent is not None


def _select_article(soup: BeautifulSoup) -> Tag:
    """When handed a full web page, narrow to the article body before cleaning."""
    for selector in ARTICLE_SELECTORS:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 400:
            return found
    return soup.body or soup


def _drop_tags(root: Tag) -> None:
    for tag in root.find_all(list(DROP_WITH_CONTENTS)):
        if _alive(tag):
            tag.decompose()


def _drop_chrome_by_class(root: Tag) -> None:
    for tag in root.find_all(True):
        if not _alive(tag):
            continue
        classes = " ".join(tag.get("class") or [])
        marker = f"{classes} {tag.get('id') or ''}".lower()
        if marker.strip() and any(p in marker for p in CHROME_CLASS_PATTERNS):
            tag.decompose()


def _is_truncated(root: Tag) -> bool:
    """Paid feeds end the partial body with a bare 'Read more' link."""
    tail = _norm(root.get_text())[-120:]
    return any(tail.endswith(phrase) for phrase in READ_MORE_TEXT)


def _drop_empty_ancestors(tag: Tag) -> None:
    parent = tag.parent
    tag.decompose()
    while parent is not None and parent.name not in ("body", "[document]"):
        if not _alive(parent):
            return
        if parent.find("img") or _norm(parent.get_text()):
            return
        grandparent = parent.parent
        parent.decompose()
        parent = grandparent


def _drop_chrome_by_text(root: Tag) -> None:
    for anchor in root.find_all("a"):
        if not _alive(anchor):
            continue
        if anchor.find("img"):
            continue
        text = _norm(anchor.get_text())
        href = (anchor.get("href") or "").lower()
        hits_text = (
            text in CHROME_LINK_TEXT
            or text in READ_MORE_TEXT
            or any(text.startswith(prefix) for prefix in CHROME_LINK_PREFIXES)
        )
        hits_href = any(p in href for p in CHROME_HREF_PATTERNS)
        if not (hits_text or (hits_href and len(text) < 60)):
            continue
        block = anchor.find_parent(["p", "div", "li", "td", "h1", "h2", "h3", "h4"])
        standalone = block is not None and _norm(block.get_text()) == text
        if standalone or hits_text:
            _drop_empty_ancestors(anchor)
        else:
            # An inline link like "you can <a>subscribe</a>." — losing the anchor is
            # right, losing the word breaks the sentence.
            anchor.unwrap()

    for tag in root.find_all(["p", "div", "h1", "h2", "h3", "h4", "blockquote"]):
        if not _alive(tag):
            continue
        text = _norm(tag.get_text())
        if text and any(text.startswith(phrase) for phrase in PAYWALL_TEXT):
            tag.decompose()


def _strip_attributes(root: Tag) -> None:
    for tag in root.find_all(True):
        if not _alive(tag):
            continue
        allowed = ALLOWED_ATTRS.get(tag.name, frozenset())
        for name in list(tag.attrs):
            if name not in allowed:
                del tag[name]


def _unwrap_to_allowlist(root: Tag) -> None:
    for tag in root.find_all(True):
        if not _alive(tag) or tag.name in KEEP_TAGS:
            continue
        if tag.name == "br":
            # br is not in the keep list, but bare unwrapping would weld the words on
            # either side together. A newline keeps them separate.
            tag.replace_with(NavigableString("\n"))
            continue
        if tag.name in ("html", "body", "head"):
            continue
        tag.unwrap()

    # An anchor wrapping only an image is Substack's lightbox link; the image is enough.
    for anchor in root.find_all("a"):
        if _alive(anchor) and anchor.find("img") and not _norm(anchor.get_text()):
            anchor.unwrap()


def _upscale_hint_to_1200(src: str) -> str:
    """Ask Substack's CDN for a 1200px render instead of downloading its 1456px one."""
    if "substackcdn.com" in src:
        return re.sub(r"(?<=[,/])w_\d+", f"w_{MAX_IMAGE_EDGE}", src)
    return src


def _collect_images(root: Tag, base_url: str) -> list[tuple[Tag, str]]:
    targets: list[tuple[Tag, str]] = []
    for img in root.find_all("img"):
        if not _alive(img):
            continue
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        if any(p in src for p in TRACKING_SRC_PATTERNS):
            img.decompose()
            continue
        # Declared dimensions catch tracking pixels and emoji before we spend a request.
        too_small = False
        for attr in ("width", "height"):
            try:
                if int(str(img.get(attr) or "").strip()) < MIN_IMAGE_PX:
                    too_small = True
            except (TypeError, ValueError):
                continue
        if too_small:
            img.decompose()
            continue
        targets.append((img, _upscale_hint_to_1200(urljoin(base_url, src))))
    return targets


def _render_jpeg(raw: bytes) -> bytes | None:
    with Image.open(io.BytesIO(raw)) as opened:
        opened.load()
        if min(opened.size) < MIN_IMAGE_PX:
            return None
        # Flatten transparency onto white first. Converting a transparent PNG straight
        # to "L" renders the clear areas black, which looks awful on e-ink.
        if opened.mode in ("P", "PA", "LA", "RGBA") or "transparency" in opened.info:
            backing = Image.new("RGBA", opened.size, (255, 255, 255, 255))
            image = Image.alpha_composite(backing, opened.convert("RGBA"))
        else:
            image = opened
        grey = image.convert("L")
        grey.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        buffer = io.BytesIO()
        grey.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        return buffer.getvalue()


def _process_images(root: Tag, base_url: str, index_offset: int) -> list[EmbeddedImage]:
    targets = _collect_images(root, base_url)
    if not targets:
        return []

    unique_urls = list(dict.fromkeys(url for _, url in targets))

    def download(url: str) -> tuple[str, bytes | None]:
        try:
            with httpx.Client(timeout=IMAGE_TIMEOUT, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
            return url, _render_jpeg(response.content)
        except Exception as exc:
            log.warning("image dropped (%s): %s", type(exc).__name__, url[:120])
            return url, None

    rendered: dict[str, bytes] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for url, data in pool.map(download, unique_urls):
            if data:
                rendered[url] = data

    images: list[EmbeddedImage] = []
    file_names: dict[str, str] = {}
    for img, url in targets:
        if not _alive(img):
            continue
        data = rendered.get(url)
        if data is None:
            img.decompose()
            continue
        if url not in file_names:
            file_names[url] = f"images/img_{index_offset + len(file_names) + 1}.jpg"
            images.append(EmbeddedImage(file_name=file_names[url], data=data))
        img["src"] = file_names[url]
        img["alt"] = img.get("alt") or ""
    return images


def clean_html(
    html: str,
    *,
    base_url: str = "",
    with_images: bool = True,
    image_index_offset: int = 0,
) -> tuple[str, bool, list[EmbeddedImage]]:
    """Return (xhtml_fragment, truncated, images)."""
    soup = BeautifulSoup(html or "", "lxml")
    root = _select_article(soup)

    _drop_tags(root)
    _drop_chrome_by_class(root)
    # Detect before removing the stub: the 'Read more' link is the only signal we get.
    truncated = _is_truncated(root)
    _drop_chrome_by_text(root)

    images = _process_images(root, base_url, image_index_offset) if with_images else []
    if not with_images:
        _collect_images(root, base_url)

    _strip_attributes(root)
    _unwrap_to_allowlist(root)

    fragment = root.decode_contents(formatter="minimal")
    fragment = re.sub(r"\n{3,}", "\n\n", fragment).strip()
    return fragment, truncated, images


def count_words(fragment: str) -> int:
    text = BeautifulSoup(fragment, "lxml").get_text(" ")
    return len(text.split())


def clean_post(post: Post, *, with_images: bool = True, image_index_offset: int = 0) -> CleanedPost:
    fragment, truncated, images = clean_html(
        post.html,
        base_url=post.link,
        with_images=with_images,
        image_index_offset=image_index_offset,
    )
    return CleanedPost(
        post=post,
        html=fragment,
        word_count=count_words(fragment),
        truncated=truncated,
        images=images,
    )
