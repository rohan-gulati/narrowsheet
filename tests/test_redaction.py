"""Feed tokens are credentials. Nothing may render them into a log line."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mornings import config as config_module
from mornings.config import (
    Publication,
    RedactingFormatter,
    load_config,
    redact,
    register_secret,
    resolve_feed_url,
)

PRIVATE_URL = "https://examplepaid.substack.com/feed/private/s3cr3t-t0ken-abc123xyz"
TOKEN = "s3cr3t-t0ken-abc123xyz"


def test_private_feed_path_is_redacted_without_registration() -> None:
    """The URL shape alone is enough; we never rely on having seen the token first."""
    assert TOKEN not in redact(f"GET {PRIVATE_URL} failed")
    assert "/feed/private/***" in redact(f"GET {PRIVATE_URL} failed")


def test_registered_secret_is_redacted_anywhere() -> None:
    register_secret("hunter2-app-password")
    assert "hunter2-app-password" not in redact("smtp auth failed for hunter2-app-password")


def test_short_values_are_not_registered() -> None:
    """A too-short secret would redact common substrings out of ordinary log lines."""
    register_secret("abc")
    assert redact("abc def") == "abc def"


def test_formatter_redacts_exception_tracebacks() -> None:
    """The formatter, not the call site, is what protects us from library exceptions."""
    formatter = RedactingFormatter("%(message)s")
    try:
        raise ConnectionError(f"failed to connect to {PRIVATE_URL}")
    except ConnectionError:
        import sys

        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="fetch failed", args=(), exc_info=sys.exc_info(),
        )
    rendered = formatter.format(record)
    assert "failed to connect" in rendered
    assert TOKEN not in rendered


def test_formatter_redacts_lazy_log_args() -> None:
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="fetching %s", args=(PRIVATE_URL,), exc_info=None,
    )
    assert TOKEN not in formatter.format(record)


def test_resolve_registers_private_url_as_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEED_TEST_PAID", PRIVATE_URL)
    pub = Publication(name="Test Paid", type="private", url_env="FEED_TEST_PAID")
    assert resolve_feed_url(pub) == PRIVATE_URL
    assert PRIVATE_URL in config_module._SECRETS


def test_missing_env_var_skips_publication_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEED_ABSENT", raising=False)
    pub = Publication(name="Absent", type="private", url_env="FEED_ABSENT")
    assert resolve_feed_url(pub) is None


def test_inline_private_url_in_yaml_is_rejected(tmp_path) -> None:
    """A private URL in the committed config is a leaked credential."""
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "publications:\n"
        '  - name: "Bad"\n'
        "    type: private\n"
        f'    url: "{PRIVATE_URL}"\n'
        '    url_env: "FEED_BAD"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="credential"):
        load_config(path)


def test_shipped_feeds_yaml_has_no_private_urls() -> None:
    text = Path("feeds.yaml").read_text(encoding="utf-8")
    assert "/feed/private/" not in text
