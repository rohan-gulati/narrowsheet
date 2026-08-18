"""Tests for the HTML sanitizer.

Three fixtures are real Substack output captured from live feeds; the fourth is a
hand-written reproduction of Substack's *email* markup, whose unsubscribe footers and
tracking pixels never appear in RSS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from mornings.clean import clean_html, count_words

FIXTURES = Path(__file__).parent / "fixtures"

REAL_FIXTURES = ("acx_post.html", "pragmatic_truncated.html", "stratechery_post.html")
ALL_FIXTURES = (*REAL_FIXTURES, "substack_email.html")

# Images are never fetched in tests; the network is not a test dependency.
CLEAN_KWARGS = {"with_images": False}


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def clean(name: str) -> tuple[str, bool]:
    fragment, truncated, _ = clean_html(load(name), **CLEAN_KWARGS)
    return fragment, truncated


def soup_of(fragment: str) -> BeautifulSoup:
    return BeautifulSoup(fragment, "lxml")


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_disallowed_tags_survive(name: str) -> None:
    fragment, _ = clean(name)
    tags = {t.name for t in soup_of(fragment).find_all(True)} - {"html", "body"}
    allowed = {
        "h1", "h2", "h3", "h4", "p", "blockquote", "ul", "ol", "li", "pre", "code",
        "img", "a", "em", "strong", "hr", "figure", "figcaption",
        "table", "thead", "tbody", "tr", "th", "td",
    }
    assert tags <= allowed, f"{name} kept disallowed tags: {sorted(tags - allowed)}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_chrome_widgets_are_gone(name: str) -> None:
    fragment, _ = clean(name)
    lowered = fragment.lower()
    for widget in ("<button", "<svg", "<form", "<input", "<source", "<script", "<style"):
        assert widget not in lowered, f"{name} still contains {widget}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_all_presentational_attributes_are_stripped(name: str) -> None:
    fragment, _ = clean(name)
    for tag in soup_of(fragment).find_all(True):
        if tag.name in ("html", "body"):
            continue
        allowed = {"a": {"href"}, "img": {"src", "alt"}}.get(tag.name, set())
        leftover = set(tag.attrs) - allowed
        assert not leftover, f"{name}: <{tag.name}> kept {sorted(leftover)}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_presentational_attributes_in_raw_markup(name: str) -> None:
    """Belt-and-braces scan of the serialized markup.

    Matched attribute-shaped only: prose legitimately contains strings like
    "data-driven decision-making", which a bare substring check would flag.
    """
    fragment, _ = clean(name)
    pattern = re.compile(
        r"""\s(?:class|style|width|height|srcset|align|bgcolor|face|data-[\w-]+)\s*=""",
        re.IGNORECASE,
    )
    found = pattern.search(fragment)
    assert found is None, f"{name} still has {found.group(0).strip() if found else ''}"


@pytest.mark.parametrize("name", REAL_FIXTURES)
def test_body_survives_intact(name: str) -> None:
    """The point of the sanitizer is what it keeps, not only what it removes."""
    fragment, _ = clean(name)
    words = count_words(fragment)
    original_words = count_words(load(name))
    assert words > 300, f"{name} kept only {words} words"
    # Chrome is a small fraction of a real post; losing half the words means the
    # sanitizer is eating body copy.
    assert words > original_words * 0.5, f"{name}: {original_words} -> {words} words"
    assert len(soup_of(fragment).find_all("p")) > 5


def test_truncated_paid_post_is_detected() -> None:
    _, truncated = clean("pragmatic_truncated.html")
    assert truncated is True


@pytest.mark.parametrize("name", ("acx_post.html", "stratechery_post.html"))
def test_full_posts_are_not_flagged_as_truncated(name: str) -> None:
    _, truncated = clean(name)
    assert truncated is False


def test_read_more_stub_is_removed() -> None:
    fragment, _ = clean("pragmatic_truncated.html")
    assert "read more" not in fragment.lower()


def test_footnotes_are_dropped() -> None:
    fragment, _ = clean("acx_post.html")
    assert "footnote" not in fragment.lower()


def test_email_ctas_and_footer_are_gone() -> None:
    fragment, _ = clean("substack_email.html")
    lowered = fragment.lower()
    for phrase in (
        "subscribe now",
        "unsubscribe",
        "manage your subscription",
        "leave a comment",
        "share this post",
        "read on substack",
        "you're receiving this because",
    ):
        assert phrase not in lowered, f"email chrome survived: {phrase}"


def test_email_tracking_pixel_is_gone() -> None:
    fragment, _ = clean("substack_email.html")
    assert "open?token=" not in fragment
    assert "open.substack.com/o/" not in fragment


def test_email_body_survives() -> None:
    fragment, _ = clean("substack_email.html")
    for kept in (
        "This first paragraph is genuine body copy",
        "A second real paragraph",
        "A quotation that belongs in the finished chapter",
        "Figure caption that should be kept",
        "The closing paragraph of the actual article",
    ):
        assert kept in fragment, f"body copy lost: {kept}"
    markup = soup_of(fragment)
    assert markup.find("strong") is not None
    assert markup.find("em") is not None
    assert markup.find("blockquote") is not None
    assert markup.find("figcaption") is not None
    # The ordinary outbound link stays; only chrome links are pulled.
    assert markup.find("a", href="https://example.com/reference") is not None


def test_lightbox_anchor_is_unwrapped_but_image_kept() -> None:
    fragment, _ = clean("substack_email.html")
    markup = soup_of(fragment)
    image = markup.find("img")
    assert image is not None, "the figure image was dropped"
    assert image.find_parent("a") is None, "image is still wrapped in a lightbox link"


def test_br_does_not_weld_words_together() -> None:
    """<br> is not in the keep list, but dropping it outright would join the words."""
    fragment, _, _ = clean_html("<p>first line<br>second line</p>", **CLEAN_KWARGS)
    text = soup_of(fragment).get_text()
    assert re.search(r"first line\s+second line", text), repr(text)


def test_empty_input_is_safe() -> None:
    fragment, truncated, images = clean_html("", **CLEAN_KWARGS)
    assert fragment == ""
    assert truncated is False
    assert images == []


def test_embedded_post_card_is_removed() -> None:
    """Substack embeds other posts as a card: logo, title, excerpt, 'Read more', likes."""
    fragment, _ = clean("pragmatic_truncated.html")
    assert "embedded-post" not in fragment
    fragment_charity, _, _ = clean_html(
        '<p>Real body text.</p>'
        '<div class="embedded-post-wrap">'
        '<a class="embedded-post" href="https://charity.wtf/p/x?utm_campaign=post_embed">'
        '<div class="embedded-post-title">Some Other Post</div>'
        '<div class="embedded-post-body">An excerpt that trails off…Read more</div>'
        "</a></div>",
        **CLEAN_KWARGS,
    )
    assert "Some Other Post" not in fragment_charity
    assert "Read more" not in fragment_charity
    assert "Real body text." in fragment_charity


def test_inline_subscribe_link_keeps_its_words() -> None:
    """Regression: matching bare 'subscribe' used to delete a word mid-sentence."""
    fragment, _, _ = clean_html(
        '<p>Most content is free, some is subscriber only; you can '
        '<a href="https://www.astralcodexten.com/subscribe">subscribe</a>. Also:</p>',
        **CLEAN_KWARGS,
    )
    text = soup_of(fragment).get_text()
    assert "you can subscribe" in text, text
    assert " ." not in text, f"dangling punctuation left behind: {text!r}"
    assert soup_of(fragment).find("a") is None, "the subscribe link itself should go"


def test_standalone_cta_block_is_removed_entirely() -> None:
    fragment, _, _ = clean_html(
        "<p>Body copy worth keeping.</p>"
        '<p><a href="https://example.substack.com/subscribe">Subscribe now</a></p>',
        **CLEAN_KWARGS,
    )
    assert "Subscribe now" not in fragment
    assert "Body copy worth keeping." in fragment
    assert soup_of(fragment).find_all("p") == soup_of(fragment).find_all("p")[:1]
