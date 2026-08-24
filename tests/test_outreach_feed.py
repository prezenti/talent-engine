"""The worklist, and the two people who must never appear on it.

Somebody who already applied, and somebody who has already been written to.
Both mistakes are unrecoverable in the way that matters: the recipient sees a
programme that does not read its own records.
"""

from __future__ import annotations

import csv
import io

from talent_engine.server import outreach_feed
from talent_engine.store.db import Store

PROGRAM = "prog"


def _scout(store, handle, first_seen="2026-01-01T00:00:00+00:00", total=None):
    # `scouted` is created by the scout mode rather than the core schema, so a
    # feed test has to stand it up the same way the scouted-feed test does.
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS scouted (program TEXT, handle TEXT, "
        "first_seen TEXT, channels TEXT DEFAULT '', PRIMARY KEY (program, handle))"
    )
    store.conn.execute(
        "INSERT OR IGNORE INTO scouted (program, handle, first_seen, channels) "
        "VALUES (?, ?, ?, 'originators')",
        (PROGRAM, handle, first_seen),
    )
    if total is not None:
        store.conn.execute(
            "INSERT INTO scores (run_id, handle, total, snapshot_digest, payload, "
            "scored_at) VALUES (?, ?, ?, 'd', '{}', ?)",
            ("r1", handle, total, first_seen),
        )
    store.conn.commit()


def _recon(store, handle, x=""):
    store.save_recon({"handle": handle, "x_handle": x, "x_source": "profile field"})


def _rows(store):
    text = outreach_feed.csv_for(store.db_path, PROGRAM)
    return list(csv.DictReader(io.StringIO(text)))


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.db_path = str(tmp_path / "t.db")
    return s


def test_only_people_with_a_published_account_appear(tmp_path):
    store = _store(tmp_path)
    _scout(store, "reachable"); _recon(store, "reachable", "reachable_x")
    _scout(store, "unreachable"); _recon(store, "unreachable", "")
    _scout(store, "never_looked_up")
    assert [r["Handle"] for r in _rows(store)] == ["reachable"]
    store.close()


def test_applicants_are_never_invited_to_apply(tmp_path):
    store = _store(tmp_path)
    _scout(store, "applied"); _recon(store, "applied", "applied_x")
    store.conn.execute(
        "INSERT INTO submissions (submission_id, program, source, handle, "
        "application_json, received_at, status) "
        "VALUES ('s1', ?, 'tally', 'Applied', '{}', '2026-01-02', 'scored')",
        (PROGRAM,),
    )
    store.conn.commit()
    assert _rows(store) == []  # matched case-insensitively, as handles are
    store.close()


def test_contacted_is_shown_and_can_be_dropped(tmp_path):
    store = _store(tmp_path)
    _scout(store, "done"); _recon(store, "done", "done_x")
    store.save_hook({"handle": "done", "hook": "a repo"})
    store.mark_contacted("done", when="2026-08-24T10:00:00+00:00")
    assert _rows(store)[0]["Contacted"] == "2026-08-24"
    text = outreach_feed.csv_for(store.db_path, PROGRAM, include_contacted=False)
    assert list(csv.DictReader(io.StringIO(text))) == []
    store.close()


def test_best_scored_first_then_most_recently_discovered(tmp_path):
    store = _store(tmp_path)
    _scout(store, "low", "2026-01-01T00:00:00+00:00", total=10.0)
    _scout(store, "high", "2026-01-01T00:00:00+00:00", total=80.0)
    _scout(store, "old_unscored", "2026-01-01T00:00:00+00:00")
    _scout(store, "new_unscored", "2026-06-01T00:00:00+00:00")
    for h in ("low", "high", "old_unscored", "new_unscored"):
        _recon(store, h, f"{h}_x")
    assert [r["Handle"] for r in _rows(store)] == [
        "high", "low", "new_unscored", "old_unscored",
    ]
    store.close()


def test_unscored_people_are_kept_because_that_is_our_budget_not_their_merit(tmp_path):
    store = _store(tmp_path)
    _scout(store, "unscored"); _recon(store, "unscored", "unscored_x")
    rows = _rows(store)
    assert [r["Handle"] for r in rows] == ["unscored"]
    assert rows[0]["Score"] == ""
    store.close()


def test_send_to_is_a_link_a_human_can_click(tmp_path):
    store = _store(tmp_path)
    _scout(store, "someone"); _recon(store, "someone", "someone_x")
    row = _rows(store)[0]
    assert row["X"] == "@someone_x"
    assert row["Send to"] == "https://x.com/someone_x"
    assert row["GitHub"] == "https://github.com/someone"
    store.close()
