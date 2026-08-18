"""Tests for delivery, notification and the seen-state file."""

from __future__ import annotations

import json
import smtplib
from datetime import date, timedelta
from pathlib import Path

import pytest

from mornings import deliver
from mornings.deliver import (
    DeliveryConfig,
    DeliveryError,
    load_seen,
    notify_failure,
    notify_success,
    record_sent,
    send_issue,
)
from tests.test_epub import make_post

CONFIG = DeliveryConfig(
    kindle_email="rohan@kindle.com",
    smtp_user="rohan@gmail.com",
    smtp_pass="app-password-1234",
    ntfy_topic="morning-read-topic",
)


@pytest.fixture
def epub_file(tmp_path: Path) -> Path:
    path = tmp_path / "2026-08-18-morning-read.epub"
    path.write_bytes(b"PK\x03\x04 pretend epub")
    return path


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(deliver.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


# --------------------------------------------------------------------------- SMTP


def test_message_is_addressed_and_attached_correctly(epub_file: Path) -> None:
    message = deliver._build_message(epub_file, "2026-08-18 — Morning Read", CONFIG)
    assert message["To"] == "rohan@kindle.com"
    assert message["From"] == "rohan@gmail.com"
    # No "convert" subject: EPUB needs no conversion.
    assert message["Subject"] == "2026-08-18 — Morning Read"
    attachment = next(part for part in message.iter_attachments())
    assert attachment.get_content_type() == "application/epub+zip"
    assert attachment.get_filename() == "2026-08-18-morning-read.epub"


def test_send_succeeds_first_time(epub_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []
    monkeypatch.setattr(deliver, "_send_once", lambda m, c: attempts.append(1))
    send_issue(epub_file, "subject", CONFIG)
    assert len(attempts) == 1


def test_transient_failure_is_retried_twice_with_backoff(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    attempts = []

    def flaky(message: object, config: object) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise smtplib.SMTPServerDisconnected("connection reset")

    monkeypatch.setattr(deliver, "_send_once", flaky)
    send_issue(epub_file, "subject", CONFIG)
    assert len(attempts) == 3, "should retry twice before giving up"
    assert no_sleeping == [5, 20], "backoff should grow"


def test_persistent_transient_failure_raises_after_three_attempts(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = []

    def always_fails(message: object, config: object) -> None:
        attempts.append(1)
        raise OSError("network unreachable")

    monkeypatch.setattr(deliver, "_send_once", always_fails)
    with pytest.raises(DeliveryError):
        send_issue(epub_file, "subject", CONFIG)
    assert len(attempts) == 3


def test_auth_failure_is_not_retried(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, no_sleeping: list[float]
) -> None:
    """A bad app password will be just as bad twenty seconds later."""
    attempts = []

    def rejected(message: object, config: object) -> None:
        attempts.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(deliver, "_send_once", rejected)
    with pytest.raises(DeliveryError):
        send_issue(epub_file, "subject", CONFIG)
    assert len(attempts) == 1
    assert no_sleeping == []


def test_smtp_error_message_does_not_leak_the_password(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejected(message: object, config: object) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(deliver, "_send_once", rejected)
    with pytest.raises(DeliveryError) as caught:
        send_issue(epub_file, "subject", CONFIG)
    assert "app-password-1234" not in str(caught.value)


# --------------------------------------------------------------------- notification


def test_success_notification_lists_every_post(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        deliver, "_post_ntfy",
        lambda topic, title, body, priority=3: captured.update(
            topic=topic, title=title, body=body, priority=priority
        ),
    )
    chapters = [make_post("First", 500), make_post("Second", 700, "Stratechery")]
    previews = [make_post("Paywalled", 100, "The Pragmatic Engineer")]
    notify_success(CONFIG, chapters, previews)

    assert captured["title"] == "Morning Read — 2 posts"
    assert "Astral Codex Ten — First" in captured["body"]
    assert "Stratechery — Second" in captured["body"]
    assert "[preview] The Pragmatic Engineer — Paywalled" in captured["body"]


def test_success_notification_pluralises_one_post(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        deliver, "_post_ntfy",
        lambda topic, title, body, priority=3: captured.update(title=title),
    )
    notify_success(CONFIG, [make_post("Only", 300)], [])
    assert captured["title"] == "Morning Read — 1 post"


def test_notification_uses_json_so_an_em_dash_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """An em dash is not latin-1 encodable and would break an HTTP header."""
    sent: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, json: dict[str, str], timeout: int) -> Response:
        sent.update(url=url, payload=json)
        return Response()

    monkeypatch.setattr(deliver.httpx, "post", fake_post)
    deliver._post_ntfy("topic", "Morning Read — 3 posts", "body")
    assert sent["url"] == "https://ntfy.sh"
    assert sent["payload"]["title"] == "Morning Read — 3 posts"
    assert sent["payload"]["topic"] == "topic"


def test_failed_notification_never_breaks_a_successful_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue is already on the device; a dead ntfy must not fail the run."""
    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("ntfy is down")

    monkeypatch.setattr(deliver, "_post_ntfy", explode)
    notify_success(CONFIG, [make_post("One", 100)], [])  # must not raise


def test_failure_notification_is_high_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        deliver, "_post_ntfy",
        lambda topic, title, body, priority=3: captured.update(
            title=title, body=body, priority=priority
        ),
    )
    notify_failure(CONFIG, "SMTP failed after 3 attempts")
    assert captured["priority"] == 4
    assert "FAILED" in captured["title"]
    assert "SMTP failed" in captured["body"]


def test_notification_is_skipped_without_a_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(deliver, "_post_ntfy", lambda *a, **k: calls.append(1))
    notify_success(DeliveryConfig("a@kindle.com", "u", "p", None), [make_post("x", 10)], [])
    notify_failure(None, "everything is on fire")
    assert calls == []


# ---------------------------------------------------------------------------- env


def test_missing_required_env_vars_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DeliveryError, match="SMTP_PASS"):
        DeliveryConfig.from_env({"KINDLE_EMAIL": "a@kindle.com", "SMTP_USER": "u"})


def test_from_env_registers_the_password_as_a_secret() -> None:
    from mornings import config as config_module

    DeliveryConfig.from_env(
        {
            "KINDLE_EMAIL": "a@kindle.com",
            "SMTP_USER": "u@gmail.com",
            "SMTP_PASS": "super-secret-app-password",
            "NTFY_TOPIC": "a-private-topic-name",
        }
    )
    assert "super-secret-app-password" in config_module._SECRETS
    assert "a-private-topic-name" in config_module._SECRETS


# --------------------------------------------------------------------------- state


def test_record_sent_writes_guids(tmp_path: Path) -> None:
    state = tmp_path / "seen.json"
    record_sent(state, [make_post("One", 10), make_post("Two", 20)], date(2026, 8, 18))
    guids = json.loads(state.read_text())["guids"]
    assert set(guids) == {"guid-One", "guid-Two"}
    assert guids["guid-One"] == "2026-08-18"


def test_record_sent_prunes_entries_past_the_retention_window(tmp_path: Path) -> None:
    state = tmp_path / "seen.json"
    today = date(2026, 8, 18)
    state.write_text(
        json.dumps(
            {
                "guids": {
                    "ancient": (today - timedelta(days=61)).isoformat(),
                    "recent": (today - timedelta(days=59)).isoformat(),
                    "malformed": "not-a-date",
                }
            }
        )
    )
    record_sent(state, [make_post("New", 10)], today)
    guids = json.loads(state.read_text())["guids"]
    assert "ancient" not in guids, "entries past 60 days should be pruned"
    assert "malformed" not in guids
    assert "recent" in guids
    assert "guid-New" in guids


def test_load_seen_survives_a_corrupt_state_file(tmp_path: Path) -> None:
    state = tmp_path / "seen.json"
    state.write_text("{ this is not json")
    assert load_seen(state) == set()


def test_load_seen_on_missing_file(tmp_path: Path) -> None:
    assert load_seen(tmp_path / "nope.json") == set()


def test_seen_guids_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "seen.json"
    record_sent(state, [make_post("One", 10)], date(2026, 8, 18))
    assert load_seen(state) == {"guid-One"}


def test_send_once_does_starttls_then_login_then_send(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other SMTP tests stub _send_once, so verify the real conversation here."""
    calls: list[tuple[str, object]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls.append(("connect", (host, port, timeout)))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            calls.append(("quit", None))

        def starttls(self) -> None:
            calls.append(("starttls", None))

        def login(self, user: str, password: str) -> None:
            calls.append(("login", (user, password)))

        def send_message(self, message: object) -> None:
            calls.append(("send", message["To"]))

    monkeypatch.setattr(deliver.smtplib, "SMTP", FakeSMTP)
    message = deliver._build_message(epub_file, "subject", CONFIG)
    deliver._send_once(message, CONFIG)

    assert [name for name, _ in calls] == ["connect", "starttls", "login", "send", "quit"]
    assert calls[0][1] == ("smtp.gmail.com", 587, 60)
    assert calls[2][1] == ("rohan@gmail.com", "app-password-1234")
    assert calls[3][1] == "rohan@kindle.com"


def test_attached_epub_survives_a_round_trip(epub_file: Path) -> None:
    """Kindle rejects a corrupt attachment silently, so check the bytes come back."""
    import email as email_module
    import email.policy

    original = epub_file.read_bytes()
    message = deliver._build_message(epub_file, "subject", CONFIG)
    reparsed = email_module.message_from_bytes(bytes(message), policy=email.policy.default)
    attachment = next(part for part in reparsed.iter_attachments())
    assert attachment.get_payload(decode=True) == original


def test_ntfy_payload_matches_the_api_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: priority was sent as the string "default" and ntfy 400'd.

    The earlier stub accepted any payload, so the suite stayed green while every
    notification failed in production. This one enforces ntfy's actual JSON types.
    """
    seen: list[dict[str, object]] = []

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    def strict_post(url: str, json: dict[str, object], timeout: int) -> Response:
        assert set(json) <= {"topic", "title", "message", "priority", "tags"}
        assert isinstance(json["topic"], str) and json["topic"]
        assert isinstance(json["title"], str)
        assert isinstance(json["message"], str)
        # ntfy rejects a string priority with 400; it must be an int in 1..5.
        assert isinstance(json["priority"], int) and not isinstance(json["priority"], bool)
        assert 1 <= json["priority"] <= 5
        seen.append(json)
        return Response()

    monkeypatch.setattr(deliver.httpx, "post", strict_post)

    notify_success(CONFIG, [make_post("One", 400)], [make_post("Two", 50, "Stratechery")])
    notify_failure(CONFIG, "SMTP failed after 3 attempts")

    assert len(seen) == 2
    assert seen[0]["priority"] == deliver.NTFY_PRIORITY_DEFAULT
    assert seen[1]["priority"] == deliver.NTFY_PRIORITY_HIGH
    assert "—" in seen[0]["title"], "the em dash must survive the JSON round trip"
