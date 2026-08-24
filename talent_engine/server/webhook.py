"""Form-webhook intake, on the standard library only.

Shape of the thing: the HTTP handler does no network work. It authenticates
the request, parses it, writes the submission down, and returns. Scoring —
which means dozens of GitHub calls and can take tens of seconds — happens on a
worker thread. Tally waits a few seconds for a webhook response and retries on
anything that is not a 2xx, so scoring inline would produce timeouts, retries,
duplicate scoring runs, and a doubled API spend on every applicant.

Durability comes from the store, not the queue: a submission is committed to
SQLite with status `queued` before it is handed to the worker, so a crash mid
scoring loses no applicant. `--drain` re-runs whatever was left queued.

Security posture:
  * Signature verification is mandatory. There is no "no secret configured, so
    accept everything" path — an open scoring endpoint is an open GitHub-API
    spend and an open way to inject applicants into a ranking.
  * The endpoint returns 202 for anything it accepted and a bare status code
    otherwise. It never echoes payload content back, which keeps it useless as
    an oracle for probing what the parser understood.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..config import ProgramConfig
from ..github.collector import Collector
from ..ingest.tally import Submission, TallyPayloadError, parse_webhook, verify_signature
from ..model import Application, utc_now_iso
from ..modes.rings import find_clusters
from ..notify import application_scored
from ..scoring.concerns import concerns
from ..scoring.engine import CODE_VERSION, score_snapshot
from ..store.db import Store
from . import outreach_feed, scores_feed, scouted_feed

log = logging.getLogger("talent_engine.intake")

MAX_BODY = 1 << 20  # 1 MiB; real form responses are a few KiB


class IntakeService:
    """Accepts submissions, scores them off the request path.

    Not thread-safe by accident: the single worker thread owns the Store and
    the Collector, and the HTTP threads talk to it only through `accept()`,
    which takes the store lock for its one write. sqlite3 connections are not
    shareable across threads, so the handler gets its own.
    """

    def __init__(
        self,
        cfg: ProgramConfig,
        db_path: str,
        collector_factory,
        source: str = "tally",
        overlay=None,
    ) -> None:
        self.cfg = cfg
        # Used only to stamp which version of the terms an applicant accepted.
        self.overlay = overlay
        self.db_path = db_path
        self.collector_factory = collector_factory
        self.source = source
        self._q: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        # sqlite3 connections are bound to the thread that created them, so the
        # worker opens its own inside `_run` rather than inheriting this one.
        self._store: Store | None = None
        # ThreadingHTTPServer serves each request on a new thread, so the
        # intake connection is shared and every use of it is taken under
        # `self._lock`.
        self._intake_store = Store(db_path, shared=True)
        self._run_ids: dict[str, str] = {}  # utc date -> run_id
        # Scoring failures are usually transient (GitHub outage, expired token),
        # and nothing outside will retry once Tally has its 202.
        self._attempts: dict[str, int] = {}
        self.max_attempts = 3
        self._worker: threading.Thread | None = None
        self.accepted = 0
        self.duplicates = 0
        self.rejected = 0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="scorer", daemon=True)
        self._worker.start()

    def stop(self, drain: bool = True) -> None:
        if not self._worker:
            return
        if drain:
            self._q.join()
        self._q.put(None)
        self._worker.join(timeout=30)

    # ---------------------------------------------------------------- intake

    def accept(self, sub: Submission) -> str:
        """Record a parsed submission and queue it. Returns a disposition."""
        if not sub.submission_id:
            self.rejected += 1
            return "no-submission-id"

        # WHICH terms were accepted has to come from the applicant, not from
        # us. Stamping `overlay.terms_digest()` here recorded whatever the
        # server considered current at the moment the webhook arrived — so a
        # stale form, cached embed or queued retry was recorded as acceptance
        # of words the applicant never saw, and the acceptance gate then
        # certified it as current.
        #
        # The form carries the digest it was rendered with. If it is absent we
        # leave the version empty rather than inventing one: the gate treats an
        # unknown version as not-current and fails closed, which is the correct
        # reading of "we do not know what they agreed to".
        sub.application.accepted_terms_version = (
            sub.application.accepted_terms_version or ""
        ).strip()

        with self._lock:
            fresh = self._intake_store.record_submission(
                submission_id=sub.submission_id,
                program=self.cfg.key,
                source=self.source,
                handle=sub.handle,
                raw_handle=sub.raw_handle,
                application=sub.application,
                form_id=sub.form_id,
                status="queued" if sub.ok else "unparsable",
            )
            if fresh and self.cfg.uid_prefix:
                # On arrival, so the reference exists even for a submission
                # that never scores. The two applicants stranded on launch day
                # still need something a steward can call them by.
                self._intake_store.assign_uid(sub.submission_id, self.cfg.uid_prefix)
            if fresh and not sub.contact.is_empty():
                self._intake_store.record_contact(sub.submission_id, sub.contact)
            if fresh and not sub.ok:
                self._intake_store.finish_submission(
                    sub.submission_id,
                    "unparsable",
                    error=f"no usable GitHub handle in {sub.raw_handle!r}",
                )

        if not fresh:
            self.duplicates += 1
            return "duplicate"
        if not sub.ok:
            # Deliberately still a 202: the response is stored and flagged for a
            # human, not dropped. Returning an error would make Tally retry a
            # submission that will never parse.
            self.rejected += 1
            return "unparsable"

        self.accepted += 1
        self._q.put(sub.submission_id)
        return "queued"

    def backlog(self) -> int:
        return self._q.qsize()

    def requeue_pending(self) -> int:
        """Push anything left in `queued` back onto the worker queue."""
        with self._lock:
            pending = self._intake_store.pending_submissions(self.cfg.key)
        for row in pending:
            self._q.put(row["submission_id"])
        return len(pending)

    # ---------------------------------------------------------------- worker

    def _run(self) -> None:
        self._store = Store(self.db_path)  # must be opened on this thread
        try:
            while True:
                item = self._q.get()
                if item is None:
                    self._q.task_done()
                    return
                try:
                    # A fresh collector per submission. The client carries a
                    # finite request budget, and reusing one across the
                    # service's whole uptime meant that after enough applicants
                    # the budget was spent and every subsequent person scored
                    # near zero — silently, permanently, until somebody thought
                    # to restart the service.
                    self._score_one(item, self.collector_factory())
                except Exception as exc:  # a bad applicant must not kill intake
                    log.exception("scoring %s failed", item)
                    # A transient failure — GitHub down, token expired, rate
                    # limit exhausted — must not strand a real applicant.
                    # Tally already received its 202, so nothing external will
                    # ever retry; the row stays `queued` and a restart or the
                    # next drain picks it up. Only a submission that has failed
                    # repeatedly is parked as `error` for a human.
                    attempts = self._attempts.get(item, 0) + 1
                    self._attempts[item] = attempts
                    terminal = attempts >= self.max_attempts
                    try:
                        self._store.finish_submission(
                            item,
                            "error" if terminal else "queued",
                            error=f"attempt {attempts}: {exc!r}",
                        )
                    except Exception:
                        log.exception("could not record failure for %s", item)
                    if terminal:
                        log.error(
                            "%s failed %d times; parked for a human", item, attempts
                        )
                finally:
                    self._q.task_done()
        finally:
            self._store.close()

    def _score_one(self, submission_id: str, collector: Collector) -> None:
        assert self._store is not None  # only ever called on the worker thread
        row = self._store.get_submission(submission_id)
        if not row:
            log.warning("submission %s vanished before scoring", submission_id)
            return
        app = Application(**json.loads(row["application_json"]))

        run_id = self._run_for_today()
        snap = collector.collect(row["handle"], app)
        self._store.save_snapshot(snap)
        if snap.partial and any("budget" in n for n in snap.collection_notes):
            # An incomplete snapshot is a floor, not a measurement. Recording
            # it as a score would put a misleadingly low number in front of a
            # human with only a soft flag to contradict it.
            raise RuntimeError(
                "collection incomplete (request budget exhausted); not scoring a "
                "partial snapshot"
            )

        score = score_snapshot(snap, self.cfg)
        self._store.save_score(run_id, score)
        caveat = concerns(score, self.cfg, snap)

        # The pool-level check, run on arrival rather than on demand. It costs
        # no API calls (it reads stored snapshots), and a ring is most useful
        # to know about while the person is still an applicant.
        ring_note = self._ring_note(row["handle"])
        if ring_note:
            caveat = f"{caveat} {ring_note}"
        self._store.finish_submission(
            submission_id, "scored", run_id=run_id, total=score.total, concerns=caveat
        )
        log.info("scored %s: %.2f (run %s) — %s", row["handle"], score.total, run_id, caveat)

        # Push, because nobody watches a table. Contact details deliberately
        # stay on this box — see notify.py.
        application_scored(
            handle=row["handle"],
            total=score.total,
            caveat=caveat,
            program=self.cfg.key,
            declared_repo=app.declared_repo,
            max_points=sum(self.cfg.weights.values()),
            contact=self._store.contact_for(submission_id),
        )

    def _ring_note(self, handle: str) -> str:
        """One line if this applicant sits in an inward-facing cluster."""
        assert self._store is not None
        try:
            snapshots = self._store.latest_snapshots(self.cfg.key)
        except Exception:
            log.exception("ring check failed; continuing without it")
            return ""
        known = set(self.cfg.ecosystem.orgs) | set(self.cfg.frontier.orgs)
        for cluster in find_clusters(snapshots, known):
            if handle in cluster.members and cluster.needs_review:
                return cluster.summary()
        return ""

    def _run_for_today(self) -> str:
        """One run per UTC day, so a rolling intake still groups into cohorts.

        A single run for the server's whole lifetime would make `measure` and
        the ranked table meaningless after a few weeks; a run per submission
        would make them meaningless immediately.
        """
        assert self._store is not None
        day = utc_now_iso()[:10]
        if day not in self._run_ids:
            self._run_ids[day] = self._store.start_run(
                self.cfg, "intake", CODE_VERSION, note=f"{self.source} intake {day}"
            )
        return self._run_ids[day]


def build_handler(service: IntakeService, secret: str, pages: dict[str, tuple[str, bytes]]):
    # The stewards' spreadsheet lives inside the Prezenti workspace and cannot
    # be shared outward, so the scores travel to it instead: a tab running
    # =IMPORTDATA(<this url>) pulls them on Google's schedule. IMPORTDATA
    # fetches anonymously, so the token in the path is the only thing between
    # this and the open web -- unset, the route does not exist at all.
    feed_token = os.environ.get("SCORES_FEED_TOKEN", "").strip()
    feed_path = f"/scores/{feed_token}.csv" if feed_token else None

    # The applicant board, same trick for a different reason. Published as an
    # artefact it is a photograph: correct the moment it is taken and wrong by
    # the next morning, because nothing on a schedule can republish it. Served
    # here it is a window -- cron rewrites the file every fifteen minutes and
    # the URL never changes. Contacts are deliberately absent from the served
    # copy; they are in the Tally tab of the stewards' own sheet, which does
    # not need a second home on a URL that anyone holding it can open.
    # The scout's own leads, beside the applicants'. Same access model; a
    # separate token so that revoking one does not take the other down with it.
    scout_token = os.environ.get("SCOUT_FEED_TOKEN", "").strip()
    scout_path = f"/scouted/{scout_token}.csv" if scout_token else None
    outreach_token = os.environ.get("OUTREACH_FEED_TOKEN", "").strip()
    outreach_path = f"/outreach/{outreach_token}.csv" if outreach_token else None

    board_token = os.environ.get("BOARD_TOKEN", "").strip()
    board_path = f"/board/{board_token}.html" if board_token else None
    board_file = os.environ.get("BOARD_HTML", "").strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "talent-engine"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # route through logging
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, content_type: str, payload: bytes,
                  no_store: bool = False, csp: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if no_store:
                # Cloudflare caches by extension, and it cached a .csv 404 for
                # four hours the first time this route was requested. A feed a
                # spreadsheet pulls has to answer with today's scores, not a
                # copy from lunchtime, so it opts out at the edge explicitly.
                self.send_header("Cache-Control", "no-store, max-age=0")
            # The page embeds Tally in an iframe and loads nothing else, so the
            # policy can be this tight: no scripts of our own, no other origins.
            self.send_header(
                "Content-Security-Policy",
                csp or
                # data: for the embedded brand fonts, which are inlined
                # rather than fetched so the page makes no external request.
                "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
                "img-src data:; frame-src https://tally.so; form-action 'none'; "
                # Nobody frames this page. It carries an organisation's name and
                # an application form, which is exactly what a clickjacker or a
                # lookalike site would want to wrap.
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if payload and self.command != "HEAD":
                self.wfile.write(payload)

        def _plain(self, code: int, body: str = "") -> None:
            self._send(code, "text/plain; charset=utf-8", body.encode())

        def do_GET(self) -> None:  # noqa: N802
            # Exact-match lookup only. No path is ever joined to a filesystem
            # root here, so traversal is not a thing that can be attempted.
            path = self.path.split("?", 1)[0]
            if path.rstrip("/") in ("/healthz", "/health"):
                self._plain(200, "ok\n")
                return
            if feed_path and hmac.compare_digest(path, feed_path):
                try:
                    body = scores_feed.csv_for(service.db_path).encode()
                except sqlite3.Error:
                    log.exception("scores feed could not read the database")
                    self._plain(503, "scores unavailable\n")
                    return
                self._send(200, "text/csv; charset=utf-8", body, no_store=True)
                return
            if scout_path and hmac.compare_digest(path, scout_path):
                try:
                    body = scouted_feed.csv_for(service.db_path, service.cfg.key).encode()
                except sqlite3.Error:
                    log.exception("scout feed could not read the database")
                    self._plain(503, "leads unavailable\n")
                    return
                self._send(200, "text/csv; charset=utf-8", body, no_store=True)
                return
            if outreach_path and hmac.compare_digest(path, outreach_path):
                try:
                    body = outreach_feed.csv_for(
                        service.db_path, service.cfg.key
                    ).encode()
                except sqlite3.Error:
                    log.exception("outreach feed could not read the database")
                    self._plain(503, "outreach unavailable\n")
                    return
                self._send(200, "text/csv; charset=utf-8", body, no_store=True)
                return
            if board_path and board_file and hmac.compare_digest(path, board_path):
                try:
                    with open(board_file, "rb") as fh:
                        body = fh.read()
                except OSError:
                    # The file is written by cron, not by this process, so a
                    # missing one means the rebuild failed -- say so rather
                    # than serving a stale copy or an empty page.
                    log.exception("board file could not be read: %s", board_file)
                    self._plain(503, "board unavailable\n")
                    return
                self._send(
                    200, "text/html; charset=utf-8", body, no_store=True,
                    # The board is the one page here that reaches outside for
                    # anything: the brand typefaces come from Google Fonts.
                    csp=("default-src 'none'; "
                         "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; img-src data:; "
                         "base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'none'"),
                )
                return
            page = pages.get(path) or (pages.get(path.rstrip("/")) if path != "/" else None)
            if page:
                self._send(200, page[0], page[1])
            else:
                self._plain(404)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in ("/webhook/tally", "/webhook"):
                self._plain(404)
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._plain(400)
                return
            if length <= 0 or length > MAX_BODY:
                self._plain(413 if length > MAX_BODY else 400)
                return

            raw = self.rfile.read(length)

            signature = self.headers.get("tally-signature") or self.headers.get(
                "Tally-Signature"
            )
            if not verify_signature(raw, signature, secret):
                log.warning("rejected unsigned/invalid webhook from %s", self.address_string())
                self._plain(401)
                return

            try:
                payload = json.loads(raw.decode("utf-8"))
                sub = parse_webhook(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                log.warning("undecodable webhook body: %s", exc)
                self._plain(400)
                return
            except TallyPayloadError as exc:
                log.warning("unreadable webhook payload: %s", exc)
                self._plain(422)
                return

            disposition = service.accept(sub)
            self._plain(202, disposition + "\n")

    return Handler


def build_server(
    service: IntakeService,
    secret: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    pages: dict[str, tuple[str, bytes]] | None = None,
) -> ThreadingHTTPServer:
    if not secret:
        raise ValueError(
            "a webhook signing secret is required — an unauthenticated scoring "
            "endpoint is an open GitHub-API spend and lets anyone inject "
            "applicants into the ranking"
        )
    return ThreadingHTTPServer(
        (host, port), build_handler(service, secret, pages or {})
    )


def run_server(
    service: IntakeService,
    secret: str,
    host: str,
    port: int,
    pages: dict[str, tuple[str, bytes]] | None = None,
) -> None:
    httpd = build_server(service, secret, host, port, pages)
    service.start()
    requeued = service.requeue_pending()
    if requeued:
        log.info("requeued %d submission(s) left from a previous run", requeued)
    log.info("listening on http://%s:%d/webhook/tally", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down; draining %d queued", service.backlog())
    finally:
        httpd.shutdown()
        service.stop()
