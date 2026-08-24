"""The half of the system that can act on a stranger without a person present.

So the tests are about restraint rather than capability: a refusal is recorded
and never retried, a batch cannot run faster than a person could type, one bad
membership does not cost the batch, and the tokens are written where only this
account can read them.
"""

from __future__ import annotations

import json
import os

from talent_engine.modes import x_outreach
from talent_engine.store.db import Store
from talent_engine.x import auth
from talent_engine.x.client import XClient, XResponse


class FakeTransport:
    """Answers by (method, path-fragment). Records every call in order."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        for fragment, (status, payload) in self.answers.items():
            if fragment in url:
                return status, json.dumps(payload), {}
        return 200, json.dumps({"data": {}}), {}


def client(transport, sleeps=None):
    return XClient(lambda: "token", transport=transport,
                   sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_usernames_resolve_to_ids_in_one_request_per_hundred():
    t = FakeTransport({"/2/users/by": (200, {"data": [
        {"id": "1", "username": "alice"}, {"id": "2", "username": "Bob"},
    ]})})
    found = x_outreach.resolve_ids(client(t), [f"u{i}" for i in range(150)])
    assert len([c for c in t.calls if "/2/users/by" in c[1]]) == 2
    assert found == {"alice": "1", "bob": "2"}   # keyed lowercase, as handles are


def test_a_handle_that_no_longer_exists_is_absent_not_an_error():
    t = FakeTransport({"/2/users/by": (200, {"data": [{"id": "1", "username": "alive"}]})})
    found = x_outreach.resolve_ids(client(t), ["alive", "deleted"])
    assert found == {"alive": "1"}


def test_a_list_is_private_unless_asked_otherwise():
    sent = {}

    def transport(method, url, headers, body):
        sent.update(json.loads(body))
        return 200, json.dumps({"data": {"id": "99"}}), {}

    assert x_outreach.ensure_list(client(transport), "Name", "Desc") == {"id": "99"}
    assert sent["private"] is True


def test_list_name_is_truncated_knowingly_rather_than_rejected_by_x():
    sent = {}

    def transport(method, url, headers, body):
        sent.update(json.loads(body))
        return 200, json.dumps({"data": {"id": "1"}}), {}

    x_outreach.ensure_list(client(transport), "x" * 60, "y" * 300)
    assert len(sent["name"]) == 25
    assert len(sent["description"]) == 100


def test_one_failed_membership_does_not_cost_the_rest():
    def transport(method, url, headers, body):
        if json.loads(body)["user_id"] == "bad":
            return 403, json.dumps({"detail": "protected"}), {}
        return 200, json.dumps({"data": {"is_member": True}}), {}

    seen = []
    result = x_outreach.add_members(
        client(transport), "1", ["a", "bad", "c"],
        on_result=lambda uid, ok, detail: seen.append((uid, ok)),
    )
    assert result == {"added": 2, "failed": 1}
    assert seen == [("a", True), ("bad", False), ("c", True)]


def test_a_refusal_is_told_apart_from_a_transport_failure():
    assert x_outreach.classify(XResponse(200, {})) == ("sent", "")
    assert x_outreach.classify(XResponse(403, {}, "cannot message"))[0] == "refused"
    assert x_outreach.classify(XResponse(400, {}, "bad recipient"))[0] == "refused"
    assert x_outreach.classify(XResponse(503, {}, "upstream"))[0] == "error"
    assert x_outreach.classify(XResponse(0, {}, "no route"))[0] == "error"


def test_sending_pauses_between_people_and_never_before_the_first():
    t = FakeTransport({"/messages": (200, {"data": {"dm_event_id": "1"}})})
    slept = []
    x_outreach.send_batch(
        client(t), [{"user_id": str(i), "text": "hi"} for i in range(4)],
        sleep=slept.append, jitter=lambda a, b: a,
    )
    assert len(slept) == 3
    assert min(slept) >= x_outreach.DM_GAP_SECONDS[0]


def test_the_batch_stops_after_three_transport_errors():
    t = FakeTransport({"/messages": (503, {"detail": "down"})})
    counts = x_outreach.send_batch(
        client(t), [{"user_id": str(i), "text": "hi"} for i in range(20)],
        sleep=lambda s: None, jitter=lambda a, b: 0,
    )
    assert counts["error"] == 3
    assert len([c for c in t.calls if "/messages" in c[1]]) == 3


def test_refusals_do_not_stop_the_batch_because_they_are_information():
    t = FakeTransport({"/messages": (403, {"detail": "not accepting"})})
    counts = x_outreach.send_batch(
        client(t), [{"user_id": str(i), "text": "hi"} for i in range(6)],
        sleep=lambda s: None, jitter=lambda a, b: 0,
    )
    assert counts["refused"] == 6


def test_limit_is_honoured():
    t = FakeTransport({"/messages": (200, {"data": {}})})
    x_outreach.send_batch(
        client(t), [{"user_id": str(i), "text": "hi"} for i in range(50)],
        limit=5, sleep=lambda s: None, jitter=lambda a, b: 0,
    )
    assert len([c for c in t.calls if "/messages" in c[1]]) == 5


def test_rate_limiting_waits_for_the_reset_x_names():
    calls = {"n": 0}

    def transport(method, url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            import time as _t
            return 429, "{}", {"x-rate-limit-reset": str(int(_t.time()) + 30)}
        return 200, json.dumps({"data": {"id": "1"}}), {}

    slept = []
    resp = client(transport, slept).post("/2/lists", {"name": "n"})
    assert resp.ok
    assert 20 <= slept[0] <= 31


def test_a_refused_person_is_recorded_and_never_queued_again(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_x_id("someone", "somebody", "42")
    store.record_dm("someone", "refused", "not accepting messages")
    row = store.x_delivery_for("someone")
    assert row["dm_status"] == "refused"
    assert row["user_id"] == "42"
    assert row["dm_at"]
    store.close()


def test_the_grant_is_written_unreadable_to_anyone_else(tmp_path):
    path = tmp_path / "x-tokens.json"
    auth.TokenStore(path).save({"access_token": "a", "refresh_token": "r",
                                "expires_in": 7200})
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_an_expired_grant_refreshes_and_the_new_one_is_saved_before_use(tmp_path, monkeypatch):
    path = tmp_path / "x-tokens.json"
    store = auth.TokenStore(path)
    store.save({"access_token": "old", "refresh_token": "r1",
                "expires_in": 10, "obtained_at": 0})
    monkeypatch.setattr(auth, "refresh", lambda *a, **k: {
        "access_token": "new", "refresh_token": "r2", "expires_in": 7200,
    })
    assert store.access_token("cid") == "new"
    # X rotates the refresh token on use: if the replacement were not persisted
    # here, the stored one would already be spent and the grant unrecoverable.
    assert json.loads(path.read_text())["refresh_token"] == "r2"


def test_a_live_grant_is_not_refreshed_for_nothing(tmp_path, monkeypatch):
    import time as _t
    path = tmp_path / "x-tokens.json"
    store = auth.TokenStore(path)
    store.save({"access_token": "live", "refresh_token": "r",
                "expires_in": 7200, "obtained_at": int(_t.time())})
    monkeypatch.setattr(auth, "refresh", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not have refreshed")))
    assert store.access_token("cid") == "live"


def test_the_authorize_url_asks_for_offline_access_or_the_grant_dies_in_hours():
    verifier, ch = auth.challenge()
    url = auth.authorize_url("cid", ch, "state")
    assert "offline.access" in url
    assert "code_challenge_method=S256" in url
    assert "dm.write" in url and "list.write" in url
    assert verifier != ch
