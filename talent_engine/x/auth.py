"""OAuth 2.0 PKCE against X, because DMs cannot be sent app-only.

X is explicit that "App-Only authentication is not supported" for Direct
Messages: every send is attributed to a person, which is the correct shape for
what this does. So there is a one-time browser step where the account owner
approves the app, and after that a refresh token this machine can use.

The tokens are the account. They are written 0600 beside the other runtime
credentials and never travel anywhere else -- not into the repository, not into
a log line, not into a chat message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

AUTHORIZE = "https://x.com/i/oauth2/authorize"
TOKEN = "https://api.x.com/2/oauth2/token"

# Everything this system does and nothing more. `offline.access` is what makes
# the refresh token exist; without it the grant dies in a couple of hours and
# the browser step becomes a daily chore.
SCOPES = [
    "tweet.read",
    "users.read",
    "dm.read",
    "dm.write",
    "list.read",
    "list.write",
    "offline.access",
]

# Loopback, because the alternative is exposing a callback on the public tunnel
# for the sake of one redirect that happens once.
REDIRECT = "http://127.0.0.1:8788/callback"


class XAuthError(RuntimeError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def challenge() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = _b64(secrets.token_bytes(64))
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, _b64(digest)


def authorize_url(client_id: str, code_challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE}?{query}"


def _post(fields: dict[str, str], client_id: str, client_secret: str = "") -> dict:
    body = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        # Confidential clients authenticate the token call itself; public ones
        # send client_id in the body and nothing else.
        pair = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {pair}"
    req = urllib.request.Request(TOKEN, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # the body says why; the status does not
        detail = exc.read().decode(errors="replace")[:400]
        raise XAuthError(f"token endpoint returned {exc.code}: {detail}") from exc


def exchange(code: str, verifier: str, client_id: str, client_secret: str = "") -> dict:
    return _post({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
        "client_id": client_id,
    }, client_id, client_secret)


def refresh(refresh_token: str, client_id: str, client_secret: str = "") -> dict:
    return _post({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }, client_id, client_secret)


class TokenStore:
    """The grant on disk, 0600, refreshed in place.

    X rotates the refresh token on every use, so the write has to happen before
    the next call goes out: a refresh whose result was not saved leaves the
    stored token already spent, and the grant is then dead with no way back
    except the browser step again.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            raise XAuthError(
                f"no X grant at {self.path} — run tools/x_auth.py once to create it"
            )
        return json.loads(self.path.read_text())

    def save(self, grant: dict) -> None:
        grant = dict(grant)
        grant.setdefault("obtained_at", int(time.time()))
        tmp = self.path.with_suffix(".tmp")
        # Written restricted from the first byte rather than chmod'ed after:
        # between creation and chmod is a window, and this is the account.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(grant, fh, indent=2)
        os.replace(tmp, self.path)

    def access_token(self, client_id: str, client_secret: str = "",
                     margin: int = 120) -> str:
        """A live access token, refreshing if this one is close to expiry."""
        grant = self.load()
        age = int(time.time()) - int(grant.get("obtained_at", 0))
        if age < int(grant.get("expires_in", 0)) - margin:
            return grant["access_token"]
        if not grant.get("refresh_token"):
            raise XAuthError(
                "the stored grant has no refresh token — it was created without "
                "offline.access; re-run tools/x_auth.py"
            )
        fresh = refresh(grant["refresh_token"], client_id, client_secret)
        self.save(fresh)
        return fresh["access_token"]
