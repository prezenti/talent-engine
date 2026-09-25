"""Tally intake: parsing, PII quarantine, signature auth, idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from talent_engine.config import load_program
from talent_engine.ingest.tally import (
    TallyPayloadError,
    parse_webhook,
    verify_signature,
)
from talent_engine.model import ProfileSnapshot, utc_now_iso
from talent_engine.server.webhook import IntakeService, build_server
from talent_engine.store.db import Store

SECRET = "tally-signing-secret"


def payload(fields, submission_id="sub_1"):
    return {
        "eventId": "evt_1",
        "eventType": "FORM_RESPONSE",
        "createdAt": "2026-08-12T10:00:00.000Z",
        "data": {
            "responseId": "resp_1",
            "submissionId": submission_id,
            "formId": "form_1",
            "formName": "Builder application",
            "createdAt": "2026-08-12T10:00:00.000Z",
            "fields": fields,
        },
    }


def f(label, value, type_="INPUT_TEXT", options=None):
    out = {"key": f"question_{label}", "label": label, "type": type_, "value": value}
    if options is not None:
        out["options"] = options
    return out


# ------------------------------------------------------------------ parsing


def test_parses_handle_from_a_question_not_a_column_name():
    sub = parse_webhook(payload([f("What is your GitHub?", "https://github.com/octocat/")]))
    assert sub.handle == "octocat"
    assert sub.ok


def test_unusable_handle_is_reported_not_guessed():
    sub = parse_webhook(payload([f("GitHub", "i don't have one")]))
    assert sub.handle == ""
    assert sub.raw_handle == "i don't have one"
    assert not sub.ok


def test_checkbox_option_ids_resolve_to_their_text():
    """Unresolved ids would score as unrecognised factors, silently."""
    sub = parse_webhook(
        payload(
            [
                f("GitHub", "octocat"),
                f(
                    "Which factors apply?",
                    ["opt_a", "opt_c"],
                    "CHECKBOXES",
                    options=[
                        {"id": "opt_a", "text": "self-taught"},
                        {"id": "opt_b", "text": "unrelated"},
                        {"id": "opt_c", "text": "no local tech industry"},
                    ],
                ),
            ]
        )
    )
    assert sub.application.context_factors == ["self-taught", "no local tech industry"]


def test_multiple_choice_single_id_resolves():
    sub = parse_webhook(
        payload(
            [
                f("GitHub", "octocat"),
                f(
                    "Referred by",
                    "opt_x",
                    "MULTIPLE_CHOICE",
                    options=[{"id": "opt_x", "text": "Prezenti"}],
                ),
            ]
        )
    )
    assert sub.application.referrer_name == "Prezenti"


def test_rejects_non_form_response_events():
    with pytest.raises(TallyPayloadError):
        parse_webhook({"eventType": "FORM_CREATED", "data": {"fields": []}})
    with pytest.raises(TallyPayloadError):
        parse_webhook({"eventType": "FORM_RESPONSE", "data": {}})


# ------------------------------------------------------- PII does not leak


def test_contact_details_are_separated_from_the_application():
    sub = parse_webhook(
        payload(
            [
                f("GitHub", "octocat"),
                f("Email", "someone@example.com", "INPUT_EMAIL"),
                f("Name", "A Person"),
                f("Telegram", "@someone"),
                f("What are you building?", "a thing"),
            ]
        )
    )
    assert sub.contact.email == "someone@example.com"
    assert sub.contact.name == "A Person"

    blob = json.dumps(sub.application.__dict__)
    assert "someone@example.com" not in blob
    assert "A Person" not in blob
    assert "@someone" not in blob


def test_email_in_an_oddly_labelled_field_still_does_not_reach_the_application():
    """The label filter alone is not enough; the value shape is checked too."""
    sub = parse_webhook(
        payload(
            [
                f("GitHub", "octocat"),
                f("How do we reach you?", "someone@example.com"),
            ]
        )
    )
    assert "someone@example.com" not in json.dumps(sub.application.extra)
    assert sub.contact.email == "someone@example.com"


def test_email_typed_field_is_caught_even_with_no_matching_label():
    sub = parse_webhook(
        payload([f("GitHub", "octocat"), f("Contact", "x@y.dev", "INPUT_EMAIL")])
    )
    assert sub.contact.email == "x@y.dev"
    assert "x@y.dev" not in json.dumps(sub.application.extra)


def test_an_email_field_is_never_mistaken_for_the_github_handle():
    """`normalize_handle` accepts bare words, so an email field must be skipped."""
    sub = parse_webhook(
        payload(
            [
                f("Your email", "octopus@example.com", "INPUT_EMAIL"),
                f("GitHub username", "octocat"),
            ]
        )
    )
    assert sub.handle == "octocat"


def test_contact_reaches_the_contacts_table_and_nothing_else(tmp_path):
    store = Store(tmp_path / "t.db")
    sub = parse_webhook(
        payload([f("GitHub", "octocat"), f("Email", "a@b.com", "INPUT_EMAIL")])
    )
    store.record_submission(
        sub.submission_id, "celo-trial", "tally", sub.handle, sub.raw_handle,
        sub.application, sub.form_id,
    )
    store.record_contact(sub.submission_id, sub.contact)

    row = store.get_submission(sub.submission_id)
    assert "a@b.com" not in json.dumps(row)
    assert store.contact_for(sub.submission_id)["email"] == "a@b.com"
    store.close()


# ------------------------------------------------------------- signatures


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_signature_roundtrip_and_rejections():
    body = b'{"a":1}'
    assert verify_signature(body, sign(body), SECRET)
    assert not verify_signature(body, sign(body, "wrong-secret"), SECRET)
    assert not verify_signature(body + b" ", sign(body), SECRET)
    assert not verify_signature(body, None, SECRET)
    assert not verify_signature(body, sign(body), "")  # no configured secret == no trust


def test_server_refuses_to_start_without_a_secret(tmp_path):
    cfg = load_program("celo-trial")
    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: None)
    with pytest.raises(ValueError, match="signing secret"):
        build_server(service, secret="", port=0)


# ------------------------------------------------------------- end to end


class FakeCollector:
    """Stands in for the GitHub-touching collector. Records what it was asked."""

    def __init__(self):
        self.collected = []

    def collect(self, handle, application):
        self.collected.append(handle)
        now = utc_now_iso()
        return ProfileSnapshot(
            handle=handle,
            account_created_at="2020-01-01T00:00:00Z",
            collected_at=now,
            window_start="2026-02-12T00:00:00Z",
            window_end=now,
            application=application,
        )


@pytest.fixture
def live(tmp_path):
    cfg = load_program("celo-trial")
    collector = FakeCollector()
    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: collector)
    httpd = build_server(service, SECRET, host="127.0.0.1", port=0)
    service.start()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield url, service, collector
    finally:
        httpd.shutdown()
        service.stop()
        thread.join(timeout=5)


def post(url, body: bytes, signature: str | None):
    req = urllib.request.Request(url + "/webhook/tally", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if signature is not None:
        req.add_header("tally-signature", signature)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_signed_submission_is_accepted_and_scored(live):
    url, service, collector = live
    body = json.dumps(
        payload([f("GitHub", "octocat"), f("Email", "a@b.com", "INPUT_EMAIL")])
    ).encode()

    status, text = post(url, body, sign(body))
    assert status == 202
    assert text.strip() == "queued"

    service.stop(drain=True)
    assert collector.collected == ["octocat"]

    store = Store(service.db_path)
    row = store.get_submission("sub_1")
    assert row["status"] == "scored"
    assert row["total"] is not None
    store.close()


def test_unsigned_submission_is_rejected_and_never_reaches_the_store(live):
    url, service, collector = live
    body = json.dumps(payload([f("GitHub", "octocat")])).encode()

    assert post(url, body, None)[0] == 401
    assert post(url, body, "not-a-signature")[0] == 401
    assert post(url, body, sign(body, "attacker-secret"))[0] == 401

    assert collector.collected == []
    store = Store(service.db_path)
    assert store.get_submission("sub_1") is None
    store.close()


def test_redelivery_does_not_score_twice(live):
    """Form platforms retry. A retry must not double-spend the API budget."""
    url, service, collector = live
    body = json.dumps(payload([f("GitHub", "octocat")])).encode()
    sig = sign(body)

    assert post(url, body, sig)[1].strip() == "queued"
    assert post(url, body, sig)[1].strip() == "duplicate"
    assert post(url, body, sig)[1].strip() == "duplicate"

    service.stop(drain=True)
    assert collector.collected == ["octocat"]


def test_unparsable_handle_is_stored_for_review_not_dropped(live):
    url, service, collector = live
    body = json.dumps(payload([f("GitHub", "no idea")])).encode()

    status, text = post(url, body, sign(body))
    assert status == 202  # not an error: retrying will never help
    assert text.strip() == "unparsable"

    service.stop(drain=True)
    assert collector.collected == []
    store = Store(service.db_path)
    row = store.get_submission("sub_1")
    assert row["status"] == "unparsable"
    assert "no idea" in row["error"]
    store.close()


def test_garbage_bodies_are_rejected_by_shape(live):
    url, _service, _collector = live
    bad = b"not json at all"
    assert post(url, bad, sign(bad))[0] == 400

    wrong = json.dumps({"eventType": "FORM_CREATED", "data": {}}).encode()
    assert post(url, wrong, sign(wrong))[0] == 422


def test_oversized_body_is_refused(live):
    url, _service, _collector = live
    # Send only the headers. The server rejects from Content-Length before it
    # reads the body; asking urllib to upload 2 MiB lets a fast rejection close
    # the socket mid-send and Python 3.12 reports BrokenPipe instead of exposing
    # the 413 response.
    host = url.removeprefix("http://")
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.putrequest("POST", "/webhook/tally")
    conn.putheader("Content-Length", str(1 << 21))
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 413
    response.read()
    conn.close()


def test_health_endpoint(live):
    url, _service, _collector = live
    with urllib.request.urlopen(url + "/healthz", timeout=5) as resp:
        assert resp.status == 200


def test_a_crash_mid_scoring_leaves_the_submission_recoverable(tmp_path):
    """Durability is in SQLite, not the queue: `serve` requeues on restart."""
    cfg = load_program("celo-trial")

    class Exploding:
        def collect(self, handle, application):
            raise RuntimeError("github fell over")

    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: Exploding())
    service.start()
    sub = parse_webhook(payload([f("GitHub", "octocat")]))
    service.accept(sub)
    service.stop(drain=True)

    store = Store(service.db_path)
    row = store.get_submission("sub_1")
    # Recoverable means retryable. This previously asserted `error`, which was
    # a dead end: nothing outside retries once Tally has its 202, so the row
    # was stored, unscored and invisible to the requeue path forever.
    assert row["status"] == "queued"
    assert "github fell over" in row["error"]
    assert store.pending_submissions(cfg.key)
    store.close()


# ------------------------------------------------------------- public page


@pytest.fixture
def live_page(tmp_path):
    from talent_engine.server import routes

    cfg = load_program("celo-trial")
    collector = FakeCollector()
    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: collector)
    httpd = build_server(
        service, SECRET, host="127.0.0.1", port=0, pages=routes(cfg.name, "wAbCdE")
    )
    service.start()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield url, service
    finally:
        httpd.shutdown()
        service.stop()
        thread.join(timeout=5)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def test_landing_page_renders_with_the_form_embedded(live_page):
    url, _ = live_page
    status, body, headers = get(url + "/")
    assert status == 200
    assert "https://tally.so/embed/wAbCdE" in body
    assert "reproduce your own score" in body
    assert headers["Content-Type"].startswith("text/html")


def test_page_carries_a_restrictive_csp(live_page):
    """The page loads no scripts of its own and one foreign origin."""
    url, _ = live_page
    _status, _body, headers = get(url + "/")
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-src https://tally.so" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_no_path_traversal_is_reachable(live_page):
    """Routing is an exact-match dict lookup, so these are all plain misses."""
    url, _ = live_page
    for probe in (
        "/../../etc/passwd",
        "/..%2f..%2fetc%2fpasswd",
        "/static/../../../etc/passwd",
        "/talent_engine.db",
        "/.env",
        "/intake.env",
    ):
        status, _body, _headers = get(url + probe)
        assert status == 404, f"{probe} returned {status}"


def test_page_is_absent_when_not_configured(live):
    """`serve --no-page` leaves only the webhook and health endpoints."""
    url, _service, _collector = live
    assert get(url + "/")[0] == 404
    assert get(url + "/healthz")[0] == 200


def test_invite_route_bypasses_the_deployment_close_flag(monkeypatch):
    from talent_engine.server import routes

    token = "inviteToken000000000000"
    monkeypatch.setenv("TE_APPLICATIONS_CLOSED", "1")
    monkeypatch.setenv("TE_APPLICATIONS_CLOSED_AT", "2026-09-24 00:00 America/Los_Angeles")
    monkeypatch.setenv("TE_INVITE_APPLY_TOKEN", token)

    pages = routes("P", "publicForm")

    public = pages["/"][1].decode()
    invite = pages[f"/invite/{token}.html"][1].decode()
    assert "Applications are closed" in public
    assert "tally.so/embed" not in public
    assert "Private invitation" in invite
    assert "https://tally.so/embed/publicForm" in invite
    assert "Applications are closed" not in invite


def test_invite_route_requires_a_long_urlsafe_token(monkeypatch):
    from talent_engine.server import routes

    monkeypatch.setenv("TE_INVITE_APPLY_TOKEN", "short")
    assert all(not path.startswith("/invite/") for path in routes("P", "formid"))

    monkeypatch.setenv("TE_INVITE_APPLY_TOKEN", "bad/token/bad/token/bad")
    assert all(not path.startswith("/invite/") for path in routes("P", "formid"))


def test_invite_route_can_use_a_separate_form(monkeypatch):
    from talent_engine.server import routes

    token = "aaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setenv("TE_INVITE_APPLY_TOKEN", token)
    monkeypatch.setenv("TE_INVITE_TALLY_FORM_ID", "privateForm")

    pages = routes("P", "publicForm")

    assert "https://tally.so/embed/publicForm" in pages["/"][1].decode()
    assert "https://tally.so/embed/privateForm" in pages[f"/invite/{token}.html"][1].decode()


def test_the_form_embed_degrades_honestly_without_a_form_id():
    from talent_engine.server import landing_page

    body = landing_page("Some Program", "").decode()
    assert "tally.so/embed" not in body
    assert "not connected yet" in body


def test_form_id_is_escaped_into_the_iframe_src():
    from talent_engine.server import landing_page

    body = landing_page("P", '"><script>alert(1)</script>').decode()
    assert "<script>alert(1)</script>" not in body


def test_page_copy_comes_from_the_program_config():
    """The words published under someone's own domain are theirs to write."""
    from talent_engine.server import landing_page

    body = landing_page(
        "P", "", {"headline": "Prezenti Builder Sponsorship", "footer": "Run by Prezenti."}
    ).decode()
    assert "Prezenti Builder Sponsorship" in body
    assert "Run by Prezenti." in body
    assert "Sponsorship for people who ship" not in body  # default replaced


def test_page_copy_is_escaped():
    from talent_engine.server import landing_page

    body = landing_page("P", "", {"headline": "<script>alert(1)</script>"}).decode()
    assert "<script>alert(1)</script>" not in body


def test_program_config_carries_page_copy(tmp_path):
    """A config with a page block round-trips and reaches the renderer."""
    import json as _json

    from talent_engine.config import ProgramConfig

    path = tmp_path / "p.json"
    path.write_text(
        _json.dumps(
            {
                "key": "p",
                "name": "P",
                "page": {"headline": "Hello"},
                # Default weights score trusted_referral, which now requires
                # a registry to check against (invariant 4).
                "referrers": ["Example Referrer"],
            }
        )
    )
    cfg = ProgramConfig.load(path)
    assert cfg.page["headline"] == "Hello"
    assert ProgramConfig.from_dict(cfg.to_dict()).page["headline"] == "Hello"


def test_the_page_makes_no_external_request_except_the_form(live_page):
    """CSP allows one foreign origin; brand assets must be embedded, not fetched."""
    url, _ = live_page
    _status, body, headers = get(url + "/")

    # Fonts and logo are inline, so no CDN host appears anywhere.
    assert "fonts.googleapis.com" not in body
    assert "fonts.gstatic.com" not in body
    assert "website-files.com" not in body
    assert "data:font/woff2" in body
    assert '<svg class="logo"' in body

    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "font-src data:" in csp
    # tally.so remains the only permitted external origin.
    assert csp.count("https://") == 1


def test_the_page_carries_prezenti_brand_tokens(live_page):
    url, _ = live_page
    _status, body, _headers = get(url + "/")
    for colour in ("#112122", "#fef4ee", "#eb4b24", "#68a9a3"):
        assert colour in body, f"brand colour {colour} missing"
    assert "Outfit" in body and "DM Sans" in body


def test_the_page_states_that_a_score_is_not_a_decision(live_page):
    """The red-team result makes this claim load-bearing, not decoration."""
    url, _ = live_page
    _status, body, _headers = get(url + "/")
    assert "A score is not a decision" in body
    assert "shortlist" in body


def test_the_test_suite_cannot_send_a_real_notification():
    """A regression guard for a bug that reached a real person's phone.

    notify.py used to fall back to a bot token on disk when the environment
    had none, so merely running pytest delivered live messages. Credentials
    now come from the environment only, and conftest sets the kill switch.
    """
    from talent_engine import notify

    assert notify.disabled(), "conftest should have disabled notifications"
    assert not hasattr(notify, "FALLBACK_ENV"), "no credential fallback may exist"

    # Even fully configured, a disabled notifier sends nothing.
    import os

    for key, value in {
        "TELEGRAM_BOT_TOKEN": "x",
        "TELEGRAM_CHAT_ID": "y",
        "SMTP_HOST": "smtp.invalid",
        "NOTIFY_EMAIL_TO": "nobody@invalid",
    }.items():
        os.environ[key] = value
    try:
        assert notify.send("this must not go anywhere") is False
        assert notify.send_email("subject", "body") is False
    finally:
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "NOTIFY_EMAIL_TO"):
            os.environ.pop(key, None)


def test_a_transient_scoring_failure_leaves_the_submission_retryable(tmp_path):
    """Tally already had its 202, so nothing external will ever retry.

    A GitHub outage or an expired token used to park a real applicant as
    `error` permanently — stored, unscored, unnotified, and invisible to the
    requeue-on-restart path, which only picks up `queued`.
    """
    cfg = load_program("celo-trial")

    class Flaky:
        def __init__(self):
            self.calls = 0

        def collect(self, handle, application):
            self.calls += 1
            raise RuntimeError("github 503")

    flaky = Flaky()
    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: flaky)
    service.start()
    service.accept(parse_webhook(payload([f("GitHub", "octocat")])))
    service.stop(drain=True)

    store = Store(service.db_path)
    row = store.get_submission("sub_1")
    assert row["status"] == "queued", "a transient failure must stay retryable"
    assert "attempt 1" in row["error"]
    # And a restart would pick it up again.
    assert store.pending_submissions(cfg.key)
    store.close()


def test_a_repeatedly_failing_submission_is_eventually_parked(tmp_path):
    """Retrying forever would spin the worker on a permanently broken row."""
    cfg = load_program("celo-trial")

    class AlwaysBroken:
        def collect(self, handle, application):
            raise RuntimeError("nope")

    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: AlwaysBroken())
    service.start()
    service.accept(parse_webhook(payload([f("GitHub", "octocat")])))
    for _ in range(service.max_attempts):
        service.requeue_pending()
        service._q.join()
    service.stop(drain=True)

    store = Store(service.db_path)
    assert store.get_submission("sub_1")["status"] == "error"
    store.close()


def test_a_budget_exhausted_snapshot_is_not_scored(tmp_path):
    """An incomplete collection is a floor, not a measurement.

    Scoring it would put a misleadingly low number in front of a human with
    only a soft flag to contradict it. It stays retryable instead.
    """
    cfg = load_program("celo-trial")

    class Starved:
        def collect(self, handle, application):
            now = utc_now_iso()
            snap = ProfileSnapshot(
                handle=handle,
                account_created_at="2020-01-01T00:00:00Z",
                collected_at=now,
                window_start="2026-02-12T00:00:00Z",
                window_end=now,
                application=application,
            )
            snap.partial = True
            snap.collection_notes.append("request budget exhausted: ran out")
            return snap

    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=lambda: Starved())
    service.start()
    service.accept(parse_webhook(payload([f("GitHub", "octocat")])))
    service.stop(drain=True)

    store = Store(service.db_path)
    row = store.get_submission("sub_1")
    assert row["status"] == "queued"          # retryable, not a bad score
    assert row["total"] is None
    assert "budget" in row["error"]
    store.close()


def test_each_submission_gets_a_fresh_collector(tmp_path):
    """The client's request budget is finite; reusing one exhausts it."""
    cfg = load_program("celo-trial")
    built = []

    def factory():
        collector = FakeCollector()
        built.append(collector)
        return collector

    service = IntakeService(cfg, str(tmp_path / "t.db"), collector_factory=factory)
    service.start()
    for i in range(3):
        service.accept(parse_webhook(payload([f("GitHub", "octocat")], submission_id=f"s{i}")))
    service.stop(drain=True)

    assert len(built) == 3, "a collector, and therefore a budget, per submission"


def test_handles_are_case_normalised():
    """GitHub logins are case-insensitive; two rows for one person reach the
    cohort table, which allocates funded seats."""
    from talent_engine.ingest.normalize import normalize_handle

    assert normalize_handle("OctoCat") == "octocat"
    assert normalize_handle("https://github.com/OctoCat/") == "octocat"
    assert normalize_handle("@OCTOCAT") == "octocat"


def test_the_same_person_in_different_case_is_one_submission(live):
    """Two spellings, one identity, one score — not two shots at a seat."""
    url, service, collector = live
    for i, spelling in enumerate(["OctoCat", "octocat", "OCTOCAT"]):
        body = json.dumps(
            payload([f("GitHub", spelling)], submission_id=f"case_{i}")
        ).encode()
        assert post(url, body, sign(body))[0] == 202

    service.stop(drain=True)
    assert collector.collected == ["octocat", "octocat", "octocat"]
    store = Store(service.db_path)
    handles = {r["handle"] for r in store.submissions("celo-trial", limit=10)}
    assert handles == {"octocat"}
    store.close()


def test_a_totally_failed_notification_is_loud(caplog, monkeypatch):
    """Both channels down must not be swallowed.

    A notification going nowhere is the failure this layer exists to prevent,
    and a False return nobody checks is indistinguishable from silence.
    """
    import logging

    from talent_engine import notify

    monkeypatch.setenv(notify.DISABLE_ENV, "0")
    monkeypatch.setattr(notify, "mail_config", lambda: {})
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)

    with caplog.at_level(logging.ERROR, logger="talent_engine.notify"):
        assert notify.notify("subject", "body") is False
    assert any("NOTIFICATION LOST" in r.message for r in caplog.records)
