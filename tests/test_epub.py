"""Tests for issue assembly: chapter ordering and the generated cover."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from mornings.clean import CleanedPost
from mornings.epub import (
    COVER_SIZE,
    build_cover_image,
    build_epub,
    issue_title,
    order_chapters,
)
from mornings.fetch import Post

PUBS = ["Astral Codex Ten", "Stratechery"]


def make_post(title: str, words: int, publication: str = "Astral Codex Ten",
              minutes_ago: int = 0) -> CleanedPost:
    body = " ".join(["word"] * words)
    post = Post(
        guid=f"guid-{title}",
        publication=publication,
        title=title,
        published=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        html=f"<p>{body}</p>",
        link="https://example.com/p/x",
    )
    return CleanedPost(post=post, html=f"<p>{body}</p>", word_count=words, truncated=False)


def open_cover(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_issue_title_sorts_chronologically_by_title() -> None:
    titles = [issue_title(date(2026, 8, 9)), issue_title(date(2026, 8, 18))]
    assert sorted(titles) == titles


def test_cover_is_greyscale_jpeg_at_kindle_ratio() -> None:
    image = open_cover(build_cover_image(date(2026, 8, 18), 31, 6, 68562, PUBS))
    assert image.size == COVER_SIZE
    assert image.mode == "L"
    assert image.format == "JPEG"
    assert abs(COVER_SIZE[1] / COVER_SIZE[0] - 1.5) < 0.01


def test_cover_pluralises_counts_of_one() -> None:
    """A cover reading '1 posts' would be visible on the device every such day."""
    from mornings.epub import _plural

    assert _plural(1, "post") == "1 post"
    assert _plural(0, "post") == "0 posts"
    assert _plural(2, "preview") == "2 previews"


@pytest.mark.parametrize(
    "day,publications",
    [
        (date(2026, 9, 2), ["Stratechery"]),
        (date(2026, 12, 31), [f"Publication {n}" for n in range(9)]),
        (date(2027, 1, 6), ["A Publication With A Very Long Name Indeed"]),
    ],
)
def test_cover_survives_awkward_inputs(day: date, publications: list[str]) -> None:
    image = open_cover(build_cover_image(day, 1, 0, 940, publications))
    assert image.size == COVER_SIZE
    # Something was actually drawn: a blank page would be uniformly white.
    assert image.convert("L").getextrema()[0] < 60


def test_cover_layout_is_identical_across_editions() -> None:
    """The lower block is fixed so editions line up beside each other in the library."""
    few = open_cover(build_cover_image(date(2026, 8, 18), 3, 0, 5000, PUBS[:1]))
    many = open_cover(build_cover_image(date(2026, 8, 19), 30, 5, 60000, PUBS * 4))

    def first_ink_row(image: Image.Image, top: int, bottom: int) -> int:
        pixels = image.load()
        for y in range(top, bottom):
            if any(pixels[x, y] < 128 for x in range(500, 900)):
                return y
        return -1

    # The summary rule sits at the same height regardless of publication count.
    assert first_ink_row(few, 1480, 1600) == first_ink_row(many, 1480, 1600)


def test_epub_embeds_the_cover_and_validates_structurally(tmp_path: Path) -> None:
    chapters = [make_post("One", 400), make_post("Two", 900, "Stratechery")]
    out = tmp_path / "issue.epub"
    build_epub(chapters, [], date(2026, 8, 18), 25000, out)

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert "EPUB/cover.jpg" in names
        assert "EPUB/toc.ncx" in names, "Kindle needs the NCX"
        assert "EPUB/nav.xhtml" in names
        opf = archive.read("EPUB/content.opf").decode()
        assert 'properties="cover-image"' in opf
        assert 'media-type="image/jpeg"' in opf
        contents = archive.read("EPUB/contents.xhtml").decode()
        assert "One" in contents and "Two" in contents


def test_issue_is_still_built_when_cover_generation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cover is a nicety; it must never cost us the issue."""
    import mornings.epub as epub_module

    def boom(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("no fonts, no framebuffer, no luck")

    monkeypatch.setattr(epub_module, "build_cover_image", boom)
    out = tmp_path / "issue.epub"
    build_epub([make_post("One", 400)], [], date(2026, 8, 18), 25000, out)

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert "EPUB/cover.jpg" not in names
        assert "EPUB/chap_1.xhtml" in names


def test_oversized_issue_keeps_everything_but_leads_with_short_reads() -> None:
    posts = [make_post("Long", 9000, minutes_ago=30), make_post("Short", 200, minutes_ago=10)]
    ordered, total, oversized = order_chapters(posts, max_words=1000)
    assert oversized is True
    assert total == 9200
    assert [c.post.title for c in ordered] == ["Short", "Long"], "shortest first"
    assert len(ordered) == 2, "nothing may be dropped for length"


def test_normal_issue_stays_chronological() -> None:
    posts = [make_post("Older", 9000, minutes_ago=90), make_post("Newer", 200, minutes_ago=5)]
    ordered, _, oversized = order_chapters(posts, max_words=25000)
    assert oversized is False
    assert [c.post.title for c in ordered] == ["Older", "Newer"]


def test_chapter_header_includes_kicker_dateline_and_read_time(tmp_path: Path) -> None:
    """The masthead-style header replaced the old plain h1 + uppercase byline."""
    out = tmp_path / "issue.epub"
    build_epub(
        [make_post("A Long Enough Title", 600, "Astral Codex Ten", minutes_ago=120)],
        [],
        date(2026, 8, 18),
        25000,
        out,
    )
    with zipfile.ZipFile(out) as archive:
        chapter = archive.read("EPUB/chap_1.xhtml").decode()

    assert 'class="kicker"' in chapter
    assert 'class="dateline"' in chapter
    assert "Astral Codex Ten" in chapter
    assert "No. 01" in chapter, "single-chapter issue should still zero-pad to 2 digits"
    assert "MIN READ" in chapter
    assert 'class="headline"' in chapter
    assert 'class="chapter-rule"' in chapter
    assert 'class="chapter-body"' in chapter, "needed so the drop-cap selector can scope to it"
    assert "byline" not in chapter, "the old plain byline paragraph should be gone"


def test_chapter_numbering_zero_pads_to_the_issue_size(tmp_path: Path) -> None:
    posts = [make_post(f"Post {n}", 300, minutes_ago=n) for n in range(12)]
    out = tmp_path / "issue.epub"
    build_epub(posts, [], date(2026, 8, 18), 25000, out)
    with zipfile.ZipFile(out) as archive:
        first = archive.read("EPUB/chap_1.xhtml").decode()
        twelfth = archive.read("EPUB/chap_12.xhtml").decode()
    assert "No. 01" in first
    assert "No. 12" in twelfth


def test_front_page_groups_entries_by_publication_into_sections(tmp_path: Path) -> None:
    posts = [
        make_post("ACX One", 400, "Astral Codex Ten", minutes_ago=200),
        make_post("Strat One", 400, "Stratechery", minutes_ago=150),
        make_post("ACX Two", 400, "Astral Codex Ten", minutes_ago=100),
    ]
    out = tmp_path / "issue.epub"
    build_epub(posts, [], date(2026, 8, 18), 25000, out)
    with zipfile.ZipFile(out) as archive:
        contents = archive.read("EPUB/contents.xhtml").decode()

    assert contents.count('class="section-head"') == 2, "one heading per publication"
    assert "<h2 class=\"section-head\">Astral Codex Ten</h2>" in contents
    assert "<h2 class=\"section-head\">Stratechery</h2>" in contents
    # Both Astral Codex Ten entries should be grouped together under its one heading,
    # not interleaved with Stratechery's, and the section owns the pub name now so
    # per-item pub labels are gone.
    acx_section = contents.split('Astral Codex Ten</h2>')[1].split("</ul>")[0]
    assert "ACX One" in acx_section and "ACX Two" in acx_section
    assert 'class="pub"' not in contents, "redundant once grouped under a section heading"


def test_front_page_section_order_follows_first_appearance(tmp_path: Path) -> None:
    """Sections should not silently re-sort into alphabetical or some other order."""
    posts = [
        make_post("Z First", 400, "Zzz Publication", minutes_ago=200),
        make_post("A Second", 400, "Aaa Publication", minutes_ago=100),
    ]
    out = tmp_path / "issue.epub"
    build_epub(posts, [], date(2026, 8, 18), 25000, out)
    with zipfile.ZipFile(out) as archive:
        contents = archive.read("EPUB/contents.xhtml").decode()
    assert contents.index("Zzz Publication") < contents.index("Aaa Publication")


def test_stylesheet_declares_the_new_magazine_classes() -> None:
    from mornings.epub import STYLESHEET

    for selector in (
        ".kicker",
        ".dateline",
        ".chapter-rule",
        ".headline",
        ".section-head",
        ".chapter-body",
        "::first-letter",
    ):
        assert selector in STYLESHEET, f"missing {selector} in the stylesheet"
    assert "text-align: center" in STYLESHEET, "blockquote should read as a centred pull quote"
