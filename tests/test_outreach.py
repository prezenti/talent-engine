"""What is said to a stranger has to be true of that stranger.

The outreach hook is the programme's one testable claim in a cold message: we
found you by reading public work, and here is which work. So these tests are
about the failure that matters -- a message going out with a reason that was
manufactured because the real one was missing.
"""

from __future__ import annotations

import json

from talent_engine.modes import outreach
from talent_engine.store.db import Store


class FakeClient:
    def __init__(self, repos=None):
        self.repos = repos or []
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        return self.repos


def repo(name, **kw):
    base = {
        "name": name,
        "full_name": f"someone/{name}",
        "html_url": f"https://github.com/someone/{name}",
        "description": f"{name} does a thing",
        "fork": False,
        "archived": False,
        "pushed_at": "2026-01-01T00:00:00Z",
        "stargazers_count": 0,
    }
    base.update(kw)
    return base


def test_channel_phrase_prefers_the_most_specific_channel():
    assert "merged pull request" in outreach.channel_phrase("adjacent,contributors")
    assert "your own repositories" in outreach.channel_phrase("originators,adjacent")
    assert outreach.channel_phrase("") == ""
    assert outreach.channel_phrase("something-else") == ""


def test_evidence_repo_is_preferred_and_costs_no_request():
    payload = json.dumps({"dimensions": [{
        "key": "shipping_agency",
        "evidence": [
            {"claim": "27 commits", "url": "https://github.com/someone?tab=repositories"},
            {"claim": "original repo", "detail": "a design system generator",
             "url": "https://github.com/someone/uicockpit"},
        ],
    }]})
    client = FakeClient([repo("other")])
    found = outreach.hook_for(client, "someone", "originators", payload)
    assert found["repo"] == "someone/uicockpit"
    assert found["basis"] == "scored evidence"
    assert client.calls == []  # the scorer already paid for this


def test_profile_tab_links_are_not_repositories():
    payload = json.dumps({"dimensions": [{
        "key": "shipping_agency",
        "evidence": [{"claim": "x", "url": "https://github.com/someone?tab=repositories"}],
    }]})
    assert outreach.repo_from_evidence(payload) == {}


def test_profile_lookup_skips_forks_dotfiles_and_the_profile_readme():
    client = FakeClient([
        repo("someone"),                      # the profile README repo
        repo("dotfiles"),
        repo("a-fork", fork=True),
        repo("shelved", archived=True),
        repo("undescribed", description=""),
        repo("real-thing"),
    ])
    found = outreach.repo_from_profile(client, "someone")
    assert found["repo"] == "someone/real-thing"


def test_most_recent_wins_over_most_starred():
    client = FakeClient([
        repo("popular", stargazers_count=9000, pushed_at="2024-01-01T00:00:00Z"),
        repo("current", stargazers_count=0, pushed_at="2026-06-01T00:00:00Z"),
    ])
    assert outreach.repo_from_profile(client, "someone")["repo"] == "someone/current"


def test_nothing_found_yields_no_hook_rather_than_a_vague_one():
    found = outreach.hook_for(FakeClient([]), "someone", "", None)
    assert found["repo"] == ""
    assert found["hook"] == ""
    assert outreach.render(outreach.SHORT_MESSAGE, handle="someone", name="", hook="") == ""


def test_hook_reads_as_a_sentence_after_you_came_up_through():
    found = outreach.hook_for(FakeClient([repo("thing")]), "someone", "originators", None)
    body = outreach.render(
        outreach.FULL_MESSAGE, handle="someone", name="Someone", hook=found["hook"]
    )
    assert "You came up through your own repositories" in body
    assert "someone/thing" in body
    assert body.startswith("Hi Someone —")


def test_message_falls_back_to_the_handle_when_no_name_is_published():
    body = outreach.render(
        outreach.SHORT_MESSAGE, handle="boykush", name="   ", hook="a repo"
    )
    assert body.startswith("Hi boykush —")


def test_messages_only_invite_and_quote_the_published_terms():
    for template in (outreach.SHORT_MESSAGE, outreach.FULL_MESSAGE):
        assert "sponsorships.prezenti.xyz" in template
        assert "No equity" in template or "no equity" in template
    assert "$14,000" in outreach.FULL_MESSAGE      # the cap, not just the ask
    assert "2%" in outreach.FULL_MESSAGE


def test_marking_someone_contacted_survives_a_rebuild_of_the_hook(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_hook({"handle": "someone", "repo": "someone/a", "hook": "a"})
    store.mark_contacted("someone", when="2026-08-24T00:00:00+00:00")
    store.save_hook({"handle": "someone", "repo": "someone/b", "hook": "b"})
    row = store.hook_for_handle("someone")
    assert row["repo"] == "someone/b"
    assert row["sent_at"] == "2026-08-24T00:00:00+00:00"
    store.close()


def test_a_hook_with_no_repository_says_so_rather_than_looking_specific():
    found = outreach.hook_for(FakeClient([]), "someone", "contributors", None)
    assert found["repo"] == ""
    assert found["hook"].startswith("a merged pull request")
    assert found["basis"] == "scout channel only"
