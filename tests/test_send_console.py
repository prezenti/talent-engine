"""The page exists to make one thing hard to skip: recording that a send happened.

So the tests are about that column. Somebody already written to must not
reappear; a failed mark must not look like a success; and the queue must never
hand back a name the operator has already dealt with.
"""

from __future__ import annotations

import re

from talent_engine.server import send_console
from talent_engine.store.db import Store

PROGRAM = "prog"
MARK = "/send/tok/mark"


def _seed(store, handle, x="somebody", hook="a repo", total=None, sent=""):
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS scouted (program TEXT, handle TEXT, "
        "first_seen TEXT, channels TEXT DEFAULT '', PRIMARY KEY (program, handle))"
    )
    store.conn.execute(
        "INSERT OR IGNORE INTO scouted (program, handle, first_seen, channels) "
        "VALUES (?, ?, '2026-01-01T00:00:00+00:00', 'originators')",
        (PROGRAM, handle),
    )
    if total is not None:
        store.conn.execute(
            "INSERT INTO scores (run_id, handle, total, snapshot_digest, payload, "
            "scored_at) VALUES ('r', ?, ?, 'd', '{}', '2026-01-01T00:00:00+00:00')",
            (handle, total),
        )
    store.conn.commit()
    store.save_recon({"handle": handle, "x_handle": x})
    store.save_hook({"handle": handle, "hook": hook, "repo": "a/b",
                     "repo_desc": "does a thing", "basis": "public repository list"})
    if sent:
        store.mark_contacted(handle, when=sent)


def _page(store):
    return send_console.page(store.db_path, PROGRAM, MARK, "2026-08-24")


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.db_path = str(tmp_path / "t.db")
    return s


def test_the_queue_holds_only_people_still_to_write_to(tmp_path):
    store = _store(tmp_path)
    _seed(store, "waiting")
    _seed(store, "already", sent="2026-08-20T00:00:00+00:00")
    _seed(store, "no_account", x="")
    page = _page(store)
    assert "waiting" in page
    assert 'id="c-already"' not in page
    assert "no_account" not in page
    store.close()


def test_someone_with_no_hook_is_not_offered_an_empty_message(tmp_path):
    store = _store(tmp_path)
    _seed(store, "nothing_to_say", hook="")
    assert 'id="c-nothing_to_say"' not in _page(store)
    store.close()


def test_marking_removes_them_from_the_queue_for_good(tmp_path):
    store = _store(tmp_path)
    _seed(store, "someone")
    assert send_console.mark(store.db_path, "someone", "2026-08-24T09:00:00+00:00")
    assert 'id="c-someone"' not in _page(store)
    store.close()


def test_marking_twice_reports_that_nothing_changed(tmp_path):
    store = _store(tmp_path)
    _seed(store, "someone")
    assert send_console.mark(store.db_path, "someone", "2026-08-24T09:00:00+00:00")
    # The second call must not overwrite the first timestamp, and must say so:
    # a button that reports success on a no-op teaches the operator to trust it
    # when it has done nothing.
    assert not send_console.mark(store.db_path, "someone", "2026-08-25T09:00:00+00:00")
    assert store.hook_for_handle("someone")["sent_at"] == "2026-08-24T09:00:00+00:00"
    store.close()


def test_marking_an_unknown_handle_changes_nothing(tmp_path):
    store = _store(tmp_path)
    assert not send_console.mark(store.db_path, "stranger", "2026-08-24T09:00:00+00:00")
    store.close()


def test_the_message_is_rendered_into_the_page_ready_to_copy(tmp_path):
    store = _store(tmp_path)
    _seed(store, "someone", hook="your own repositories — specifically a/b")
    page = _page(store)
    assert "sponsorships.prezenti.xyz" in page
    assert "zoz from Prezenti" in page
    assert "specifically a/b" in page


def test_applicants_never_reach_the_send_queue(tmp_path):
    store = _store(tmp_path)
    _seed(store, "applied")
    store.conn.execute(
        "INSERT INTO submissions (submission_id, program, source, handle, "
        "application_json, received_at, status) "
        "VALUES ('s1', ?, 'tally', 'Applied', '{}', '2026-01-02', 'scored')",
        (PROGRAM,),
    )
    store.conn.commit()
    assert 'id="c-applied"" ' not in _page(store)
    assert 'id="c-applied"' not in _page(store)
    store.close()


def test_a_weak_hook_is_flagged_on_the_card(tmp_path):
    store = _store(tmp_path)
    _seed(store, "vague")
    store.save_hook({"handle": "vague", "hook": "a channel",
                     "basis": "scout channel only"})
    assert "weaker opener" in _page(store)
    store.close()


def test_the_page_escapes_what_github_published(tmp_path):
    store = _store(tmp_path)
    _seed(store, "someone")
    store.save_recon({"handle": "someone", "x_handle": "somebody",
                      "name": '<script>alert(1)</script>'})
    page = _page(store)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    store.close()


def test_pace_warning_only_once_past_the_soft_cap(tmp_path):
    store = _store(tmp_path)
    for i in range(send_console.SOFT_DAILY_CAP):
        _seed(store, f"p{i}", sent=f"2026-08-24T09:{i:02d}:00+00:00")
    _seed(store, "next_one")
    assert "reads as a bulk send" in _page(store)
    store.close()


def test_no_pace_warning_on_a_normal_day(tmp_path):
    store = _store(tmp_path)
    _seed(store, "a", sent="2026-08-24T09:00:00+00:00")
    _seed(store, "b")
    assert "reads as a bulk send" not in _page(store)
    store.close()


def test_the_mark_path_reaches_the_script_as_data_not_markup(tmp_path):
    store = _store(tmp_path)
    _seed(store, "someone")
    page = _page(store)
    assert re.search(r'const MARK_PATH = "/send/tok/mark"', page)
    store.close()
