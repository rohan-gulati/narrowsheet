"""Command line entry point: `python -m mornings ...`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from .clean import clean_post
from .config import load_config, setup_logging
from .epub import build_epub, issue_title
from .fetch import fetch_all, fetch_single, select_recent

log = logging.getLogger("mornings")

DEFAULT_CONFIG = "feeds.yaml"
DEFAULT_STATE = "state/seen.json"
DEFAULT_OUT = "out"


def load_seen(path: str | Path) -> set[str]:
    file = Path(path)
    if not file.exists():
        return set()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; treating every post as unseen", path)
        return set()
    return set(data.get("guids", {}))


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


def command_run(args: argparse.Namespace) -> int:
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

    cleaned = []
    image_offset = 0
    for post in fresh:
        result = clean_post(post, image_index_offset=image_offset)
        image_offset += len(result.images)
        cleaned.append(result)
        state = "preview" if result.truncated else f"{result.word_count} words"
        log.info("cleaned %s — %s (%s)", post.publication, post.title, state)

    chapters = [c for c in cleaned if not c.truncated]
    previews = [c for c in cleaned if c.truncated]

    out_path = Path(args.out) / f"{today.isoformat()}-morning-read.epub"
    build_epub(chapters, previews, today, settings.max_words_per_issue, out_path)

    if args.dry_run:
        run_epubcheck(out_path)
        log.info("dry run: wrote %s, sent nothing, wrote no state", out_path)
        return 0

    log.error("delivery is not wired up yet; re-run with --dry-run")
    return 1


def command_preview(args: argparse.Namespace) -> int:
    post = fetch_single(args.url)
    result = clean_post(post, with_images=False)
    header = f"<!-- {result.word_count} words, truncated={result.truncated} -->"
    sys.stdout.write(f"{header}\n{result.html}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mornings", description=issue_title(date.today()))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="fetch, clean, build and send the issue")
    run_parser.add_argument("--dry-run", action="store_true", help="build only; no send, no state")
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
