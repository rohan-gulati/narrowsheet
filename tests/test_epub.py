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
