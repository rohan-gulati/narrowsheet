# Morning Read

Compiles your Substack subscriptions into a single daily EPUB, emails it to your
Kindle, and sends one push notification listing what's inside.

Runs on the GitHub Actions free tier. No Calibre, no paid services — the EPUB is
built directly with `ebooklib`.

```
fetch  →  filter  →  clean  →  build EPUB  →  send  →  notify  →  record state
```

Nothing is marked as seen until a send is confirmed, so a failed run retries the
same posts the next morning rather than losing them.

## Layout

| File | Does |
| --- | --- |
| `mornings/config.py` | Loads `feeds.yaml`; resolves private feed URLs from env; redacts secrets from logs |
| `mornings/fetch.py` | RSS (public + private) concurrently, and the IMAP email fallback |
| `mornings/clean.py` | Strips Substack chrome, embeds greyscale images, detects truncated posts |
| `mornings/epub.py` | Cover image, contents page, chapters, NCX + nav, stylesheet |
| `mornings/deliver.py` | SMTP send with retries, ntfy notification, `state/seen.json` |
| `mornings/__main__.py` | CLI |

## Setup

### 1. Configure your publications

Edit `feeds.yaml`. Three kinds of source:

```yaml
publications:
  # Public RSS, readable by anyone
  - name: "Stratechery"
    type: public
    url: "https://stratechery.com/feed/"

  # Paid publication with a per-subscriber feed. The URL is a credential and
  # lives in an env var, never in this file.
  - name: "Example Paid"
    type: private
    url_env: "FEED_EXAMPLE_PAID"

  # Publication that only arrives by email
  - name: "Email Only"
    type: email
    match_from: "@examplepaid.substack.com"

  # Any publication can filter its own posts.
  - name: "Noisy Publication"
    type: public
    url: "https://example.com/feed"
    exclude_titles:        # case-insensitive substrings, NOT regexes
      - "🎙"
      - " | "
    min_words: 700         # drop posts whose cleaned body is shorter than this

settings:
  lookback_hours: 168         # a week of unread, not a day; see below
  max_words_per_issue: 25000  # a soft cap; see below
  skip_if_empty: true
  imap_label: "kindle"
```

A private feed URL placed inline under `url:` is rejected at load time — it would
be a credential committed to the repo.

### 2. Add repository secrets

`Settings → Secrets and variables → Actions`:

| Secret | For |
| --- | --- |
| `KINDLE_EMAIL` | Your `@kindle.com` address |
| `SMTP_USER` | Gmail address the issue is sent from |
| `SMTP_PASS` | Gmail **app password**, not your account password |
| `IMAP_USER` / `IMAP_PASS` | Only if you use `type: email` sources |
| `NTFY_TOPIC` | Your ntfy.sh topic. Treat it as secret: anyone who knows it can post to it |
| `FEED_*` | One per private feed, matching its `url_env` |

Then add a line per private feed to the `env:` block in
`.github/workflows/morning-read.yml` — GitHub cannot enumerate secrets for you:

```yaml
FEED_EXAMPLE_PAID: ${{ secrets.FEED_EXAMPLE_PAID }}
```

### 3. Approve the sender

In Amazon's **Manage Your Content and Devices → Preferences → Personal Document
Settings**, add your `SMTP_USER` address to the approved sender list. Amazon
silently drops mail from unapproved senders.

### 4. For email sources

Create a Gmail filter that applies the label `kindle` to the publications you
want, and generate an app password for IMAP. The label name is configurable via
`settings.imap_label`.

## Schedule

`0 0 * * *` — 00:00 UTC, which is 05:30 IST. Also runs on `workflow_dispatch` for
manual runs. Edit the cron in `.github/workflows/morning-read.yml` to move it.

## Local use

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python -m mornings run --dry-run                      # build only
.venv/bin/python -m mornings run --dry-run --lookback-hours 720 # a fuller issue
.venv/bin/python -m mornings preview https://example.substack.com/p/some-post
```

| Command | Does |
| --- | --- |
| `run` | The full pipeline: fetch, build, send, notify, record state |
| `run --dry-run` | Fetch and build into `./out`. Sends nothing, writes no state |
| `run --lookback-hours N` | Widen the window. Useful for producing a fat issue while working on the CSS |
| `preview <url>` | Clean a single post and dump the HTML, to debug the sanitizer against one publication |

`--dry-run` validates the EPUB with epubcheck when it can find one, via
`EPUBCHECK_JAR` or `epubcheck` on `PATH`. A missing checker is skipped, not an error.

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest
```

## Behaviour worth knowing

**What gets into an issue.** Two filters, in `select_recent` (`mornings/fetch.py`):
a post must be published inside `lookback_hours`, and its GUID must not already be
in `state/seen.json`.

This is a queue of *unread*, not of *recent*. The window is a week rather than a day
because most publications post weekly or slower — a 26-hour window only ever caught
4 of 13 of them, and anything falling outside it was missed permanently, since the
window moves on and `seen.json` only prevents duplicates, it never recovers a miss.
Widening the window cannot cause re-sends, because `seen.json` still does the
deduping; it only stops posts being dropped on the floor. Widen it further if you
want a longer catch-up tail.

**Per-publication filters.** `exclude_titles` drops posts whose title contains any
of the listed substrings; `min_words` drops posts whose cleaned body is too short.
Both are per-publication, and useful for newsletters that mix long-form writing with
podcast show notes — the audio is stripped by the sanitizer, so those posts otherwise
arrive as near-empty chapters.

`exclude_titles` entries are **case-insensitive substrings, not regexes**. That is
deliberate: the most useful pattern is `" | "`, Substack's guest-interview title
convention, and as a regex the pipe is an alternation operator that would match
every space and drop everything.

`min_words` deliberately does not apply to truncated posts. Those are short because
the publisher paywalled the feed, not because they're filler, and they are already
handled by being listed on the cover as a preview instead of becoming a chapter.

**Truncated paid posts.** Substack's paid feeds send a partial body ending in a
"Read more" stub. Those are detected, kept out of the chapter list, and listed on
the cover page as previews — a chapter that is only a stub is worse than no
chapter. They are still marked as seen, so they don't re-list every morning.

**Oversized issues.** `max_words_per_issue` never drops anything. Past the cap,
chapters are reordered shortest-first and the total is noted on the cover.

**Images** are downloaded, flattened onto white, converted to greyscale, capped at
1200px on the long edge and embedded as JPEG. Anything under 100px is dropped as a
tracking pixel or emoji. A failed image is dropped; it never fails the post.

**Footnotes are stripped** as chrome, along with subscribe CTAs, share and comment
widgets, embedded post cards, unsubscribe footers and paywall teasers.

**`<br>` is not in the keep list**, per the sanitizer's tag allowlist, so hard line
breaks become whitespace and verse reflows as prose. Add `br` to `KEEP_TAGS` in
`mornings/clean.py` if you read a lot of poetry.

**A failing source never kills the run.** It is logged and skipped, and the rest of
the issue goes out.

**Secrets never reach the logs.** Redaction is installed as a logging formatter
rather than applied at call sites, so a feed token embedded in a URL cannot escape
through a traceback raised inside `httpx` or `imaplib`.

## State

`state/seen.json` maps post GUIDs to the date they were sent, and is committed back
to the repo by the Action after a successful send. Entries older than 60 days are
pruned. To re-send something, remove its GUID.
