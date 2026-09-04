"""Settings loading, and the one setting combination that fails silently."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mornings.config import load_config

BASE = """publications:
  - name: "Astral Codex Ten"
    type: public
    url: "https://www.astralcodexten.com/feed"
settings:
"""


def write(tmp_path: Path, settings: str) -> Path:
    path = tmp_path / "feeds.yaml"
    path.write_text(BASE + settings)
    return path


def test_the_new_settings_load(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "  min_posts_per_issue: 10\n  max_hold_days: 14\n"))
    assert config.settings.min_posts_per_issue == 10
    assert config.settings.max_hold_days == 14


def test_defaults_keep_the_old_send_every_run_behaviour(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "  lookback_hours: 26\n"))
    assert config.settings.min_posts_per_issue == 1


def test_a_window_shorter_than_the_hold_ceiling_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A post held past the lookback window is dropped, not delayed -- and silently,
    since seen.json only prevents duplicates, it never recovers a miss."""
    settings = "  lookback_hours: 168\n  min_posts_per_issue: 10\n  max_hold_days: 14\n"
    path = write(tmp_path, settings)
    with caplog.at_level(logging.WARNING):
        load_config(path)
    assert "lookback_hours" in caplog.text
    assert "336" in caplog.text  # 14 days, in hours


def test_no_warning_when_the_window_outlasts_the_hold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = "  lookback_hours: 720\n  min_posts_per_issue: 10\n  max_hold_days: 14\n"
    path = write(tmp_path, settings)
    with caplog.at_level(logging.WARNING):
        load_config(path)
    assert caplog.text == ""


def test_no_warning_when_the_gate_is_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a threshold nothing is ever held, so the window cannot expire under one."""
    path = write(tmp_path, "  lookback_hours: 26\n  max_hold_days: 14\n")
    with caplog.at_level(logging.WARNING):
        load_config(path)
    assert caplog.text == ""


def test_the_shipped_config_is_consistent() -> None:
    """The real feeds.yaml must not trip the warning above."""
    settings = load_config("feeds.yaml").settings
    assert settings.lookback_hours >= settings.max_hold_days * 24
