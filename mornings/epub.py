"""Bind cleaned posts into one EPUB that looks deliberate on e-ink."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from ebooklib import epub

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

p {
  margin: 0 0 0.8em;
}

blockquote {
  margin: 1.3em 0 1.3em 0.5em;
  padding-left: 0.9em;
  border-left: 0.18em solid #888;
  font-style: italic;
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
.byline {
  font-size: 0.78em;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  margin: -0.2em 0 2em;
}

.cover-note { font-size: 0.82em; font-style: italic; margin-top: 2.5em; }
"""


def issue_title(day: date) -> str:
    """ISO date first so the Kindle library sorts issues chronologically by title."""
    return f"{day.isoformat()} — Morning Read"


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
) -> str:
    parts = [
        f'<h1 class="cover-date">{day.isoformat()}</h1>',
        f'<p class="cover-day">{day.strftime("%A")}</p>',
    ]

    if chapters:
        parts.append('<p class="cover-heading">In this issue</p>')
        parts.append('<ul class="contents">')
        for index, chapter in enumerate(chapters, 1):
            parts.append(
                "<li>"
                f'<span class="pub">{_escape(chapter.post.publication)}</span>'
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
) -> Path:
    """Write the issue and return its path."""
    chapters, total_words, oversized = order_chapters(posts, max_words)

    book = epub.EpubBook()
    book.set_identifier(f"morning-read-{day.isoformat()}-{uuid.uuid4().hex[:8]}")
    book.set_title(issue_title(day))
    book.set_language("en")
    book.add_author(AUTHOR)

    style = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=STYLESHEET,
    )
    book.add_item(style)

    cover = epub.EpubHtml(title="Contents", file_name="cover.xhtml", lang="en")
    cover.content = _cover_html(day, chapters, previews, total_words, oversized)
    cover.add_item(style)
    book.add_item(cover)

    spine: list[object] = [cover, "nav"]
    toc: list[object] = [epub.Link("cover.xhtml", "Contents", "contents")]

    for index, chapter in enumerate(chapters, 1):
        item = epub.EpubHtml(title=chapter.title, file_name=f"chap_{index}.xhtml", lang="en")
        heading = (
            f'<h1>{_escape(chapter.post.title)}</h1>'
            f'<p class="byline">{_escape(chapter.post.publication)}</p>'
        )
        item.content = heading + "\n" + chapter.html
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
