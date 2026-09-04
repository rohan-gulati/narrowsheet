"""Command line entry point: `python -m mornings ...`."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from .clean import CleanedPost, clean_post
from .config import Settings, load_config, setup_logging
from .deliver import (
    DeliveryConfig,
    DeliveryError,
    load_issue_state,
    load_seen,
    notify_failure,
    notify_success,
    record_sent,
    send_issue,
)
from .epub import build_epub, issue_title
from .feeds import (
    FeedError,
    add_publication,
    apply_issue,
    list_publications,
    remove_publication,
    set_enabled,
)
from .fetch import fetch_all, fetch_single, select_recent

log = logging.getLogger("mornings")

DEFAULT_CONFIG = "feeds.yaml"
DEFAULT_STATE = "state/seen.json"
DEFAULT_OUT = "out"


def run_epubcheck(epub_path: Path) -> None:
    """Validate if epubcheck is reachable; a missing checker is not an error."""
    jar = os.environ.get("EPUBCHECK_JAR")
    if shutil.which("epubcheck"):
        command = ["epubcheck", str(epub_path)]
    elif jar and Path(jar).exists() and shutil.which("java"):
        command = ["java", "-jar", jar, str(epub_path)]
    else:
        log.info("epubcheck not found; skipping validation (set EPUBCHECK_JAR to enable)")
        return
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        log.info("epubcheck: valid")
    else:
        log.warning("epubcheck reported problems:\n%s", (result.stdout or result.stderr)[-4000:])


def _run_pipeline(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    settings = config.settings
    if args.lookback_hours:
        settings = replace(settings, lookback_hours=args.lookback_hours)
    today = date.today()

    posts = fetch_all(config)
    log.info("fetched %d posts across %d publications", len(posts), len(config.publications))

    seen = load_seen(args.state)
    fresh = select_recent(posts, settings, seen, now=datetime.now(UTC))
    log.info("%d posts inside the %dh window", len(fresh), settings.lookback_hours)

    if not fresh and settings.skip_if_empty:
        log.info("nothing new; skipping this issue")
        return 0

    min_words_by_pub = {
        p.name: p.min_words for p in config.publications if p.min_words is not None
    }

    cleaned = []
    image_offset = 0
    for post in fresh:
        result = clean_post(post, image_index_offset=image_offset)
        image_offset += len(result.images)
        # Applied after cleaning, since the word count is not known until the chrome
        # is stripped. Scoped to non-truncated posts on purpose: the floor exists to
        # drop podcast show-note stubs, whereas a truncated post is short only because
        # the publisher paywalled the feed, and those are already handled by being
        # listed on the cover as a preview instead of becoming a chapter.
        floor = min_words_by_pub.get(post.publication)
        if floor is not None and not result.truncated and result.word_count < floor:
            log.info(
                "skipped %s — %s (%d words, under the %d-word floor)",
                post.publication,
                post.title,
                result.word_count,
                floor,
            )
            continue
        cleaned.append(result)
        state = "preview" if result.truncated else f"{result.word_count} words"
        log.info("cleaned %s — %s (%s)", post.publication, post.title, state)

    if not (args.dry_run or args.force) and _should_hold(cleaned, settings, today):
        return 0

    chapters = [c for c in cleaned if not c.truncated]
    previews = [c for c in cleaned if c.truncated]

    issue_number = load_issue_state(args.state).number + 1

    out_path = Path(args.out) / f"{today.isoformat()}-morning-read.epub"
    build_epub(chapters, previews, today, settings.max_words_per_issue, out_path, issue_number)

    if args.dry_run:
        run_epubcheck(out_path)
        log.info("dry run: wrote %s, sent nothing, wrote no state", out_path)
        return 0

    delivery: DeliveryConfig | None = None
    try:
        delivery = DeliveryConfig.from_env(dict(os.environ))
        send_issue(out_path, issue_title(today, issue_number), delivery)
    except DeliveryError as exc:
        # Nothing is marked seen, so tomorrow's run retries these same posts.
        log.error("delivery failed: %s", exc)
        notify_failure(delivery, f"{exc}\n\n{len(cleaned)} posts were not delivered.")
        return 1

    notify_success(delivery, chapters, previews, issue_number)
    record_sent(args.state, cleaned, today, issue_number=issue_number)
    return 0


def _should_hold(cleaned: list[CleanedPost], settings: Settings, today: date) -> bool:
    """Decide whether to hold this pile back for a fuller issue.

    Counted after cleaning on purpose: exclude_titles and min_words drop posts during
    the clean, so a pre-clean count would happily ship an issue of six items that turns
    into three. Holding costs nothing -- record_sent only runs on a confirmed send, so
    the same posts are selected again next run and the pile grows.
    """
    if len(cleaned) >= settings.min_posts_per_issue:
        return False

    waiting = len(cleaned)
    if waiting:
        oldest = min(c.post.published.date() for c in cleaned)
        held_days = (today - oldest).days
        if held_days >= settings.max_hold_days:
            log.info(
                "only %d post(s), but the oldest has waited %d days; sending anyway",
                waiting,
                held_days,
            )
            return False
        log.info(
            "holding %d post(s); waiting for %d (oldest has waited %d of %d days)",
            waiting,
            settings.min_posts_per_issue,
            held_days,
            settings.max_hold_days,
        )
    else:
        log.info("nothing waiting; no issue today")
    return True


def command_run(args: argparse.Namespace) -> int:
    """Run the pipeline, announcing any breakage rather than failing quietly."""
    if args.dry_run:
        return _run_pipeline(args)
    try:
        return _run_pipeline(args)
    except Exception as exc:
        log.exception("run failed")
        # Build a config purely to reach the notification channel; if even that is
        # unavailable there is nothing more we can do than the log line above.
        try:
            delivery = DeliveryConfig.from_env(dict(os.environ))
        except DeliveryError:
            delivery = None
        notify_failure(delivery, f"{type(exc).__name__}: {exc}")
        return 1


def command_preview(args: argparse.Namespace) -> int:
    post = fetch_single(args.url)
    result = clean_post(post, with_images=False)
    header = f"<!-- {result.word_count} words, truncated={result.truncated} -->"
    sys.stdout.write(f"{header}\n{result.html}\n")
    return 0


def command_feeds(args: argparse.Namespace) -> int:
    """Manage feeds.yaml. Output is markdown so the Action can post it verbatim."""
    path = Path(args.config)
    try:
        if args.action == "apply-issue":
            # Body arrives through the environment, never through argv or a shell.
            print(
                apply_issue(
                    path,
                    os.environ.get("ISSUE_BODY", ""),
                    os.environ.get("ISSUE_TITLE", ""),
                )
            )
            return 0
        if args.action == "list":
            print(list_publications(path))
            return 0
        if args.action == "add":
            message, report = add_publication(path, args.target, name=args.name)
            print(message)
            print()
            print("\n".join(report.summary_lines()))
            return 0
        if args.action == "remove":
            print(remove_publication(path, args.target))
            return 0
        if args.action in ("pause", "resume"):
            print(set_enabled(path, args.target, enabled=args.action == "resume"))
            return 0
    except FeedError as exc:
        print(f"Could not do that: {exc}")
        return 1
    raise ValueError(f"unknown action {args.action!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mornings", description=issue_title(date.today()))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="fetch, clean, build and send the issue")
    run_parser.add_argument("--dry-run", action="store_true", help="build only; no send, no state")
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="send even if fewer than settings.min_posts_per_issue have piled up",
    )
    run_parser.add_argument("--config", default=DEFAULT_CONFIG)
    run_parser.add_argument("--state", default=DEFAULT_STATE)
    run_parser.add_argument("--out", default=DEFAULT_OUT)
    run_parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="override settings.lookback_hours, for iterating on a fuller issue",
    )
    run_parser.set_defaults(func=command_run)

    feeds_parser = sub.add_parser("feeds", help="manage the publication list")
    feeds_parser.add_argument(
        "action",
        choices=["add", "remove", "pause", "resume", "list", "apply-issue"],
    )
    feeds_parser.add_argument(
        "target",
        nargs="?",
        default="",
        help="a Substack URL when adding, otherwise the publication name",
    )
    feeds_parser.add_argument("--name", help="override the name taken from the feed")
    feeds_parser.add_argument("--config", default=DEFAULT_CONFIG)
    feeds_parser.set_defaults(func=command_feeds)

    preview_parser = sub.add_parser("preview", help="clean a single post and dump the HTML")
    preview_parser.add_argument("url")
    preview_parser.set_defaults(func=command_preview)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception:
        log.exception("run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
