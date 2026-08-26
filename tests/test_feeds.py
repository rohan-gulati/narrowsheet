"""Tests for feed resolution and editing feeds.yaml.

No network: the HTTP layer is stubbed. The point of these is the two things that
would break quietly — resolution picking the wrong URL shape, and an edit silently
deleting the file's comments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mornings import feeds
from mornings.config import load_config
from mornings.feeds import (
    FeedError,
    add_publication,
    apply_issue,
    find_publication,
    list_publications,
    parse_issue_form,
    remove_publication,
    resolve_feed,
    set_enabled,
)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Example Publication</title>
<item><title>A post</title><link>https://example.com/p/a</link>
<pubDate>Mon, 18 Aug 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Body text.&lt;/p&gt;</description></item>
</channel></rss>"""

PAGE_WITH_LINK = (
    '<html><head><link rel="alternate" type="application/rss+xml" '
    'title="RSS" href="/feed"/></head><body>hi</body></html>'
)
PAGE_WITHOUT_LINK = "<html><head></head><body>hi</body></html>"

SAMPLE_YAML = '''# Morning Read source list.
#
# Never put a private feed URL in this file. It is committed to the repo.

publications:
  - name: "Existing Publication"
    type: public
    url: "https://existing.example.com/feed"
    # This publication needs its podcast posts filtered out.
    exclude_titles:
      - "🎙"
    min_words: 700

  # A paid publication with a per-subscriber feed.
  # - name: "Example Paid"
  #   type: private
  #   url_env: "FEED_EXAMPLE_PAID"

settings:
  # A week, not a day.
  lookback_hours: 168
  skip_if_empty: true
'''


class FakeResponse:
    def __init__(self, url: str, content: bytes) -> None:
        self.url = url
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Serves a fixed map of url -> body; anything else 404s."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return FakeResponse(url, self.pages[url].encode())

    def close(self) -> None:
        return None


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "feeds.yaml"
    path.write_text(SAMPLE_YAML, encoding="utf-8")
    return path


# ------------------------------------------------------------------ resolution


def test_url_that_is_already_a_feed_is_used_directly() -> None:
    """Appending /feed to a feed URL yields /feed/feed, which 404s or parses empty."""
    client = FakeClient({"https://example.com/feed": RSS})
    url, parsed = resolve_feed("https://example.com/feed", client)
    assert url == "https://example.com/feed"
    assert parsed.feed.title == "Example Publication"
    assert "https://example.com/feed/feed" not in client.requested


def test_homepage_resolves_via_autodiscovery() -> None:
    client = FakeClient(
        {"https://example.com/": PAGE_WITH_LINK, "https://example.com/feed": RSS}
    )
    url, _ = resolve_feed("https://example.com/", client)
    assert url == "https://example.com/feed"


def test_post_url_resolves_to_the_publication_feed() -> None:
    """You paste a link to a post you liked, not the homepage."""
    client = FakeClient(
        {
            "https://example.com/p/some-post": PAGE_WITH_LINK,
            "https://example.com/feed": RSS,
        }
    )
    url, _ = resolve_feed("https://example.com/p/some-post", client)
    assert url == "https://example.com/feed"


def test_falls_back_to_slash_feed_when_the_page_has_no_link_tag() -> None:
    """Non-Substack blogs often omit RSS autodiscovery but still serve /feed."""
    client = FakeClient(
        {"https://example.com/": PAGE_WITHOUT_LINK, "https://example.com/feed": RSS}
    )
    url, _ = resolve_feed("https://example.com/", client)
    assert url == "https://example.com/feed"


def test_unresolvable_url_raises_rather_than_writing_junk() -> None:
    client = FakeClient({"https://example.com/": PAGE_WITHOUT_LINK})
    with pytest.raises(FeedError, match="no usable RSS feed"):
        resolve_feed("https://example.com/", client)


def test_unreachable_url_raises() -> None:
    with pytest.raises(FeedError, match="could not fetch"):
        resolve_feed("https://example.com/", FakeClient({}))


# --------------------------------------------------------------------- editing


def _stub_inspect(monkeypatch: pytest.MonkeyPatch, name: str, url: str) -> None:
    report = feeds.FeedReport(
        name=name, feed_url=url, entries=20, newest=None,
        posts_last_30d=8, truncated=0, sampled=10,
    )
    monkeypatch.setattr(feeds, "inspect_feed", lambda _url, **_kw: report)


def test_adding_preserves_every_comment(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that matters: PyYAML would delete all of these."""
    before = config_file.read_text().count("#")
    _stub_inspect(monkeypatch, "New Publication", "https://new.example.com/feed")
    add_publication(config_file, "https://new.example.com/")
    after = config_file.read_text()
    assert after.count("#") == before
    assert "Never put a private feed URL in this file" in after
    assert "This publication needs its podcast posts filtered out." in after
    assert "# A week, not a day." in after


def test_add_then_remove_restores_the_file_exactly(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_file.read_text()
    _stub_inspect(monkeypatch, "New Publication", "https://new.example.com/feed")
    add_publication(config_file, "https://new.example.com/")
    assert config_file.read_text() != original
    remove_publication(config_file, "New Publication")
    assert config_file.read_text() == original


def test_added_entry_is_quoted_like_the_rest_of_the_file(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_inspect(monkeypatch, "New Publication", "https://new.example.com/feed")
    add_publication(config_file, "https://new.example.com/")
    assert '- name: "New Publication"' in config_file.read_text()


def test_adding_a_duplicate_changes_nothing(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_file.read_text()
    _stub_inspect(monkeypatch, "Existing Publication", "https://other.example.com/feed")
    message, _ = add_publication(config_file, "https://other.example.com/")
    assert "already in the list" in message
    assert config_file.read_text() == original


def test_adding_a_duplicate_feed_url_under_a_new_name_changes_nothing(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_file.read_text()
    _stub_inspect(monkeypatch, "Renamed", "https://existing.example.com/feed")
    message, _ = add_publication(config_file, "https://existing.example.com/")
    assert "already in the list" in message
    assert config_file.read_text() == original


def test_pause_keeps_the_publication_and_its_filters(config_file: Path) -> None:
    """Pausing must not discard exclude_titles/min_words the way remove would."""
    set_enabled(config_file, "Existing Publication", enabled=False)
    text = config_file.read_text()
    assert "enabled: false" in text
    assert "min_words: 700" in text
    assert "🎙" in text

    pub = load_config(config_file).publications[0]
    assert pub.enabled is False
    assert pub.min_words == 700


def test_resume_removes_the_flag_entirely(config_file: Path) -> None:
    original = config_file.read_text()
    set_enabled(config_file, "Existing Publication", enabled=False)
    set_enabled(config_file, "Existing Publication", enabled=True)
    assert config_file.read_text() == original


def test_names_match_case_insensitively(config_file: Path) -> None:
    data, _ = feeds._load(config_file)
    assert find_publication(data, "existing publication") is not None
    assert find_publication(data, "  EXISTING PUBLICATION  ") is not None
    assert find_publication(data, "nope") is None


def test_acting_on_an_unknown_publication_changes_nothing(config_file: Path) -> None:
    original = config_file.read_text()
    assert "No publication called" in remove_publication(config_file, "Ghost")
    assert "No publication called" in set_enabled(config_file, "Ghost", enabled=False)
    assert config_file.read_text() == original


def test_list_marks_paused_publications(config_file: Path) -> None:
    assert "(paused)" not in list_publications(config_file)
    set_enabled(config_file, "Existing Publication", enabled=False)
    assert "(paused)" in list_publications(config_file)


# ----------------------------------------------------------------- issue forms

ISSUE_BODY = """### Action

add

### Substack link (to add) or publication name (to remove, pause or resume)

https://new.example.com/p/a-post
"""


def test_issue_form_parsing() -> None:
    assert parse_issue_form(ISSUE_BODY) == ("add", "https://new.example.com/p/a-post")


def test_issue_form_accepts_any_valid_action() -> None:
    for action in ("add", "remove", "pause", "resume"):
        body = ISSUE_BODY.replace("\nadd\n", f"\n{action}\n")
        assert parse_issue_form(body)[0] == action


@pytest.mark.parametrize(
    "body",
    [
        "",
        "### Action\n\ndelete everything\n\n### Substack link\n\nhttps://x.com",
        "### Action\n\nadd\n\n### Substack link\n\n_No response_",
    ],
)
def test_malformed_issue_bodies_are_rejected(body: str) -> None:
    with pytest.raises(FeedError):
        parse_issue_form(body)


def test_apply_issue_end_to_end(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_inspect(monkeypatch, "New Publication", "https://new.example.com/feed")
    result = apply_issue(config_file, ISSUE_BODY)
    assert "Added **New Publication**" in result
    assert load_config(config_file).publications[-1].name == "New Publication"


# ------------------------------------------------------- substack profile links

PROFILE_JSON = (
    '{"name": "Someone", "handle": "someone",'
    ' "primaryPublication": {"name": "Their Newsletter", "subdomain": "theirs",'
    ' "custom_domain": null}}'
)


def test_profile_handle_extraction() -> None:
    from mornings.feeds import profile_handle

    assert profile_handle("https://substack.com/@ashwinivaishnaw") == "ashwinivaishnaw"
    # Shared from the Substack iOS app, which appends tracking params.
    assert (
        profile_handle("https://substack.com/@ashwinivaishnaw?r=8dhyo&utm_medium=ios")
        == "ashwinivaishnaw"
    )
    assert profile_handle("https://www.substack.com/@someone/note/abc") == "someone"
    assert profile_handle("https://www.noahpinion.blog/p/a-post") is None
    assert profile_handle("") is None


def test_profile_resolves_via_the_handle_subdomain() -> None:
    """Regression: substack.com/@handle used to fall through to substack.com/feed, which 404s.

    The handle guess is tried first because it never touches substack.com, which
    served a GitHub Actions runner a non-2xx where a browser gets a 200.
    """
    client = FakeClient({"https://someone.substack.com/feed": RSS})
    url, parsed = resolve_feed("https://substack.com/@someone", client)
    assert url == "https://someone.substack.com/feed"
    assert parsed.feed.title == "Example Publication"
    assert "https://substack.com/feed" not in client.requested
    assert "https://substack.com/@someone" not in client.requested


class ProfileClient(FakeClient):
    """Serves the profile API as JSON, like httpx does."""

    def get(self, url: str) -> FakeResponse:
        response = super().get(url)
        import json as _json

        response.json = lambda: _json.loads(response.text)  # type: ignore[attr-defined]
        return response


def test_profile_falls_back_to_the_api_when_the_handle_guess_misses() -> None:
    """A handle and its publication subdomain do not always match."""
    client = ProfileClient(
        {
            "https://substack.com/api/v1/user/someone/public_profile": PROFILE_JSON,
            "https://theirs.substack.com/feed": RSS,
        }
    )
    url, _ = resolve_feed("https://substack.com/@someone", client)
    assert url == "https://theirs.substack.com/feed"


def test_profile_with_no_publication_says_so_plainly() -> None:
    client = ProfileClient(
        {
            "https://substack.com/api/v1/user/nobody/public_profile":
                '{"name": "Nobody", "primaryPublication": null, "publicationUsers": []}'
        }
    )
    with pytest.raises(FeedError, match="is a Substack profile"):
        resolve_feed("https://substack.com/@nobody", client)


def test_http_error_reports_the_status_code() -> None:
    """'(HTTPStatusError)' told the user nothing about what went wrong."""
    import httpx

    class Failing(FakeClient):
        def get(self, url: str) -> FakeResponse:
            request = httpx.Request("GET", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("403", request=request, response=response)

    with pytest.raises(FeedError, match="returned HTTP 403"):
        resolve_feed("https://example.com/blocked", Failing({}))


def test_link_pasted_into_the_title_still_works() -> None:
    """What actually happened: on a phone the cursor lands in the title box first."""
    body = (
        "### Substack link\n\n_No response_\n\n### Action\n\nadd\n"
    )
    action, target = parse_issue_form(body, title="https://substack.com/@ashwinivaishnaw")
    assert action == "add"
    assert target == "https://substack.com/@ashwinivaishnaw"


def test_title_link_keeps_its_tracking_params() -> None:
    action, target = parse_issue_form(
        "### Substack link\n\n_No response_\n\n### Action\n\nadd\n",
        title="feed: https://substack.com/@x?r=8dhyo&utm_medium=ios",
    )
    assert target == "https://substack.com/@x?r=8dhyo&utm_medium=ios"


def test_the_field_wins_when_both_are_filled() -> None:
    _, target = parse_issue_form(ISSUE_BODY, title="https://wrong.example.com/")
    assert target == "https://new.example.com/p/a-post"


def test_no_link_anywhere_still_errors() -> None:
    with pytest.raises(FeedError, match="Substack link box"):
        parse_issue_form(
            "### Substack link\n\n_No response_\n\n### Action\n\nadd\n",
            title="feed: nothing useful here",
        )
