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


def test_the_message_names_the_repository_and_opens_like_a_person():
    found = outreach.hook_for(FakeClient([repo("thing")]), "someone", "originators", None)
    body = outreach.render(
        outreach.FULL_MESSAGE, handle="someone", name="Someone",
        hook=found["hook"], repo=found["repo"],
    )
    assert "real-thing" not in body
    assert "thing" in body
    assert body.startswith("Hi Someone,")
    # The tells that make a cold message obviously generated. None of them
    # belong in something claiming a person read your code.
    assert "—" not in body
    assert "Genuinely" not in body


def test_their_own_repo_is_named_the_way_they_name_it():
    # "JSONbored/metagraphed" said to JSONbored is a database field read aloud.
    assert outreach.short_repo("JSONbored/metagraphed", "jsonbored") == "metagraphed"
    # Somebody else's repository keeps its owner, because there it is the point.
    assert outreach.short_repo("scylladb/scylla", "mykaul") == "scylladb/scylla"


def test_two_people_who_compare_messages_find_two_different_ones():
    made = {
        outreach.render(outreach.SHORT_MESSAGE, handle=h, name="", repo=f"{h}/x")
        for h in ("alice", "bob", "carol", "dave", "erin", "frank", "grace")
    }
    # Not a guarantee of uniqueness -- a rotation cannot give 300 people 300
    # different messages -- but seven people must not all get the same one.
    assert len(made) >= 4


def test_the_opening_is_stable_for_a_person_across_rebuilds():
    first = outreach.render(outreach.SHORT_MESSAGE, handle="someone", name="", repo="a")
    again = outreach.render(outreach.SHORT_MESSAGE, handle="someone", name="", repo="a")
    assert first == again


def test_message_falls_back_to_the_handle_when_no_name_is_published():
    body = outreach.render(
        outreach.SHORT_MESSAGE, handle="boykush", name="   ", hook="a repo"
    )
    assert body.startswith("Hi boykush,")


def test_only_the_first_name_is_used_because_a_full_name_reads_as_a_merge():
    body = outreach.render(
        outreach.SHORT_MESSAGE, handle="x", name="Michael Gasperini", repo="x/y"
    )
    assert "Hi Michael," in body
    assert "Gasperini" not in body


def test_messages_only_invite_and_quote_the_published_terms():
    short = outreach.render(outreach.SHORT_MESSAGE, handle="a", name="", repo="a/b")
    full = outreach.render(outreach.FULL_MESSAGE, handle="a", name="", repo="a/b")
    for body in (short, full):
        assert "sponsorships.prezenti.xyz" in body
        assert "no equity" in body.lower()
    assert "$14,000" in full      # the cap, stated with the ask, not after it
    assert "2%" in full
    # The ask appears before the offer's benefits in the long version: burying
    # what we want is the thing that would actually cost trust.
    assert full.index("2%") < full.index("rubric")


def test_every_body_variant_carries_the_link_and_the_terms():
    for body in outreach.BODIES:
        assert "sponsorships.prezenti.xyz" in body
        assert "equity" in body


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
