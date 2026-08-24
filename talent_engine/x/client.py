"""A thin client over the X API. It makes requests; it decides nothing.

Deliberately small. The only judgements it makes are the ones that have to
happen at the transport layer: refresh a token before it expires, wait when
told to wait, and turn an HTTP failure into something a caller can record
rather than an exception that loses the reason.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.x.com"


class XResponse:
    """What happened, including when what happened was a refusal.

    A refused DM is not an exception here. X answers "this person does not
    accept messages from strangers" with a status code, and that is a fact
    about the recipient worth recording -- not an error worth stopping a batch
    over.
    """

    def __init__(self, status: int, body: Any, detail: str = ""):
        self.status = status
        self.body = body
        self.detail = detail

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __repr__(self) -> str:
        return f"XResponse({self.status}, {self.detail[:80]!r})"


class XClient:
    def __init__(self, token_provider, *, transport=None, sleep=time.sleep):
        """`token_provider()` returns a live bearer token each call.

        A callable rather than a string so that a long batch refreshes mid-run
        without the caller thinking about it -- an hour of paced sending
        outlives an access token.
        """
        self._token = token_provider
        self._transport = transport or self._urllib
        self._sleep = sleep
        self.calls = 0

    def _urllib(self, method: str, url: str, headers: dict, body: bytes | None):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace"), dict(exc.headers or {})
        except urllib.error.URLError as exc:
            return 0, json.dumps({"detail": str(exc.reason)}), {}

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, retries: int = 2) -> XResponse:
        url = f"{API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        payload = json.dumps(body).encode() if body is not None else None

        for attempt in range(retries + 1):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "User-Agent": "talent-engine",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            status, text, resp_headers = self._transport(method, url, headers, payload)
            self.calls += 1

            if status == 429 and attempt < retries:
                # X sends the reset as an epoch second. Honouring it is cheaper
                # than a fixed backoff and much cheaper than being throttled
                # harder for guessing.
                reset = resp_headers.get("x-rate-limit-reset")
                wait = 60.0
                try:
                    if reset:
                        wait = max(1.0, float(reset) - time.time())
                except ValueError:
                    pass
                self._sleep(min(wait, 900))
                continue

            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {}
            detail = ""
            if isinstance(parsed, dict):
                detail = str(
                    parsed.get("detail")
                    or parsed.get("title")
                    or (parsed.get("errors") or [{}])[0].get("message", "")
                    or ""
                )
            return XResponse(status, parsed, detail or text[:200])

        return XResponse(429, {}, "rate limited after retries")

    def get(self, path: str, params: dict | None = None) -> XResponse:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> XResponse:
        return self.request("POST", path, body=body)
