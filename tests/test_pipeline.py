"""End-to-end wiring: what gets recorded, and when.

The rule that matters: nothing is marked seen unless the send is confirmed, so a
failed run retries the same posts tomorrow instead of dropping them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mornings import __main__ as cli
from mornings.deliver import DeliveryError
from mornings.fetch import Post

FEEDS_YAML = """publications:
  - name: "Astral Codex Ten"
    type: public
    url: "https://www.astralcodexten.com/feed"
settings:
  lookback_hours: 26
  skip_if_empty: true
"""

ENV = {
    "KINDLE_EMAIL": "rohan@kindle.com",
    "SMTP_USER": "rohan@gmail.com",
    "SMTP_PASS": "app-password",
    "NTFY_TOPIC": "topic",
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "feeds.yaml").write_text(FEEDS_YAML)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "seen.json").write_text('{"guids": {}}')

    post = Post(
        guid="https://www.astralcodexten.com/p/one",
        publication="Astral Codex Ten",
        title="A Post",
        published=datetime.now(UTC),
        html="<p>Body copy that is long enough to matter.</p>",
        link="https://www.astralcodexten.com/p/one",
    )
    monkeypatch.setattr(cli, "fetch_all", lambda config: [post])
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "notify_success", lambda *a, **k: None)
    monkeypatch.setattr(cli, "notify_failure", lambda *a, **k: None)
    return tmp_path


def run_cli(workspace: Path, *extra: str) -> int:
    args = cli.build_parser().parse_args(
        [
            "run",
            "--config", str(workspace / "feeds.yaml"),
            "--state", str(workspace / "state" / "seen.json"),
            "--out", str(workspace / "out"),
            *extra,
        ]
    )
    return cli.command_run(args)


def seen_guids(workspace: Path) -> dict[str, str]:
    return json.loads((workspace / "state" / "seen.json").read_text())["guids"]


def test_successful_send_records_state(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "send_issue", lambda path, subject, config: None)
    assert run_cli(workspace) == 0
    assert "https://www.astralcodexten.com/p/one" in seen_guids(workspace)


def test_failed_send_records_nothing(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, subject: str, config: object) -> None:
        raise DeliveryError("SMTP failed after 3 attempts")

    monkeypatch.setattr(cli, "send_issue", refuse)
    assert run_cli(workspace) == 1
    assert seen_guids(workspace) == {}, "a failed send must not mark posts as seen"


def test_failed_send_notifies(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def refuse(path: Path, subject: str, config: object) -> None:
        raise DeliveryError("SMTP failed after 3 attempts")

    monkeypatch.setattr(cli, "send_issue", refuse)
    monkeypatch.setattr(cli, "notify_failure", lambda cfg, summary: captured.append(summary))
    run_cli(workspace)
    assert captured and "SMTP failed" in captured[0]


def test_unexpected_crash_still_notifies(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any breakage should reach the phone, not just SMTP trouble."""
    captured: list[str] = []
    monkeypatch.setattr(cli, "fetch_all", lambda config: (_ for _ in ()).throw(
        RuntimeError("feed parser exploded")
    ))
    monkeypatch.setattr(cli, "notify_failure", lambda cfg, summary: captured.append(summary))
    assert run_cli(workspace) == 1
    assert captured and "feed parser exploded" in captured[0]


def test_dry_run_sends_nothing_and_records_nothing(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[int] = []
    monkeypatch.setattr(cli, "send_issue", lambda *a, **k: sends.append(1))
    assert run_cli(workspace, "--dry-run") == 0
    assert sends == []
    assert seen_guids(workspace) == {}
    assert list((workspace / "out").glob("*.epub")), "dry run should still write the EPUB"


def test_already_seen_posts_are_skipped(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "state" / "seen.json").write_text(
        json.dumps({"guids": {"https://www.astralcodexten.com/p/one": "2026-08-18"}})
    )
    sends: list[int] = []
    monkeypatch.setattr(cli, "send_issue", lambda *a, **k: sends.append(1))
    assert run_cli(workspace) == 0
    assert sends == [], "an empty issue should not be sent when skip_if_empty is set"
