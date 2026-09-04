# Morning Read

Compiles your Substack subscriptions into a numbered EPUB issue, emails it to your
Kindle, and sends one push notification listing what's inside.

It runs every morning but does not send every morning: an issue goes out once enough
posts have piled up, so what lands on the device is a magazine rather than a two-post
daily.

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
| `mornings/feeds.py` | Resolves a link to a feed and edits `feeds.yaml` without losing its comments |
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
  lookback_hours: 720         # a month of unread, not a day; see below
  min_posts_per_issue: 10     # hold the issue until this many have piled up
  max_hold_days: 14           # ...but never go quiet for longer than this
  max_words_per_issue: 45000  # a soft cap; see below
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

## Managing subscriptions

You do not need to edit `feeds.yaml` by hand.

**From GitHub (works on the phone app):** open a new issue, pick the *Manage a
subscription* template, choose add / remove / pause / resume, and paste a link. An
Action resolves the feed, applies the change, commits it, replies with what it found,
and closes the issue.

Any link works when adding — the homepage, a post you liked, or the feed itself. The
publication's name comes from the feed rather than the URL, which is how
`nickcollins1953` ends up as "Maritime Trade History". Before saving, it reports how
often the publication posts and what share of recent posts arrive paywalled, so you
find out up front when something will mostly show as previews.

Only the repository owner can drive that workflow — this repo is public, so the job is
gated on the issue author.

**From the terminal:**

```bash
python -m mornings feeds add https://www.noahpinion.blog/p/some-post
python -m mornings feeds pause "Lenny's Newsletter"
python -m mornings feeds resume "Lenny's Newsletter"
python -m mornings feeds remove "Seymour Hersh"
python -m mornings feeds list
```

Pausing keeps the entry and its filters in the file and skips it at fetch time, so you
can mute a publication without losing its `exclude_titles` and `min_words` config.

Edits go through a round-trip YAML loader, so the comments in `feeds.yaml` survive.

## Schedule

`0 0 * * *` — 00:00 UTC, which is 05:30 IST. The job wakes daily but only *sends* when
`min_posts_per_issue` have accumulated, so the cadence comes from the threshold rather
than the cron. Waking daily is what lets it ship the morning the pile is big enough.

`workflow_dispatch` runs it on demand, with a **Force** checkbox that sends whatever is
waiting regardless of the threshold.

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
| `run --force` | Send now, even if fewer than `min_posts_per_issue` are waiting |
| `preview <url>` | Clean a single post and dump the HTML, to debug the sanitizer against one publication |

`--dry-run` validates the EPUB with epubcheck when it can find one, via
`EPUBCHECK_JAR` or `epubcheck` on `PATH`. A missing checker is skipped, not an error.

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest
```

## Behaviour worth knowing

**When an issue is sent.** Not every run. After cleaning, if fewer than
`min_posts_per_issue` posts are waiting, the run **holds**: it builds nothing, sends
nothing, notifies nobody and writes no state. Because `record_sent` only runs after a
confirmed send, the same posts are selected again next morning and the pile grows.

That is the whole cadence mechanism. Seventeen days of daily sending produced a median
issue of **two posts**; a threshold of 10 turns the same volume into an issue every four
or five days that is worth opening. Raise it for a slower magazine, drop it to `1` to go
back to sending every morning.

The counterweight is `max_hold_days`. Once the oldest waiting post has been held that
long, the issue ships short rather than leaving a post to rot in a quiet fortnight.

The gate counts posts *after* cleaning, because `exclude_titles` and `min_words` drop
posts during the clean — counting before it would ship an issue of six that turns into
three.

**What gets into an issue.** Two filters, in `select_recent` (`mornings/fetch.py`):
a post must be published inside `lookback_hours`, and its GUID must not already be
in `state/seen.json`.

This is a queue of *unread*, not of *recent*. The window is a month rather than a day
because most publications post weekly or slower — a 26-hour window only ever caught
4 of 13 of them, and anything falling outside it was missed permanently, since the
window moves on and `seen.json` only prevents duplicates, it never recovers a miss.
Widening the window cannot cause re-sends, because `seen.json` still does the
deduping; it only stops posts being dropped on the floor.

**`lookback_hours` must outlast `max_hold_days`.** A held post is not stored anywhere —
it is re-selected from the feed on each run. If the window closes before the issue
ships, the post is dropped rather than delayed, silently. `load_config` warns when the
two are set inconsistently.

**Issues are numbered.** The counter lives in `state/seen.json` alongside the GUIDs, so
the commit the Action already makes carries it. It shows up in the EPUB title
(`2026-09-04 — Morning Read No. 7`), on the generated cover, on the contents page, and
in the push notification. The cover shows the span the issue covers — `AUG 21 – SEP 04`
— taken from the oldest post in it, rather than a single day numeral.

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

`state/seen.json` maps post GUIDs to the date they were sent, and carries
`issue_number` and `last_issue_date`. It is committed back to the repo by the Action
after a successful send. Entries older than 60 days are pruned. To re-send something,
remove its GUID; to renumber, edit `issue_number`.
