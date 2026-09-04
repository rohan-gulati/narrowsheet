"""End-to-end wiring: what gets recorded, and when.

The rule that matters: nothing is marked seen unless the send is confirmed, so a
failed run retries the same posts tomorrow instead of dropping them.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
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


# ------------------------------------------------------- per-publication filtering

MIN_WORDS_YAML = """publications:
  - name: "Lenny's Newsletter"
    type: public
    url: "https://www.lennysnewsletter.com/feed"
    exclude_titles:
      - "How I AI"
      - " | "
    min_words: 700
settings:
  lookback_hours: 168
  skip_if_empty: true
"""


def _post(title: str, body_words: int, guid: str) -> Post:
    return Post(
        guid=guid,
        publication="Lenny's Newsletter",
        title=title,
        published=datetime.now(UTC),
        html="<p>" + " ".join(["word"] * body_words) + "</p>",
        link="https://www.lennysnewsletter.com/p/x",
    )


@pytest.fixture
def lenny_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "feeds.yaml").write_text(MIN_WORDS_YAML)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "seen.json").write_text('{"guids": {}}')
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "notify_success", lambda *a, **k: None)
    monkeypatch.setattr(cli, "notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(cli, "send_issue", lambda *a, **k: None)
    return tmp_path


def _titles_in_issue(workspace: Path) -> str:
    epub = next((workspace / "out").glob("*.epub"))
    with zipfile.ZipFile(epub) as archive:
        return "".join(
            archive.read(n).decode()
            for n in archive.namelist()
            if n.endswith(".xhtml")
        )


def test_short_post_is_dropped_by_the_word_floor(
    lenny_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "fetch_all",
        lambda config: [
            _post("A real essay worth reading", 900, "g-long"),
            _post("A podcast show-note stub", 300, "g-short"),
        ],
    )
    assert run_cli(lenny_workspace, "--dry-run") == 0
    body = _titles_in_issue(lenny_workspace)
    assert "A real essay worth reading" in body
    assert "A podcast show-note stub" not in body


def test_word_floor_does_not_swallow_a_truncated_preview(
    lenny_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paywalled post is short for a different reason, and is already cover-listed."""
    truncated = _post("Subscriber-only digest", 20, "g-paywalled")
    truncated.html = (
        "<p>Welcome to this subscriber-only edition.</p>"
        '<p><a href="https://www.lennysnewsletter.com/p/x">Read more</a></p>'
    )
    monkeypatch.setattr(
        cli,
        "fetch_all",
        lambda config: [_post("A real essay", 900, "g-long"), truncated],
    )
    assert run_cli(lenny_workspace, "--dry-run") == 0
    body = _titles_in_issue(lenny_workspace)
    # Listed on the cover as a preview rather than dropped outright.
    assert "Subscriber-only digest" in body


def test_excluded_titles_never_reach_the_issue(
    lenny_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mornings.fetch import excluded_title_pattern

    config = cli.load_config(str(lenny_workspace / "feeds.yaml"))
    pub = config.publications[0]
    assert excluded_title_pattern("How I AI: episode", pub.exclude_titles) == "How I AI"
    assert excluded_title_pattern("An essay | Some Guest", pub.exclude_titles) == " | "
    assert excluded_title_pattern("How to make people care", pub.exclude_titles) is None


# --------------------------------------------------------------------------------
# The send gate: an issue goes out when enough has piled up, not every morning.
# --------------------------------------------------------------------------------

GATED_YAML = """publications:
  - name: "Lenny's Newsletter"
    type: public
    url: "https://www.lennysnewsletter.com/feed"
    min_words: 700
settings:
  lookback_hours: 720
  min_posts_per_issue: 3
  max_hold_days: 14
  skip_if_empty: true
"""


@pytest.fixture
def gated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "feeds.yaml").write_text(GATED_YAML)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "seen.json").write_text('{"guids": {}}')
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "notify_success", lambda *a, **k: None)
    monkeypatch.setattr(cli, "notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(cli, "send_issue", lambda *a, **k: None)
    return tmp_path


def _aged_post(title: str, guid: str, days_old: int = 0, words: int = 900) -> Post:
    return Post(
        guid=guid,
        publication="Lenny's Newsletter",
        title=title,
        published=datetime.now(UTC) - timedelta(days=days_old),
        html="<p>" + " ".join(["word"] * words) + "</p>",
        link=f"https://www.lennysnewsletter.com/p/{guid}",
    )


def _built_an_issue(workspace: Path) -> bool:
    return any((workspace / "out").glob("*.epub")) if (workspace / "out").exists() else False


def test_below_the_threshold_the_run_holds(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two posts is not an issue. Nothing is built, sent or recorded."""
    monkeypatch.setattr(
        cli,
        "fetch_all",
        lambda config: [_aged_post("One", "g1"), _aged_post("Two", "g2")],
    )
    assert run_cli(gated_workspace) == 0
    assert not _built_an_issue(gated_workspace)
    assert seen_guids(gated_workspace) == {}


def test_a_held_pile_ships_once_it_is_big_enough(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The posts held above are not lost -- they go out with the next arrival."""
    posts = [_aged_post("One", "g1"), _aged_post("Two", "g2")]
    monkeypatch.setattr(cli, "fetch_all", lambda config: list(posts))
    assert run_cli(gated_workspace) == 0
    assert seen_guids(gated_workspace) == {}

    posts.append(_aged_post("Three", "g3"))
    assert run_cli(gated_workspace) == 0
    assert set(seen_guids(gated_workspace)) == {"g1", "g2", "g3"}


def test_the_hold_ceiling_ships_a_short_issue(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet fortnight must not leave a post to rot in the queue."""
    monkeypatch.setattr(
        cli,
        "fetch_all",
        lambda config: [_aged_post("Old news", "g-old", days_old=15)],
    )
    assert run_cli(gated_workspace) == 0
    assert set(seen_guids(gated_workspace)) == {"g-old"}


def test_force_ships_regardless_of_the_threshold(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "fetch_all", lambda config: [_aged_post("One", "g1")])
    assert run_cli(gated_workspace, "--force") == 0
    assert set(seen_guids(gated_workspace)) == {"g1"}


def test_dry_run_ignores_the_gate(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "fetch_all", lambda config: [_aged_post("One", "g1")])
    assert run_cli(gated_workspace, "--dry-run") == 0
    assert _built_an_issue(gated_workspace)
    assert seen_guids(gated_workspace) == {}


def test_the_gate_counts_cleaned_posts_not_fetched_ones(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """min_words drops posts during the clean, so counting before it would ship an
    issue of three that turns into two."""
    monkeypatch.setattr(
        cli,
        "fetch_all",
        lambda config: [
            _aged_post("One", "g1"),
            _aged_post("Two", "g2"),
            _aged_post("Podcast show notes", "g3", words=120),
        ],
    )
    assert run_cli(gated_workspace) == 0
    assert seen_guids(gated_workspace) == {}


def test_issues_are_numbered_and_the_counter_survives(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "fetch_all", lambda config: [_aged_post("One", "g1")])
    assert run_cli(gated_workspace, "--force") == 0
    state = json.loads((gated_workspace / "state" / "seen.json").read_text())
    assert state["issue_number"] == 1

    monkeypatch.setattr(cli, "fetch_all", lambda config: [_aged_post("Two", "g2")])
    assert run_cli(gated_workspace, "--force") == 0
    state = json.loads((gated_workspace / "state" / "seen.json").read_text())
    assert state["issue_number"] == 2
    assert set(state["guids"]) == {"g1", "g2"}


def test_the_issue_number_reaches_the_epub_and_the_notification(
    gated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, str] = {}
    monkeypatch.setattr(
        cli, "send_issue", lambda path, subject, config: sent.update(subject=subject)
    )
    notified: dict[str, int] = {}
    monkeypatch.setattr(
        cli,
        "notify_success",
        lambda config, chapters, previews, number: notified.update(number=number),
    )
    monkeypatch.setattr(cli, "fetch_all", lambda config: [_aged_post("One", "g1")])
    assert run_cli(gated_workspace, "--force") == 0

    assert sent["subject"].endswith("Morning Read No. 1")
    assert notified["number"] == 1
    assert "No. 01" in _titles_in_issue(gated_workspace)
