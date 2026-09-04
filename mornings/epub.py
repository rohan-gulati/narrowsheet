"""Bind cleaned posts into one EPUB that looks deliberate on e-ink."""

from __future__ import annotations

import functools
import io
import logging
import uuid
from datetime import date
from pathlib import Path

from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont

from .clean import CleanedPost

log = logging.getLogger(__name__)

AUTHOR = "Morning Read"

STYLESHEET = """/* Morning Read — hand-written for e-ink. No pixel font sizes. */

body {
  font-family: Georgia, "Bookerly", "Times New Roman", serif;
  line-height: 1.6;
  margin: 0 0.5em;
  text-align: left;
  widows: 2;
  orphans: 2;
}

h1, h2, h3, h4 {
  font-weight: normal;
  line-height: 1.25;
  margin: 1.4em 0 0.4em;
  page-break-after: avoid;
}

h1 { font-size: 1.5em; }
h2 { font-size: 1.25em; }
h3 { font-size: 1.1em; }
h4 { font-size: 1em; font-style: italic; }

/* Chapter headline: bigger and tighter than a plain h1, reads as a magazine
   feature title rather than a body heading. */
.headline {
  font-size: 1.7em;
  line-height: 1.15;
  letter-spacing: -0.01em;
  margin: 0 0 0.5em;
}

p {
  margin: 0 0 0.8em;
}

/* Pull-quote treatment: rules above/below and a larger size, not a left border. */
blockquote {
  margin: 1.8em 0;
  padding: 0.9em 0;
  border-top: 0.09em solid #111;
  border-bottom: 0.09em solid #111;
  font-size: 1.3em;
  line-height: 1.45;
  font-style: italic;
  text-align: center;
}

blockquote p:last-child { margin-bottom: 0; }

pre {
  font-family: "Courier New", monospace;
  font-size: 0.85em;
  line-height: 1.4;
  background: #f2f2f2;
  border-left: 0.18em solid #bbb;
  padding: 0.7em 0.8em;
  white-space: pre-wrap;
  word-wrap: break-word;
}

code {
  font-family: "Courier New", monospace;
  font-size: 0.9em;
}

pre code { font-size: 1em; }

ul, ol { margin: 0 0 0.9em 1.3em; padding: 0; }
li { margin-bottom: 0.35em; }

figure {
  margin: 1.5em 0;
  text-align: center;
  page-break-inside: avoid;
}

figcaption {
  font-size: 0.8em;
  font-style: italic;
  margin-top: 0.45em;
  text-align: center;
}

img { max-width: 100%; height: auto; }

hr {
  border: 0;
  border-top: 0.06em solid #999;
  margin: 1.8em 25%;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9em;
  margin: 1.3em 0;
}

th, td {
  border: 0.06em solid #999;
  padding: 0.35em 0.5em;
  text-align: left;
}

a {
  color: inherit;
  text-decoration: none;
  border-bottom: 0.06em solid #bbb;
}

/* Cover page */
.cover-date { font-size: 2em; margin: 0 0 0.1em; letter-spacing: 0.01em; }
.cover-day { font-size: 1.1em; font-style: italic; margin: 0 0 2em; }
.cover-heading {
  font-size: 0.8em;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  margin: 2em 0 0.8em;
}
.contents { list-style: none; margin: 0; padding: 0; }
.contents li { margin-bottom: 0.9em; }
.contents .pub { font-size: 0.78em; letter-spacing: 0.09em; text-transform: uppercase; }
.contents .title { display: block; font-size: 1.05em; }
.contents .mins { font-size: 0.8em; font-style: italic; }
/* Chapter header: kicker (publication) + dateline (issue no. / date / read time). */
.meta-line {
  margin: 0 0 0.5em;
  font-size: 0.78em;
  letter-spacing: 0.06em;
}
.kicker {
  text-transform: uppercase;
  font-weight: bold;
}
.dateline {
  text-transform: uppercase;
  color: #444;
}
.chapter-rule {
  border: 0;
  border-top: 0.14em solid #111;
  border-bottom: 0.02em solid #111;
  height: 0.22em;
  margin: 0 0 1.6em;
}

/* Drop cap on the article's opening paragraph, when it opens with one. CSS-only
   and safe: if the chapter opens with a blockquote or figure instead, this selector
   simply doesn't match and nothing breaks. */
.chapter-body > p:first-child::first-letter {
  float: left;
  font-size: 3.2em;
  line-height: 0.78;
  font-weight: normal;
  padding-right: 0.09em;
  padding-top: 0.03em;
}

/* Front page, grouped into sections by publication. */
.section-head {
  font-size: 0.85em;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-weight: normal;
  margin: 1.6em 0 0.5em;
  padding-bottom: 0.25em;
  border-bottom: 0.09em solid #111;
}

.cover-note { font-size: 0.82em; font-style: italic; margin-top: 2.5em; }
"""


# --------------------------------------------------------------------------------
# Cover image
#
# Kindle shows this in the library grid, so it has to read at thumbnail size: a few
# big shapes, high contrast, no fine detail. Greyscale to match the rest of the issue.
# --------------------------------------------------------------------------------

COVER_SIZE = (1400, 2100)  # 2:3, the ratio Kindle expects
COVER_INK = 17
COVER_MUTED = 105
COVER_PAPER = 255
COVER_INSET = 54
# Everything below this y is fixed, so every edition shares one skeleton.
LOWER_BLOCK_TOP = 1512

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/freefont",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts",
)

_SERIF = ("DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "FreeSerif.ttf", "Georgia.ttf")
_SERIF_BOLD = (
    "DejaVuSerif-Bold.ttf",
    "LiberationSerif-Bold.ttf",
    "FreeSerifBold.ttf",
    "Georgia Bold.ttf",
)
_SERIF_ITALIC = (
    "DejaVuSerif-Italic.ttf",
    "LiberationSerif-Italic.ttf",
    "FreeSerifItalic.ttf",
    "Georgia Italic.ttf",
)


@functools.lru_cache(maxsize=8)
def _find_font_file(names: tuple[str, ...]) -> str | None:
    for name in names:
        for directory in _FONT_DIRS:
            candidate = Path(directory) / name
            if candidate.exists():
                return str(candidate)
    return None


def _font(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    """Resolve a font, falling back to Pillow's own scalable face.

    A bare runner with no system fonts installed still gets a usable cover rather
    than a crashed run.
    """
    path = _find_font_file(names)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            log.debug("could not load %s; falling back", path)
    return ImageFont.load_default(size=size)


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    top_y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    tracking: float,
    fill: int,
) -> float:
    """Letterspaced, centred text drawn from its top edge. Returns the height drawn.

    Pillow has no tracking, so each glyph is placed individually -- but on a shared
    baseline, not a shared top. Pillow's "t" anchor is each glyph's own ink top, so
    anchoring per glyph floats short ones upward: an en dash between two runs of caps
    ends up level with their cap height, reading as a macron rather than a dash.
    """
    widths = [draw.textlength(char, font=font) for char in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x = center_x - total / 2
    # How far the whole string's ink top sits above the baseline.
    baseline_y = top_y - draw.textbbox((0, 0), text, font=font, anchor="ls")[1]
    for char, width in zip(text, widths, strict=True):
        draw.text((x, baseline_y), char, font=font, fill=fill, anchor="ls")
        x += width + tracking
    return _text_height(draw, text, font)


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    box = draw.textbbox((0, 0), text, font=font, anchor="lt")
    return box[3] - box[1]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    names: tuple[str, ...],
    size: int,
    max_width: int,
) -> ImageFont.FreeTypeFont:
    """Shrink until it fits, so a long weekday or big word count never overflows."""
    while size > 12:
        font = _font(names, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(names, size)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def build_cover_image(
    day: date,
    chapter_count: int,
    preview_count: int,
    total_words: int,
    publications: list[str],
    issue_number: int | None = None,
    covers_from: date | None = None,
) -> bytes:
    """Draw the edition cover.

    The layout is measured rather than positioned at fixed offsets: glyph heights vary
    enough between the preferred serif and the fallback face that hardcoded y-values
    collide (a 300px "18" happily overlaps the month above it).
    """
    width, height = COVER_SIZE
    canvas = Image.new("L", COVER_SIZE, COVER_PAPER)
    draw = ImageDraw.Draw(canvas)
    middle = width / 2
    max_text_width = width - 2 * (COVER_INSET + 18) - 80

    # Frame
    draw.rectangle(
        [COVER_INSET, COVER_INSET, width - COVER_INSET, height - COVER_INSET],
        outline=COVER_INK,
        width=4,
    )
    inner = COVER_INSET + 18
    draw.rectangle([inner, inner, width - inner, height - inner], outline=COVER_MUTED, width=1)

    # Masthead
    mast_font = _font(_SERIF, 50)
    mast_height = _draw_tracked(draw, middle, 205, "MORNING READ", mast_font, 17, COVER_INK)
    rule_y = 205 + mast_height + 34
    draw.line([(middle - 190, rule_y), (middle + 190, rule_y)], fill=COVER_INK, width=3)

    # Bottom block, laid out upward from the frame so it never runs into the border.
    listed = publications[:5]
    remaining = len(publications) - len(listed)
    if remaining > 0:
        listed.append(f"and {remaining} more")

    # Fixed positions, not bottom-anchored: these come out daily, and a masthead that
    # drifts by a few dozen pixels depending on how many publications ran looks wrong
    # when the editions sit next to each other in the library.
    pub_line_height = 58
    pubs_top = LOWER_BLOCK_TOP + 148

    summary = f"{_plural(chapter_count, 'post')} · {total_words:,} words"
    if preview_count:
        summary += f" · {_plural(preview_count, 'preview')}"
    summary_font = _fit_text(draw, summary, _SERIF, 46, max_text_width)
    summary_top = LOWER_BLOCK_TOP + 52
    draw.text((middle, summary_top), summary, font=summary_font, fill=COVER_INK, anchor="mt")

    lower_rule_y = LOWER_BLOCK_TOP
    draw.line(
        [(middle - 130, lower_rule_y), (middle + 130, lower_rule_y)], fill=COVER_MUTED, width=2
    )

    y = pubs_top
    for name in listed:
        name_font = _fit_text(draw, name, _SERIF, 42, max_text_width)
        draw.text((middle, y), name, font=name_font, fill=COVER_MUTED, anchor="mt")
        y += pub_line_height

    # Date block, centred in whatever space is left between the two rules. Issues are
    # numbered and cover a span of days, so the issue number is the dominant mark and
    # the span sits above it -- a single day numeral and a weekday would both be
    # wrong for something that collects a week or more of reading.
    span_text = format_range(covers_from or day, day)
    number_text = f"No. {issue_number:02d}" if issue_number else day.strftime("%d").lstrip("0")
    year_text = day.strftime("%Y")

    span_font = _fit_text(draw, span_text, _SERIF, 76, max_text_width - 200)
    # Tracked drawing adds width the fit check doesn't see, so leave room for it.
    number_font = _fit_text(draw, number_text, _SERIF_BOLD, 300, max_text_width - 120)
    year_font = _font(_SERIF, 74)

    gaps = (44, 34)
    heights = (
        _text_height(draw, span_text, span_font),
        _text_height(draw, number_text, number_font),
        _text_height(draw, year_text, year_font),
    )
    block_height = sum(heights) + sum(gaps)
    band_top = rule_y + 40
    band_bottom = lower_rule_y - 40
    y = band_top + max(0.0, (band_bottom - band_top - block_height) / 2)

    y += _draw_tracked(draw, middle, y, span_text, span_font, 13, COVER_INK) + gaps[0]
    draw.text((middle, y), number_text, font=number_font, fill=COVER_INK, anchor="mt")
    y += heights[1] + gaps[1]
    _draw_tracked(draw, middle, y, year_text, year_font, 12, COVER_MUTED)

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=88, optimize=True, progressive=True)
    return buffer.getvalue()


def issue_title(day: date, number: int | None = None) -> str:
    """ISO date first so the Kindle library sorts issues chronologically by title."""
    if number:
        return f"{day.isoformat()} — Morning Read No. {number}"
    return f"{day.isoformat()} — Morning Read"


def format_range(start: date, end: date) -> str:
    """The span an issue covers, as it reads on the cover: "AUG 21 - SEP 04".

    A single-day issue collapses to one date rather than repeating itself.
    """
    if start >= end:
        return end.strftime("%b %d").upper()
    return f"{start.strftime('%b %d').upper()} \u2013 {end.strftime('%b %d').upper()}"


def issue_range(chapters: list[CleanedPost], previews: list[CleanedPost], day: date) -> date:
    """The oldest post in the issue -- what the issue actually covers, not a nominal period."""
    dates = [c.post.published.date() for c in chapters + previews]
    return min(dates + [day])


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def order_chapters(
    posts: list[CleanedPost], max_words: int
) -> tuple[list[CleanedPost], int, bool]:
    """Chronological, unless the issue is oversized — then shortest-first.

    Nothing is ever dropped for length; a long issue just leads with the short reads.
    """
    total_words = sum(p.word_count for p in posts)
    oversized = total_words > max_words
    if oversized:
        ordered = sorted(posts, key=lambda p: p.word_count)
    else:
        ordered = sorted(posts, key=lambda p: p.post.published)
    return ordered, total_words, oversized


def _cover_html(
    day: date,
    chapters: list[CleanedPost],
    previews: list[CleanedPost],
    total_words: int,
    oversized: bool,
    issue_number: int | None = None,
    covers_from: date | None = None,
) -> str:
    heading = f"No. {issue_number:02d}" if issue_number else day.isoformat()
    parts = [
        f'<h1 class="cover-date">{heading}</h1>',
        f'<p class="cover-day">{format_range(covers_from or day, day)}</p>',
    ]

    if chapters:
        parts.append('<p class="cover-heading">In this issue</p>')
        # Grouped into sections by publication, in order of first appearance, so the
        # front page reads like a table of contents organised by section rather than
        # one flat list. The section heading carries the publication name, so the
        # per-item pub label from the old flat list is dropped as redundant.
        sections: dict[str, list[tuple[int, CleanedPost]]] = {}
        for index, chapter in enumerate(chapters, 1):
            sections.setdefault(chapter.post.publication, []).append((index, chapter))
        for pub_name, entries in sections.items():
            parts.append(f'<h2 class="section-head">{_escape(pub_name)}</h2>')
            parts.append('<ul class="contents">')
            for index, chapter in entries:
                parts.append(
                    "<li>"
                    f'<a class="title" href="chap_{index}.xhtml">'
                    f"{_escape(chapter.post.title)}</a>"
                    f'<span class="mins">{chapter.read_minutes} min</span>'
                    "</li>"
                )
            parts.append("</ul>")

    if previews:
        parts.append('<p class="cover-heading">Previews only</p>')
        parts.append('<ul class="contents">')
        for preview in previews:
            parts.append(
                "<li>"
                f'<span class="pub">{_escape(preview.post.publication)}</span>'
                f'<span class="title">{_escape(preview.post.title)}</span>'
                f'<span class="mins">truncated by the publisher</span>'
                "</li>"
            )
        parts.append("</ul>")

    if not chapters and not previews:
        parts.append("<p>Nothing new today.</p>")

    minutes = max(1, round(total_words / 200))
    note = f"{len(chapters)} posts · {total_words:,} words · about {minutes} min"
    if oversized:
        note += " · oversized issue, shortest reads first"
    parts.append(f'<p class="cover-note">{note}</p>')
    return "\n".join(parts)


def build_epub(
    posts: list[CleanedPost],
    previews: list[CleanedPost],
    day: date,
    max_words: int,
    out_path: Path,
    issue_number: int | None = None,
) -> Path:
    """Write the issue and return its path."""
    chapters, total_words, oversized = order_chapters(posts, max_words)
    covers_from = issue_range(chapters, previews, day)

    book = epub.EpubBook()
    book.set_identifier(f"morning-read-{day.isoformat()}-{uuid.uuid4().hex[:8]}")
    book.set_title(issue_title(day, issue_number))
    book.set_language("en")
    book.add_author(AUTHOR)

    style = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=STYLESHEET,
    )
    book.add_item(style)

    publications = list(dict.fromkeys(c.post.publication for c in chapters + previews))
    has_cover = False
    try:
        book.set_cover(
            "cover.jpg",
            build_cover_image(
                day,
                len(chapters),
                len(previews),
                total_words,
                publications,
                issue_number=issue_number,
                covers_from=covers_from,
            ),
        )
        # ebooklib marks its cover page linear="no", which EPUB 3.3 rejects unless
        # something hyperlinks to it. Making it the genuine first page is both valid
        # and what a reader expects when opening the issue.
        for item in book.items:
            if isinstance(item, epub.EpubCoverHtml):
                item.is_linear = True
        has_cover = True
    except Exception:
        # A cover is a nicety; never lose the issue over one.
        log.exception("cover generation failed; continuing without a cover image")

    contents = epub.EpubHtml(title="Contents", file_name="contents.xhtml", lang="en")
    contents.content = _cover_html(
        day, chapters, previews, total_words, oversized, issue_number, covers_from
    )
    contents.add_item(style)
    book.add_item(contents)

    spine: list[object] = (["cover"] if has_cover else []) + [contents, "nav"]
    toc: list[object] = [epub.Link("contents.xhtml", "Contents", "contents")]

    # Zero-padded to at least 2 digits so "No. 3" and "No. 12" line up; wider only
    # if the issue somehow runs past 99 chapters.
    number_width = max(2, len(str(len(chapters))))

    for index, chapter in enumerate(chapters, 1):
        item = epub.EpubHtml(title=chapter.title, file_name=f"chap_{index}.xhtml", lang="en")
        dateline = (
            f"No. {index:0{number_width}d} · "
            f"{chapter.post.published.strftime('%b %d').upper()} · "
            f"{chapter.read_minutes} MIN READ"
        )
        heading = (
            '<p class="meta-line">'
            f'<span class="kicker">{_escape(chapter.post.publication)}</span>'
            f' · <span class="dateline">{dateline}</span>'
            "</p>"
            f'<h1 class="headline">{_escape(chapter.post.title)}</h1>'
            '<hr class="chapter-rule"/>'
        )
        item.content = heading + f'<div class="chapter-body">\n{chapter.html}\n</div>'
        item.add_item(style)
        book.add_item(item)
        spine.append(item)
        toc.append(item)

        for image in chapter.images:
            book.add_item(
                epub.EpubImage(
                    uid=image.file_name.replace("/", "_").replace(".", "_"),
                    file_name=image.file_name,
                    media_type=image.media_type,
                    content=image.data,
                )
            )

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    log.info("wrote %s (%d chapters, %d words)", out_path, len(chapters), total_words)
    return out_path
