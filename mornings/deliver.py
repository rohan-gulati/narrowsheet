"""Send the issue to Kindle, announce it, and record what went out.

State is written only after a confirmed send, so a failed run retries the same
posts tomorrow rather than losing them.
"""

from __future__ import annotations

import json
import logging
import smtplib
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import httpx

from .clean import CleanedPost
from .config import register_secret

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 60
SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = (5, 20)

NTFY_URL = "https://ntfy.sh"
NTFY_TIMEOUT = 15

STATE_RETENTION_DAYS = 60

# Auth and addressing problems will fail identically on a retry.
PERMANENT_SMTP_ERRORS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
)


class DeliveryError(RuntimeError):
    """Raised when the issue could not be delivered."""


@dataclass(frozen=True)
class DeliveryConfig:
    kindle_email: str
    smtp_user: str
    smtp_pass: str
    ntfy_topic: str | None = None
    smtp_host: str = SMTP_HOST
    smtp_port: int = SMTP_PORT

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> DeliveryConfig:
        missing = [k for k in ("KINDLE_EMAIL", "SMTP_USER", "SMTP_PASS") if not environ.get(k)]
        if missing:
            raise DeliveryError(f"missing required environment variables: {', '.join(missing)}")

        smtp_pass = environ["SMTP_PASS"]
        ntfy_topic = environ.get("NTFY_TOPIC") or None
        register_secret(smtp_pass)
        register_secret(ntfy_topic)

        kindle_email = environ["KINDLE_EMAIL"].strip()
        if not kindle_email.endswith("@kindle.com"):
            log.warning("KINDLE_EMAIL does not look like a @kindle.com address")

        return cls(
            kindle_email=kindle_email,
            smtp_user=environ["SMTP_USER"].strip(),
            smtp_pass=smtp_pass,
            ntfy_topic=ntfy_topic,
        )


def _build_message(epub_path: Path, subject: str, config: DeliveryConfig) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.smtp_user
    message["To"] = config.kindle_email
    # No "convert" subject: that is a MOBI-era instruction and EPUB needs no conversion.
    message["Subject"] = subject
    message.set_content("Your Morning Read is attached.")
    message.add_attachment(
        epub_path.read_bytes(),
        maintype="application",
        subtype="epub+zip",
        filename=epub_path.name,
    )
    return message


def _send_once(message: EmailMessage, config: DeliveryConfig) -> None:
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=SMTP_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_pass)
        smtp.send_message(message)


def send_issue(epub_path: Path, subject: str, config: DeliveryConfig) -> None:
    """Mail the issue, retrying twice with backoff on transient SMTP failures."""
    message = _build_message(epub_path, subject, config)
    for attempt in range(SEND_RETRIES + 1):
        try:
            _send_once(message, config)
            log.info("sent %s to the Kindle address", epub_path.name)
            return
        except PERMANENT_SMTP_ERRORS as exc:
            raise DeliveryError(f"SMTP rejected the message: {type(exc).__name__}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            if attempt == SEND_RETRIES:
                raise DeliveryError(
                    f"SMTP failed after {SEND_RETRIES + 1} attempts: {type(exc).__name__}"
                ) from exc
            delay = RETRY_BACKOFF_SECONDS[attempt]
            log.warning(
                "send attempt %d failed (%s); retrying in %ds",
                attempt + 1,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)


def _post_ntfy(topic: str, title: str, body: str, priority: str = "default") -> None:
    # The JSON endpoint rather than headers: an em dash in the title is not
    # latin-1 encodable and would blow up header serialisation.
    response = httpx.post(
        NTFY_URL,
        json={"topic": topic, "title": title, "message": body, "priority": priority},
        timeout=NTFY_TIMEOUT,
    )
    response.raise_for_status()


def notify_success(config: DeliveryConfig, chapters: list[CleanedPost],
                   previews: list[CleanedPost]) -> None:
    if not config.ntfy_topic:
        log.info("no NTFY_TOPIC configured; skipping notification")
        return
    lines = [f"{c.post.publication} — {c.post.title}" for c in chapters]
    lines += [f"[preview] {p.post.publication} — {p.post.title}" for p in previews]
    title = f"Morning Read — {len(chapters)} post{'' if len(chapters) == 1 else 's'}"
    try:
        _post_ntfy(config.ntfy_topic, title, "\n".join(lines) or "Nothing new today.")
    except Exception:
        # The issue is already on the Kindle; a failed ping must not fail the run.
        log.exception("could not send the success notification")


def notify_failure(config: DeliveryConfig | None, summary: str) -> None:
    """Announce a broken run. Better to know than to find out by absence."""
    topic = config.ntfy_topic if config else None
    if not topic:
        log.info("no NTFY_TOPIC configured; skipping failure notification")
        return
    try:
        _post_ntfy(topic, "Morning Read — FAILED", summary, priority="high")
    except Exception:
        log.exception("could not send the failure notification")


def load_seen(path: str | Path) -> set[str]:
    file = Path(path)
    if not file.exists():
        return set()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; treating every post as unseen", file)
        return set()
    return set(data.get("guids", {}))


def record_sent(
    path: str | Path,
    posts: list[CleanedPost],
    today: date | None = None,
    retention_days: int = STATE_RETENTION_DAYS,
) -> int:
    """Mark posts as sent and prune entries older than the retention window."""
    file = Path(path)
    today = today or datetime.now(UTC).date()
    guids: dict[str, str] = {}
    if file.exists():
        try:
            guids = json.loads(file.read_text(encoding="utf-8")).get("guids", {})
        except json.JSONDecodeError:
            log.warning("%s was unreadable; starting a fresh state file", file)

    for post in posts:
        guids[post.post.guid] = today.isoformat()

    cutoff = today - timedelta(days=retention_days)
    kept = {}
    for guid, seen_on in guids.items():
        try:
            if date.fromisoformat(seen_on) >= cutoff:
                kept[guid] = seen_on
        except ValueError:
            continue  # drop malformed entries

    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"guids": dict(sorted(kept.items(), key=lambda kv: (kv[1], kv[0])))}
    file.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    log.info("recorded %d posts as sent (%d tracked)", len(posts), len(kept))
    return len(kept)
