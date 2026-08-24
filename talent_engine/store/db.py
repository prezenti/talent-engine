"""Persistence and the audit log.

The reproducibility requirement is that any historical score can be regenerated
exactly.  That needs three things stored together, and storing only the score
satisfies none of them:

  * the snapshot   -- the exact inputs the scorer saw
  * the weights    -- the rubric as configured at the time
  * the code version -- which scorer produced it

`replay()` reconstitutes a stored snapshot and re-scores it under the stored
weights.  If the result differs from what was recorded, the scorer changed
behaviour between then and now, and `verify()` reports that as a mismatch
rather than quietly returning the new number.  Silent re-scoring would make the
audit log actively misleading.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from ..config import ProgramConfig
from ..model import (
    Application,
    CandidateScore,
    ProfileSnapshot,
    PullRequestActivity,
    RepoActivity,
    ReviewActivity,
    utc_now_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    mode TEXT NOT NULL,
    code_version TEXT NOT NULL,
    weights_digest TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    digest TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    run_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    total REAL NOT NULL,
    snapshot_digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (run_id, handle)
);
CREATE TABLE IF NOT EXISTS cohort (
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    declared_repo TEXT DEFAULT '',
    baseline_run_id TEXT DEFAULT '',
    selected_at TEXT NOT NULL,
    -- Acceptance artefacts. The payment route and attestation UID are the
    -- public objects the terms depend on, so they live next to the cohort row
    -- rather than in someone's notes: `monitor` and `measure` need to reach
    -- them, and so does anyone auditing what was actually agreed.
    accepted_at TEXT DEFAULT '',
    split_address TEXT DEFAULT '', -- legacy name kept for old DBs; no new Splits are deployed
    payment_address TEXT DEFAULT '',
    attestation_uid TEXT DEFAULT '',
    attestation_signer TEXT DEFAULT '',
    months_received INTEGER DEFAULT 0,
    PRIMARY KEY (program, handle)
);
CREATE INDEX IF NOT EXISTS idx_scores_handle ON scores(handle);

-- Inbound form submissions. `submission_id` is the form's own id, so a webhook
-- redelivery is a no-op rather than a second score for the same person.
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    source TEXT NOT NULL,
    form_id TEXT DEFAULT '',
    handle TEXT DEFAULT '',
    raw_handle TEXT DEFAULT '',
    application_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | scored | unparsable | error
    run_id TEXT DEFAULT '',
    total REAL,
    concerns TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_handle ON submissions(handle);

-- Decisions, and the feedback owed because of them. The policy commits the
-- programme to giving feedback to unsuccessful applicants; an obligation with
-- no queue gets honoured for the first three people and dropped at scale, so
-- it is tracked rather than remembered.
CREATE TABLE IF NOT EXISTS decisions (
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    decision TEXT NOT NULL,          -- accepted | declined
    decided_at TEXT NOT NULL,
    note TEXT DEFAULT '',
    feedback_sent_at TEXT DEFAULT '',
    PRIMARY KEY (program, handle)
);
CREATE INDEX IF NOT EXISTS idx_decisions_feedback ON decisions(feedback_sent_at);

-- Human sign-offs that acceptance depends on. Some programme gates are not
-- machine-checkable -- whether an access barrier was verified, whether the
-- build plan was reviewed, whether the Celo fit holds, whether a conflict was
-- cleared. Before this table, `accept --select` would take an arbitrary handle
-- with no scored application and none of these, and produce a full acceptance
-- letter. A judgement nobody signed is not a judgement, so each one is
-- recorded against a named steward and acceptance fails closed without them.
CREATE TABLE IF NOT EXISTS gate_signoffs (
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    gate TEXT NOT NULL,
    steward TEXT NOT NULL,
    note TEXT DEFAULT '',
    signed_at TEXT NOT NULL,
    PRIMARY KEY (program, handle, gate)
);

-- Overrides of a failed gate. Deliberately a separate table rather than a
-- column: an override is an event with an author and a reason, and it must be
-- as easy to audit as it was to perform.
CREATE TABLE IF NOT EXISTS gate_overrides (
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    gates TEXT NOT NULL,             -- comma-separated keys that were failing
    steward TEXT NOT NULL,
    reason TEXT NOT NULL,
    overridden_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_overrides_handle ON gate_overrides(program, handle);

-- Programme-level clearances. Legal clearance is tied to the exact terms digest
-- and hash; candidate-level overrides must never bypass it.
CREATE TABLE IF NOT EXISTS program_clearances (
    program TEXT NOT NULL,
    clearance_type TEXT NOT NULL,
    terms_digest TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    steward TEXT NOT NULL,
    note TEXT DEFAULT '',
    cleared_at TEXT NOT NULL,
    PRIMARY KEY (program, clearance_type, terms_digest)
);
CREATE INDEX IF NOT EXISTS idx_program_clearances ON program_clearances(program, clearance_type);

-- Smoke tests, known bad rows, and other records that must not enter selection
-- or reporting. Kept by handle because selection allocates funded seats by
-- handle, and a smoke-test handle must fail closed everywhere.
CREATE TABLE IF NOT EXISTS applicant_quarantine (
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (program, handle)
);

-- Public pledge lifecycle. The original EAS attestation is signed by the
-- builder, so close-out and revocation are events with their own UIDs/txs; they
-- are not mutable fields on the cohort row.
CREATE TABLE IF NOT EXISTS attestation_events (
    event_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    handle TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- initial | replacement | revocation
    uid TEXT NOT NULL,
    previous_uid TEXT DEFAULT '',
    signer TEXT DEFAULT '',
    months_funded INTEGER,
    tx_hash TEXT DEFAULT '',
    note TEXT DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attestation_events_handle ON attestation_events(program, handle);

-- What happens after acceptance. The engine scored people, accepted them, and
-- then had nothing to say about whether the money actually moved: receipts,
-- reimbursements, the month-two Celo result, months funded, vendor offsets,
-- Reserve returns and the final KPIs lived nowhere. For five people a shared
-- manual tracker is enough, but it needs a defined shape and a named owner per
-- entry, or "we track that" means whoever remembers.
--
-- Deliberately an append-only ledger of typed entries rather than a wide row
-- per recipient: the obligations arrive at different times, from different
-- people, and a correction should be visible as a correction.
CREATE TABLE IF NOT EXISTS operating_ledger (
    entry_id TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    handle TEXT DEFAULT '',          -- blank for programme-level entries
    entry_type TEXT NOT NULL,
    period TEXT DEFAULT '',          -- 'YYYY-MM' or a milestone key
    amount_usd REAL,                 -- NULL where the entry is not financial
    owner TEXT NOT NULL,             -- who is accountable for this line
    reference TEXT DEFAULT '',       -- receipt id, tx hash, post URL
    note TEXT DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_program ON operating_ledger(program, handle);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON operating_ledger(entry_type);

-- Contact details live here and ONLY here: never in a snapshot, never in a
-- score, never in a dossier. Joined to a submission by id when a human needs
-- to reach someone, and separable from everything publishable by dropping
-- this one table.
CREATE TABLE IF NOT EXISTS uid_counters (
    -- High-water mark per prefix. Separate from the uid table on purpose:
    -- deriving the next number from MAX(seq) hands the deleted row's number to
    -- the next applicant, which points an already-quoted reference at a
    -- different person. This only ever counts up.
    prefix TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applicant_uids (
    -- A human-facing reference for one applicant, in the same shape as the
    -- programme's other identifiers (PRE-S3-S-001). It is allocated once, on
    -- arrival, and stored -- never derived from position. A number computed
    -- from row order silently renumbers everyone below a deleted row, and a
    -- reference that has been read out in an email must not move.
    submission_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    seq INTEGER NOT NULL,
    assigned_at TEXT NOT NULL
);

-- Public profile detail for people the SCOUT found, who have not applied and
-- have accepted no terms. Deliberately not `contacts`: that table holds what
-- an applicant gave us under the terms they accepted, and the two must not be
-- mixed even though both describe how to reach a person. Everything here is
-- already published on the person's own GitHub profile.
CREATE TABLE IF NOT EXISTS profile_recon (
    handle TEXT PRIMARY KEY,
    x_handle TEXT DEFAULT '',
    x_source TEXT DEFAULT '',
    name TEXT DEFAULT '',
    blog TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    location TEXT DEFAULT '',
    socials TEXT DEFAULT '',
    email TEXT DEFAULT '',
    checked_at TEXT NOT NULL
);

-- The one line of a cold message that has to be true per person: which piece
-- of their public work brought them up, and through which scout channel. Kept
-- beside `profile_recon` rather than inside it because recon answers "where
-- can this person be reached" and this answers "what is there to say" -- and
-- because a repository goes stale on a different clock to a profile.
--
-- `sent_at` is written by the operator, not the pipeline. It exists so that a
-- second pass can leave alone anybody already contacted, which is the one
-- mistake in outreach that cannot be taken back.
CREATE TABLE IF NOT EXISTS outreach_hooks (
    handle TEXT PRIMARY KEY,
    repo TEXT DEFAULT '',
    repo_url TEXT DEFAULT '',
    repo_desc TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    channel_phrase TEXT DEFAULT '',
    hook TEXT DEFAULT '',
    built_at TEXT NOT NULL,
    sent_at TEXT DEFAULT ''
);

-- X-specific state, kept out of `outreach_hooks` because that table is about
-- what there is to say to somebody and this is about one channel's mechanics.
-- The numeric id is the thing X actually addresses; the handle is a display
-- name that its owner can change under you.
--
-- `dm_status` is the honest record of what happened: X will not tell you in
-- advance whether somebody accepts messages from strangers, so "undeliverable"
-- is a fact discovered by trying, and worth keeping so it is discovered once.
CREATE TABLE IF NOT EXISTS x_delivery (
    handle TEXT PRIMARY KEY,
    x_handle TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    resolved_at TEXT DEFAULT '',
    listed_at TEXT DEFAULT '',
    dm_at TEXT DEFAULT '',
    dm_status TEXT DEFAULT '',
    dm_detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS contacts (
    submission_id TEXT PRIMARY KEY,
    email TEXT DEFAULT '',
    name TEXT DEFAULT '',
    telegram TEXT DEFAULT '',
    x TEXT DEFAULT '',
    discord TEXT DEFAULT '',
    recorded_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = "talent_engine.db", *, shared: bool = False) -> None:
        """`shared=True` permits use from more than one thread.

        Only pass it when the caller serialises its own access — the HTTP
        intake path does, under a single lock, because ThreadingHTTPServer
        hands every request to a fresh thread and sqlite3 otherwise binds a
        connection to whichever thread opened it.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=not shared)
        # The contacts table holds applicant PII, so the quarantine has to be a
        # filesystem boundary as well as a schema convention. sqlite creates its
        # database and WAL with the process umask, which left them world
        # readable on the live host; every process on the box could read
        # applicant emails despite the code-level separation.
        self._restrict_permissions()
        self.conn.row_factory = sqlite3.Row
        # Intake and scoring hold separate connections to the same file, so a
        # writer must not lock the other out: WAL lets them overlap, and the
        # busy timeout turns the remaining contention into a wait instead of an
        # immediate "database is locked" that would drop a submission.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Small additive migrations for already-live SQLite databases."""
        cohort_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(cohort)").fetchall()
        }
        for name, ddl in {
            "payment_address": "ALTER TABLE cohort ADD COLUMN payment_address TEXT DEFAULT ''",
            "attestation_signer": "ALTER TABLE cohort ADD COLUMN attestation_signer TEXT DEFAULT ''",
        }.items():
            if name not in cohort_cols:
                self.conn.execute(ddl)

        recon_cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(profile_recon)").fetchall()
        }
        if recon_cols and "email" not in recon_cols:
            self.conn.execute("ALTER TABLE profile_recon ADD COLUMN email TEXT DEFAULT ''")
        if recon_cols and "socials" not in recon_cols:
            # Added once the first recon pass showed how many developers keep no
            # X account at all but do link a Bluesky, Mastodon or LinkedIn.
            self.conn.execute("ALTER TABLE profile_recon ADD COLUMN socials TEXT DEFAULT ''")

    def _restrict_permissions(self) -> None:
        """0600 on the database and its sidecars. Best effort, never fatal."""
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:  # a read-only mount or foreign owner must not stop a run
                pass

    # ------------------------------------------------------------------ runs

    def start_run(self, cfg: ProgramConfig, mode: str, code_version: str, note: str = "") -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO runs (run_id, program, mode, code_version, weights_digest, "
            "config_json, started_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                cfg.key,
                mode,
                code_version,
                cfg.weights_digest(),
                json.dumps(cfg.to_dict(), sort_keys=True),
                utc_now_iso(),
                note,
            ),
        )
        self.conn.commit()
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, program: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if program:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE program = ? ORDER BY started_at DESC LIMIT ?",
                (program, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- snapshots/scores

    def save_snapshot(self, snap: ProfileSnapshot) -> str:
        digest = snap.digest()
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots (digest, handle, collected_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (digest, snap.handle, snap.collected_at, json.dumps(snap.to_dict(), sort_keys=True)),
        )
        self.conn.commit()
        return digest

    def load_snapshot(self, digest: str) -> ProfileSnapshot | None:
        row = self.conn.execute(
            "SELECT payload FROM snapshots WHERE digest = ?", (digest,)
        ).fetchone()
        return _snapshot_from_dict(json.loads(row["payload"])) if row else None

    def save_recon(self, found: dict) -> None:
        """Record where a scouted candidate can be reached.

        Overwrites rather than accumulating: a profile is a current statement,
        and keeping yesterday's website would only invite someone to message a
        dead link.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO profile_recon "
            "(handle, x_handle, x_source, name, blog, bio, location, socials, email, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                found["handle"], found.get("x_handle", ""), found.get("x_source", ""),
                found.get("name", ""), found.get("blog", ""), found.get("bio", ""),
                found.get("location", ""), found.get("socials", ""),
                found.get("email", ""), utc_now_iso(),
            ),
        )
        self.conn.commit()

    def recon_for(self, handle: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM profile_recon WHERE handle = ?", (handle,)
        ).fetchone()
        return dict(row) if row else None

    def save_hook(self, found: dict) -> None:
        """Record what there is to say to a scouted candidate.

        Preserves `sent_at` across rebuilds. The hook is derived data and may
        be recomputed at any time; the fact that a human already sent a message
        is not, and losing it would mean messaging somebody twice.
        """
        prior = self.conn.execute(
            "SELECT sent_at FROM outreach_hooks WHERE handle = ?", (found["handle"],)
        ).fetchone()
        self.conn.execute(
            "INSERT OR REPLACE INTO outreach_hooks "
            "(handle, repo, repo_url, repo_desc, basis, channel_phrase, hook, "
            "built_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                found["handle"], found.get("repo", ""), found.get("repo_url", ""),
                found.get("repo_desc", ""), found.get("basis", ""),
                found.get("channel_phrase", ""), found.get("hook", ""),
                utc_now_iso(), (prior["sent_at"] if prior else "") or "",
            ),
        )
        self.conn.commit()

    def mark_contacted(self, handle: str, when: str = "") -> None:
        """Record that a human sent this person a message."""
        self.conn.execute(
            "UPDATE outreach_hooks SET sent_at = ? WHERE handle = ?",
            (when or utc_now_iso(), handle),
        )
        self.conn.commit()

    def hook_for_handle(self, handle: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM outreach_hooks WHERE handle = ?", (handle,)
        ).fetchone()
        return dict(row) if row else None

    def save_x_id(self, handle: str, x_handle: str, user_id: str) -> None:
        """Remember the numeric id X addresses this person by."""
        self.conn.execute(
            "INSERT INTO x_delivery (handle, x_handle, user_id, resolved_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(handle) DO UPDATE SET "
            "x_handle = excluded.x_handle, user_id = excluded.user_id, "
            "resolved_at = excluded.resolved_at",
            (handle, x_handle, user_id, utc_now_iso()),
        )
        self.conn.commit()

    def mark_listed(self, handle: str) -> None:
        self.conn.execute(
            "UPDATE x_delivery SET listed_at = ? WHERE handle = ?",
            (utc_now_iso(), handle),
        )
        self.conn.commit()

    def record_dm(self, handle: str, status: str, detail: str = "") -> None:
        """What happened when we tried. `status` is one of sent | refused | error.

        Recorded whatever the outcome, because the point of trying through the
        API rather than by hand is that failure is legible: a refusal means that
        person cannot be reached this way and should not be queued again.
        """
        self.conn.execute(
            "UPDATE x_delivery SET dm_at = ?, dm_status = ?, dm_detail = ? "
            "WHERE handle = ?",
            (utc_now_iso(), status, detail[:300], handle),
        )
        self.conn.commit()

    def x_delivery_for(self, handle: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM x_delivery WHERE handle = ?", (handle,)
        ).fetchone()
        return dict(row) if row else None

    def save_score(self, run_id: str, score: CandidateScore) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scores (run_id, handle, total, snapshot_digest, "
            "payload, scored_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                score.handle,
                score.total,
                score.snapshot_digest,
                json.dumps(score.to_dict(), sort_keys=True),
                score.scored_at,
            ),
        )
        self.conn.commit()

    def scores_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM scores WHERE run_id = ? ORDER BY total DESC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def score_for(self, run_id: str, handle: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM scores WHERE run_id = ? AND handle = ?", (run_id, handle)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ reproduce

    def replay(self, run_id: str, handle: str) -> CandidateScore:
        """Re-score a stored snapshot under the run's stored weights."""
        from ..scoring.engine import score_snapshot

        rec = self.score_for(run_id, handle)
        if not rec:
            raise KeyError(f"no score for {handle} in {run_id}")
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"no such run {run_id}")
        snap = self.load_snapshot(rec["snapshot_digest"])
        if snap is None:
            raise KeyError(f"snapshot {rec['snapshot_digest']} missing")
        cfg = ProgramConfig.from_dict(json.loads(run["config_json"]))
        return score_snapshot(snap, cfg)

    def verify(self, run_id: str, handle: str) -> dict[str, Any]:
        """Did the recorded score survive a replay? Reports drift, never hides it."""
        recorded = self.score_for(run_id, handle)
        if not recorded:
            raise KeyError(f"no score for {handle} in {run_id}")
        replayed = self.replay(run_id, handle)
        run = self.get_run(run_id) or {}
        matches = abs(replayed.total - recorded["total"]) < 1e-6
        return {
            "handle": handle,
            "run_id": run_id,
            "recorded_total": recorded["total"],
            "replayed_total": replayed.total,
            "matches": matches,
            "recorded_code_version": run.get("code_version"),
            "replay_code_version": replayed.code_version,
            "explanation": (
                "reproduced exactly"
                if matches
                else "scorer behaviour changed since this run; the recorded value "
                "stands as the score of record"
            ),
        }

    # ----------------------------------------------------------------- cohort

    def select_cohort(
        self, program: str, handles: Iterable[str], baseline_run_id: str,
        declared_repos: dict[str, str] | None = None,
    ) -> None:
        declared_repos = declared_repos or {}
        for h in handles:
            if self.is_quarantined(program, h):
                raise ValueError(f"{h} is quarantined and cannot be selected")
            self.conn.execute(
                "INSERT OR REPLACE INTO cohort (program, handle, declared_repo, "
                "baseline_run_id, selected_at) VALUES (?, ?, ?, ?, ?)",
                (program, h, declared_repos.get(h, ""), baseline_run_id, utc_now_iso()),
            )
        self.conn.commit()

    def record_acceptance(
        self,
        program: str,
        handle: str,
        *,
        payment_address: str = "",
        split_address: str = "",
        attestation_uid: str = "",
        attestation_signer: str = "",
        months_received: int | None = None,
    ) -> bool:
        """Attach acceptance artefacts to an existing cohort row.

        Kept for narrow tests and old callers. Real CLI acceptance uses
        `accept_candidate`, which validates and writes the decision/cohort row
        in one transaction.
        """
        row = self.conn.execute(
            "SELECT * FROM cohort WHERE program = ? AND handle = ?", (program, handle)
        ).fetchone()
        if not row:
            return False
        if not ((payment_address or split_address) and attestation_uid and attestation_signer):
            raise ValueError("acceptance requires payment address, attestation UID and signer")
        existing = dict(row)
        wanted_payment = payment_address or split_address
        if existing.get("accepted_at"):
            same = (
                existing.get("payment_address") == wanted_payment
                and existing.get("split_address") == (split_address or payment_address)
                and existing.get("attestation_uid") == attestation_uid
                and existing.get("attestation_signer") == attestation_signer
                and (
                    months_received is None
                    or int(existing.get("months_received") or 0) == months_received
                )
            )
            if same:
                return True
            raise ValueError("candidate is already accepted with different artefacts")
        sets, params = ["accepted_at = ?"], [utc_now_iso()]
        if payment_address or split_address:
            sets.append("payment_address = ?")
            params.append(wanted_payment)
            sets.append("split_address = ?")
            params.append(split_address or payment_address)
        if attestation_uid:
            sets.append("attestation_uid = ?")
            params.append(attestation_uid)
        if attestation_signer:
            sets.append("attestation_signer = ?")
            params.append(attestation_signer)
        if months_received is not None:
            sets.append("months_received = ?")
            params.append(months_received)
        params += [program, handle]
        self.conn.execute(
            f"UPDATE cohort SET {', '.join(sets)} WHERE program = ? AND handle = ?",
            params,
        )
        self.conn.commit()
        return True

    def accept_candidate(
        self,
        program: str,
        handle: str,
        *,
        selected: bool,
        baseline_run_id: str = "",
        declared_repo: str = "",
        payment_address: str,
        attestation_uid: str,
        attestation_signer: str,
        override: tuple[list[str], str, str] | None = None,
        capacity: int | None = None,
    ) -> bool:
        """Atomically select, accept, and attach required artefacts."""
        if not (payment_address and attestation_uid and attestation_signer):
            raise ValueError("acceptance requires payment_address, attestation_uid and attestation_signer")
        now = utc_now_iso()
        try:
            with self.conn:
                if self.is_quarantined(program, handle):
                    raise ValueError(f"{handle} is quarantined and cannot be accepted")
                existing_row = self.conn.execute(
                    "SELECT * FROM cohort WHERE program = ? AND handle = ?", (program, handle)
                ).fetchone()
                if existing_row and existing_row["accepted_at"]:
                    existing = dict(existing_row)
                    if (
                        existing.get("payment_address") == payment_address
                        and existing.get("split_address") == payment_address
                        and existing.get("attestation_uid") == attestation_uid
                        and existing.get("attestation_signer") == attestation_signer
                    ):
                        return True
                    raise ValueError("candidate is already accepted with different artefacts")
                if capacity is not None:
                    accepted_count = self.conn.execute(
                        "SELECT COUNT(*) FROM cohort WHERE program = ? AND accepted_at != ''",
                        (program,),
                    ).fetchone()[0]
                    if accepted_count >= capacity:
                        raise ValueError(
                            f"acceptance capacity reached for {program}: {accepted_count}/{capacity}"
                        )
                if selected:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO cohort (program, handle, declared_repo, "
                        "baseline_run_id, selected_at) VALUES (?, ?, ?, ?, ?)",
                        (program, handle, declared_repo, baseline_run_id, now),
                    )
                elif not existing_row:
                    return False
                if override:
                    gates, steward, reason = override
                    self.conn.execute(
                        "INSERT INTO gate_overrides (program, handle, gates, steward, reason, "
                        "overridden_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (program, handle, ",".join(gates), steward, reason, now),
                    )
                self.conn.execute(
                    "INSERT OR REPLACE INTO decisions (program, handle, decision, decided_at, "
                    "note, feedback_sent_at) VALUES (?, ?, 'accepted', ?, '', "
                    "COALESCE((SELECT feedback_sent_at FROM decisions WHERE program = ? "
                    "AND handle = ?), ''))",
                    (program, handle, now, program, handle),
                )
                self.conn.execute(
                    "UPDATE cohort SET accepted_at = ?, payment_address = ?, split_address = ?, "
                    "attestation_uid = ?, attestation_signer = ? WHERE program = ? AND handle = ?",
                    (
                        now,
                        payment_address,
                        payment_address,
                        attestation_uid,
                        attestation_signer,
                        program,
                        handle,
                    ),
                )
                self._record_attestation_event_uncommitted(
                    program,
                    handle,
                    "initial",
                    attestation_uid,
                    signer=attestation_signer,
                    months_funded=0,
                    note="initial mandatory public pledge",
                )
        except sqlite3.Error:
            raise
        return True

    def cohort(self, program: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM cohort WHERE program = ? ORDER BY handle", (program,)
        ).fetchall()
        return [dict(r) for r in rows]

    def record_program_clearance(
        self,
        program: str,
        clearance_type: str,
        terms_digest: str,
        terms_hash: str,
        steward: str,
        note: str = "",
    ) -> None:
        if not steward:
            raise ValueError("clearance requires a named steward")
        self.conn.execute(
            "INSERT INTO program_clearances (program, clearance_type, terms_digest, "
            "terms_hash, steward, note, cleared_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(program, clearance_type, terms_digest) DO UPDATE SET "
            "terms_hash = excluded.terms_hash, steward = excluded.steward, "
            "note = excluded.note, cleared_at = excluded.cleared_at",
            (
                program,
                clearance_type,
                terms_digest,
                terms_hash,
                steward,
                note,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def program_clearance(
        self,
        program: str,
        clearance_type: str,
        terms_digest: str,
        terms_hash: str = "",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM program_clearances WHERE program = ? AND clearance_type = ? "
            "AND terms_digest = ?",
            (program, clearance_type, terms_digest),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        if terms_hash and out.get("terms_hash", "").lower() != terms_hash.lower():
            return None
        return out

    def quarantine_applicant(self, program: str, handle: str, reason: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO applicant_quarantine (program, handle, reason, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (program, handle, reason, utc_now_iso()),
        )
        self.conn.commit()

    def is_quarantined(self, program: str, handle: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM applicant_quarantine WHERE program = ? AND handle = ?",
                (program, handle),
            ).fetchone()
        )

    def quarantine(self, program: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM applicant_quarantine WHERE program = ? ORDER BY handle", (program,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _record_attestation_event_uncommitted(
        self,
        program: str,
        handle: str,
        event_type: str,
        uid: str,
        *,
        previous_uid: str = "",
        signer: str = "",
        months_funded: int | None = None,
        tx_hash: str = "",
        note: str = "",
    ) -> str:
        event_id = f"{program}:{handle}:{event_type}:{uid}:{previous_uid or '-'}"
        self.conn.execute(
            "INSERT OR IGNORE INTO attestation_events (event_id, program, handle, event_type, uid, "
            "previous_uid, signer, months_funded, tx_hash, note, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                program,
                handle,
                event_type,
                uid,
                previous_uid,
                signer,
                months_funded,
                tx_hash,
                note,
                utc_now_iso(),
            ),
        )
        return event_id

    def record_attestation_event(
        self,
        program: str,
        handle: str,
        event_type: str,
        uid: str,
        *,
        previous_uid: str = "",
        signer: str = "",
        months_funded: int | None = None,
        tx_hash: str = "",
        note: str = "",
    ) -> str:
        if event_type not in {"initial", "replacement", "revocation"}:
            raise ValueError("unknown attestation event type")
        event_id = self._record_attestation_event_uncommitted(
            program,
            handle,
            event_type,
            uid,
            previous_uid=previous_uid,
            signer=signer,
            months_funded=months_funded,
            tx_hash=tx_hash,
            note=note,
        )
        self.conn.commit()
        return event_id

    def attestation_events(self, program: str, handle: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM attestation_events WHERE program = ? AND handle = ? "
            "ORDER BY recorded_at",
            (program, handle),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_closeout_replacement(
        self,
        program: str,
        handle: str,
        *,
        owner: str,
        months_funded: int,
        replacement_uid: str,
        original_uid: str,
        signer: str,
        note: str = "",
    ) -> tuple[str, bool]:
        """Record the replacement UID once. Returns (event_id, created)."""
        if not owner:
            raise ValueError("close-out needs an owner")
        if months_funded < 0:
            raise ValueError("months_funded cannot be negative")
        existing = self.conn.execute(
            "SELECT * FROM attestation_events WHERE program = ? AND handle = ? "
            "AND event_type = 'replacement' AND previous_uid = ? ORDER BY recorded_at LIMIT 1",
            (program, handle, original_uid),
        ).fetchone()
        if existing:
            row = dict(existing)
            if (
                row.get("uid") != replacement_uid
                or int(row.get("months_funded") or 0) != months_funded
                or (signer and row.get("signer") and row.get("signer") != signer)
            ):
                raise ValueError("close-out replacement already recorded with different details")
            return row["event_id"], False

        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE cohort SET months_received = ? WHERE program = ? AND handle = ?",
                (months_funded, program, handle),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO operating_ledger (entry_id, program, handle, "
                "entry_type, period, amount_usd, owner, reference, note, recorded_at) "
                "VALUES (?, ?, ?, 'months_funded', '', NULL, ?, ?, ?, ?)",
                (
                    f"{program}:months_funded:{handle}:{original_uid}",
                    program,
                    handle,
                    owner,
                    replacement_uid,
                    str(months_funded),
                    now,
                ),
            )
            event_id = self._record_attestation_event_uncommitted(
                program,
                handle,
                "replacement",
                replacement_uid,
                previous_uid=original_uid,
                signer=signer,
                months_funded=months_funded,
                note=note or "builder-signed close-out replacement",
            )
        return event_id, True

    def closeout_replacement_for(
        self, program: str, handle: str, original_uid: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM attestation_events WHERE program = ? AND handle = ? "
            "AND event_type = 'replacement' AND previous_uid = ? ORDER BY recorded_at LIMIT 1",
            (program, handle, original_uid),
        ).fetchone()
        return dict(row) if row else None

    def record_closeout_revocation(
        self,
        program: str,
        handle: str,
        *,
        owner: str,
        original_uid: str,
        replacement_uid: str,
        signer: str,
        revocation_tx: str,
        months_funded: int | None = None,
        note: str = "",
    ) -> tuple[str, bool]:
        """Record original UID revocation once. Returns (event_id, created)."""
        if not owner:
            raise ValueError("close-out needs an owner")
        if not revocation_tx:
            raise ValueError("revocation transaction is required")
        replacement = self.closeout_replacement_for(program, handle, original_uid)
        if not replacement or replacement.get("uid") != replacement_uid:
            raise ValueError("record the matching close-out replacement before revocation")
        existing = self.conn.execute(
            "SELECT * FROM attestation_events WHERE program = ? AND handle = ? "
            "AND event_type = 'revocation' AND uid = ? ORDER BY recorded_at LIMIT 1",
            (program, handle, original_uid),
        ).fetchone()
        if existing:
            row = dict(existing)
            if row.get("tx_hash") != revocation_tx:
                raise ValueError("close-out revocation already recorded with a different tx")
            return row["event_id"], False
        with self.conn:
            event_id = self._record_attestation_event_uncommitted(
                program,
                handle,
                "revocation",
                original_uid,
                previous_uid=replacement_uid,
                signer=signer,
                months_funded=months_funded,
                tx_hash=revocation_tx,
                note=note or "builder revoked superseded attestation",
            )
        return event_id, True

    # ------------------------------------------------------------ submissions

    def record_submission(
        self,
        submission_id: str,
        program: str,
        source: str,
        handle: str,
        raw_handle: str,
        application: Application,
        form_id: str = "",
        status: str = "queued",
    ) -> bool:
        """Insert a submission. Returns False if this id was already seen.

        Idempotency is the point: form platforms retry webhooks on any non-2xx
        and sometimes on a slow 2xx, and a retry must not produce a second
        score, a second API spend, or a duplicate row in the ranking.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO submissions (submission_id, program, source, form_id, "
            "handle, raw_handle, application_json, received_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                program,
                source,
                form_id,
                handle,
                raw_handle,
                json.dumps(asdict(application), sort_keys=True),
                utc_now_iso(),
                status,
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def assign_uid(self, submission_id: str, prefix: str, width: int = 3) -> str:
        """Return this submission's reference, allocating one on first sight.

        Idempotent: a webhook retry, a requeue or a second read all get the
        same string back. Sequence numbers are per prefix, so changing the
        season starts a new run of numbers rather than continuing the old one.
        """
        row = self.conn.execute(
            "SELECT uid FROM applicant_uids WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        if row:
            return row["uid"]

        counter = self.conn.execute(
            "SELECT next_seq FROM uid_counters WHERE prefix = ?", (prefix,)
        ).fetchone()
        if counter is None:
            # First use of this prefix. Seed above anything already issued, so
            # adopting the counter on a live database cannot re-issue a
            # reference that has already gone out.
            issued = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM applicant_uids WHERE uid LIKE ?",
                (f"{prefix}%",),
            ).fetchone()[0]
            seq = int(issued) + 1
        else:
            seq = int(counter["next_seq"])
        self.conn.execute(
            "INSERT INTO uid_counters (prefix, next_seq) VALUES (?, ?) "
            "ON CONFLICT(prefix) DO UPDATE SET next_seq = excluded.next_seq",
            (prefix, seq + 1),
        )
        uid = f"{prefix}{seq:0{width}d}"
        self.conn.execute(
            "INSERT INTO applicant_uids (submission_id, uid, seq, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (submission_id, uid, seq, utc_now_iso()),
        )
        self.conn.commit()
        return uid

    def uid_for(self, submission_id: str) -> str:
        row = self.conn.execute(
            "SELECT uid FROM applicant_uids WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        return row["uid"] if row else ""

    def record_contact(self, submission_id: str, contact: Any) -> None:
        """Store contact details in the quarantined table.

        Takes the dataclass rather than a dict so a caller cannot casually pass
        the whole form payload in here.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO contacts (submission_id, email, name, telegram, x, "
            "discord, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                getattr(contact, "email", ""),
                getattr(contact, "name", ""),
                getattr(contact, "telegram", ""),
                getattr(contact, "x", ""),
                getattr(contact, "discord", ""),
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def finish_submission(
        self, submission_id: str, status: str, run_id: str = "", total: float | None = None,
        error: str = "", concerns: str = "",
    ) -> None:
        """Record the outcome. `concerns` travels with the number by design.

        A score stored on its own gets read as a verdict; the caveat sentence
        is stored alongside it so no consumer of this table can show one
        without the other.
        """
        self.conn.execute(
            "UPDATE submissions SET status = ?, run_id = ?, total = ?, concerns = ?, "
            "error = ? WHERE submission_id = ?",
            (status, run_id, total, concerns, error[:500], submission_id),
        )
        self.conn.commit()

    def pending_submissions(self, program: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM submissions WHERE status = 'queued'"
        params: list[Any] = []
        if program:
            sql += " AND program = ?"
            params.append(program)
        sql += " ORDER BY received_at"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def submissions(
        self,
        program: str | None = None,
        limit: int = 100,
        *,
        include_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM submissions"
        params: list[Any] = []
        where: list[str] = []
        if program:
            where.append("program = ?")
            params.append(program)
        if not include_quarantined:
            where.append(
                "NOT EXISTS (SELECT 1 FROM applicant_quarantine q "
                "WHERE q.program = submissions.program AND q.handle = submissions.handle)"
            )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def shortlist(self, program: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Deterministic latest-score shortlist across the whole applicant pool."""
        rows = self.conn.execute(
            "SELECT s.* FROM submissions s "
            "WHERE s.program = ? AND s.status = 'scored' "
            "AND NOT EXISTS (SELECT 1 FROM applicant_quarantine q "
            "WHERE q.program = s.program AND q.handle = s.handle) "
            "ORDER BY s.handle ASC, s.received_at DESC, s.submission_id DESC",
            (program,),
        ).fetchall()
        latest_by_handle: dict[str, dict[str, Any]] = {}
        for row in rows:
            handle = row["handle"]
            if handle not in latest_by_handle:
                latest_by_handle[handle] = dict(row)
        out = sorted(
            latest_by_handle.values(),
            key=lambda r: (-(r["total"] or 0), r["handle"]),
        )
        return out[:limit] if limit else out

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None

    def latest_snapshots(self, program: str) -> dict[str, ProfileSnapshot]:
        """Most recent snapshot per handle across every run of a program.

        Ring detection is a question about the applicant *pool*, not about any
        single run, so it reads across runs — someone who applied in March and
        someone who applied in August can still be the same person.
        """
        rows = self.conn.execute(
            "SELECT s.handle, s.snapshot_digest, s.scored_at FROM scores s "
            "JOIN runs r ON r.run_id = s.run_id WHERE r.program = ? "
            "AND NOT EXISTS (SELECT 1 FROM applicant_quarantine q "
            "WHERE q.program = r.program AND q.handle = s.handle) "
            "ORDER BY s.scored_at",
            (program,),
        ).fetchall()
        latest: dict[str, str] = {}
        for row in rows:
            latest[row["handle"]] = row["snapshot_digest"]  # later rows win
        out: dict[str, ProfileSnapshot] = {}
        for handle, digest in latest.items():
            snap = self.load_snapshot(digest)
            if snap is not None:
                out[handle] = snap
        return out

    # ------------------------------------------------------------- decisions

    def record_decision(
        self, program: str, handle: str, decision: str, note: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO decisions (program, handle, decision, decided_at, "
            "note, feedback_sent_at) VALUES (?, ?, ?, ?, ?, "
            "COALESCE((SELECT feedback_sent_at FROM decisions WHERE program = ? "
            "AND handle = ?), ''))",
            (program, handle, decision, utc_now_iso(), note, program, handle),
        )
        self.conn.commit()

    def mark_feedback_sent(self, program: str, handle: str) -> None:
        self.conn.execute(
            "UPDATE decisions SET feedback_sent_at = ? WHERE program = ? AND handle = ?",
            (utc_now_iso(), program, handle),
        )
        self.conn.commit()

    def pending_feedback(self, program: str) -> list[dict[str, Any]]:
        """Declined applicants who have not yet been told anything."""
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE program = ? AND decision = 'declined' "
            "AND feedback_sent_at = '' ORDER BY decided_at",
            (program,),
        ).fetchall()
        return [dict(r) for r in rows]

    def decisions(self, program: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE program = ? ORDER BY decided_at DESC",
            (program,),
        ).fetchall()
        return [dict(r) for r in rows]

    def contact_for(self, submission_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------- rehydration


def _snapshot_from_dict(data: dict[str, Any]) -> ProfileSnapshot:
    app = Application(**data.get("application", {}))
    return ProfileSnapshot(
        handle=data["handle"],
        account_created_at=data.get("account_created_at"),
        collected_at=data["collected_at"],
        window_start=data["window_start"],
        window_end=data["window_end"],
        repos=[RepoActivity(**r) for r in data.get("repos", [])],
        merged_prs=[PullRequestActivity(**p) for p in data.get("merged_prs", [])],
        reviews=[ReviewActivity(**r) for r in data.get("reviews", [])],
        active_weeks=data.get("active_weeks", []),
        application=app,
        collection_notes=data.get("collection_notes", []),
        partial=data.get("partial", False),
    )


# --------------------------------------------------------------------------
# Acceptance gates
# --------------------------------------------------------------------------


def _gate_methods():  # pragma: no cover - wiring only
    """Attached below; kept out of the class body for readability."""


def record_signoff(
    self,
    program: str,
    handle: str,
    gate: str,
    steward: str,
    note: str = "",
) -> None:
    """Record that a named person checked a gate. Idempotent per gate."""
    self.conn.execute(
        "INSERT INTO gate_signoffs (program, handle, gate, steward, note, signed_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(program, handle, gate) DO UPDATE SET "
        "steward = excluded.steward, note = excluded.note, signed_at = excluded.signed_at",
        (program, handle, gate, steward, note, utc_now_iso()),
    )
    self.conn.commit()


def signoffs(self, program: str, handle: str) -> dict[str, dict[str, Any]]:
    rows = self.conn.execute(
        "SELECT gate, steward, note, signed_at FROM gate_signoffs "
        "WHERE program = ? AND handle = ?",
        (program, handle),
    ).fetchall()
    return {r["gate"]: dict(r) for r in rows}


def record_override(
    self, program: str, handle: str, gates: list[str], steward: str, reason: str
) -> None:
    self.conn.execute(
        "INSERT INTO gate_overrides (program, handle, gates, steward, reason, "
        "overridden_at) VALUES (?, ?, ?, ?, ?, ?)",
        (program, handle, ",".join(sorted(gates)), steward, reason, utc_now_iso()),
    )
    self.conn.commit()


def overrides(self, program: str, handle: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM gate_overrides WHERE program = ?"
    params: list[Any] = [program]
    if handle:
        sql += " AND handle = ?"
        params.append(handle)
    return [dict(r) for r in self.conn.execute(sql + " ORDER BY overridden_at", params)]


def has_scored_application(self, program: str, handle: str) -> bool:
    """Did this person actually apply and get scored under this programme?"""
    if self.is_quarantined(program, handle):
        return False
    row = self.conn.execute(
        "SELECT 1 FROM submissions WHERE program = ? AND handle = ? "
        "AND status = 'scored' LIMIT 1",
        (program, handle),
    ).fetchone()
    return bool(row)


def latest_application(self, program: str, handle: str) -> dict[str, Any] | None:
    row = self.conn.execute(
        "SELECT application_json, received_at FROM submissions WHERE program = ? "
        "AND handle = ? ORDER BY received_at DESC LIMIT 1",
        (program, handle),
    ).fetchone()
    return dict(row) if row else None


for _fn in (
    record_signoff,
    signoffs,
    record_override,
    overrides,
    has_scored_application,
    latest_application,
):
    setattr(Store, _fn.__name__, _fn)


# --------------------------------------------------------------------------
# Operating ledger
# --------------------------------------------------------------------------

# The obligations a programme takes on once it accepts someone. Named here so
# that "what do we owe and what have we done" is answerable from the database
# rather than from memory.
LEDGER_TYPES = (
    "receipt",           # a tooling invoice the recipient paid
    "reimbursement",     # money we sent back to them
    "vendor_offset",     # credit or discount a vendor gave us instead of cash
    "reserve_return",    # give-back income actually received
    "public_update",     # the public record we promised, with a URL
    "celo_checkpoint",   # the month-two Celo result
    "months_funded",     # how many months this person actually took
    "kpi",               # a final outcome measure
)


def record_ledger_entry(
    self,
    program: str,
    entry_type: str,
    owner: str,
    *,
    handle: str = "",
    period: str = "",
    amount_usd: float | None = None,
    reference: str = "",
    note: str = "",
) -> str:
    """Append one operating entry. Returns its id.

    `owner` is required and not defaulted: an obligation with no named person
    behind it is the thing this table exists to prevent.
    """
    if entry_type not in LEDGER_TYPES:
        raise ValueError(f"unknown entry type {entry_type!r}; expected one of {LEDGER_TYPES}")
    if not owner:
        raise ValueError("every ledger entry needs an owner")
    entry_id = f"{program}:{entry_type}:{handle or '-'}:{period or '-'}:{utc_now_iso()}"
    self.conn.execute(
        "INSERT INTO operating_ledger (entry_id, program, handle, entry_type, period, "
        "amount_usd, owner, reference, note, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry_id, program, handle, entry_type, period,
            amount_usd, owner, reference, note, utc_now_iso(),
        ),
    )
    self.conn.commit()
    return entry_id


def ledger(
    self, program: str, handle: str = "", entry_type: str = ""
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM operating_ledger WHERE program = ?"
    params: list[Any] = [program]
    if handle:
        sql += " AND handle = ?"
        params.append(handle)
    if entry_type:
        sql += " AND entry_type = ?"
        params.append(entry_type)
    return [dict(r) for r in self.conn.execute(sql + " ORDER BY recorded_at", params)]


def programme_periods(duration_months: int, start: str) -> list[str]:
    """The 'YYYY-MM' keys a term of this length covers, from its start month."""
    if not start or duration_months <= 0:
        return []
    if len(start) != 7 or start[4] != "-":
        raise ValueError("programme start must be YYYY-MM")
    year, month = int(start[:4]), int(start[5:7])
    if not 1 <= month <= 12:
        raise ValueError("programme start month must be 01..12")
    out = []
    for _ in range(duration_months):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def ledger_summary(
    self, program: str, *, periods: list[str] | None = None
) -> dict[str, Any]:
    """Per-recipient totals and what is still missing.

    The 'missing' list is the useful part: it is the difference between a
    tracker and a filing cabinet. It is period-aware on purpose. Checking only
    that a type had *ever* been recorded meant one receipt in month one
    satisfied the tracker for the whole four-month term, and `public_update`
    -- a monthly obligation the policy actually commits to -- was not checked
    at all. Absence in month three is exactly what this is for.
    """
    rows = self.ledger(program)
    people = sorted(
        {r["handle"] for r in rows if r["handle"]}
        | {m["handle"] for m in self.cohort(program)}
    )
    periods = periods or []
    out: dict[str, Any] = {
        "program": program,
        "periods": periods,
        "recipients": {},
        "totals": {},
    }
    for t in LEDGER_TYPES:
        total = sum(r["amount_usd"] or 0 for r in rows if r["entry_type"] == t)
        if total:
            out["totals"][t] = round(total, 2)

    # Once per term.
    ONCE = ("celo_checkpoint", "months_funded", "kpi")
    # Once per programme month.
    MONTHLY = ("receipt", "reimbursement", "public_update")

    for h in people:
        mine = [r for r in rows if r["handle"] == h]
        seen = {r["entry_type"] for r in mine}
        by_period = {(r["entry_type"], r["period"]) for r in mine}
        missing = [t for t in ONCE if t not in seen]
        for t in MONTHLY:
            gaps = [p for p in periods if (t, p) not in by_period]
            if not periods and t not in seen:
                missing.append(t)
            missing.extend(f"{t}:{p}" for p in gaps)
        out["recipients"][h] = {
            "entries": len(mine),
            "reimbursed_usd": round(
                sum(r["amount_usd"] or 0 for r in mine if r["entry_type"] == "reimbursement"), 2
            ),
            "missing": missing,
            "owners": sorted({r["owner"] for r in mine}),
        }
    return out


for _fn in (record_ledger_entry, ledger, ledger_summary):
    setattr(Store, _fn.__name__, _fn)
