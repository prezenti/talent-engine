#!/usr/bin/env python3
"""Authorise this machine to act as the programme's X account. Once.

Two steps, because the browser doing the approving is not on this machine.

  x_auth.py --start
      prints a URL. Open it, approve. X then redirects to 127.0.0.1:8788,
      which is nothing on your laptop -- the browser will say it cannot
      connect. That is expected. The address bar now holds the grant.

  x_auth.py --finish '<paste the whole address bar>'
      exchanges it and writes the tokens 0600.

The redirect deliberately goes nowhere. A callback served on the public tunnel
would mean a public endpoint that accepts authorisation codes, permanently, for
the sake of one redirect that happens once. A dead loopback address leaks
nothing and costs one paste.

Run the second step yourself so the code never travels through a chat window.
It is single-use and short-lived, but "short-lived" is not "harmless".
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talent_engine.x import auth  # noqa: E402

RUNTIME = Path.home() / "talent-engine-runtime"
PENDING = RUNTIME / ".x-auth-pending.json"
TOKENS = RUNTIME / "x-tokens.json"


def _client() -> tuple[str, str]:
    cid = os.environ.get("X_CLIENT_ID", "").strip()
    if not cid:
        raise SystemExit(
            "X_CLIENT_ID is not set. Add it (and X_CLIENT_SECRET if your app is\n"
            "confidential) to ~/talent-engine-runtime/intake.env, then re-run with\n"
            "  set -a; . ~/talent-engine-runtime/intake.env; set +a"
        )
    return cid, os.environ.get("X_CLIENT_SECRET", "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true")
    g.add_argument("--finish", metavar="REDIRECT_URL")
    ap.add_argument("--tokens", default=str(TOKENS))
    args = ap.parse_args()

    client_id, client_secret = _client()

    if args.start:
        verifier, code_challenge = auth.challenge()
        state = secrets.token_urlsafe(24)
        fd = os.open(PENDING, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"verifier": verifier, "state": state}, fh)
        print("Open this, approve, then copy the address bar you land on:\n")
        print(auth.authorize_url(client_id, code_challenge, state))
        print(f"\nScopes requested: {' '.join(auth.SCOPES)}")
        return 0

    if not PENDING.exists():
        raise SystemExit("no pending authorisation — run --start first")
    pending = json.loads(PENDING.read_text())

    parsed = urllib.parse.urlparse(args.finish.strip())
    query = urllib.parse.parse_qs(parsed.query)
    if "error" in query:
        raise SystemExit(f"X refused: {query['error'][0]} {query.get('error_description', [''])[0]}")
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code:
        raise SystemExit("no ?code= in that URL — paste the whole address bar")
    # The state check is the only thing standing between this and a code
    # somebody else obtained; without it, "paste the URL you were given" is a
    # workable attack rather than a workflow.
    if not secrets.compare_digest(state, pending.get("state", "")):
        raise SystemExit("state mismatch — that URL is not from the --start we issued")

    grant = auth.exchange(code, pending["verifier"], client_id, client_secret)
    store = auth.TokenStore(args.tokens)
    store.save(grant)
    PENDING.unlink(missing_ok=True)

    have_refresh = "yes" if grant.get("refresh_token") else "NO — offline.access was not granted"
    print(f"saved {args.tokens} (0600)")
    print(f"scopes: {grant.get('scope', '?')}")
    print(f"refresh token: {have_refresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
