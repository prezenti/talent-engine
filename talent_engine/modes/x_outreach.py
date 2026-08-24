"""Two things X can do for this programme: hold a list, and carry a message.

The list is the uncontroversial half. A private list of everyone the scout
surfaced is a feed of what these people are actually shipping, which is worth
more than the DM: replying to somebody's work before writing to them changes
the reply rate more than any wording of the message will. Private matters --
a public list notifies the people added to it, and several hundred strangers
discovering they are on a list called "candidates" is a worse first contact
than the message itself.

The messages are the half that needs care, and the care is pacing rather than
content. Several hundred sends in an afternoon is a bulk send to X's spam
heuristics whatever each one says, and the account that gets limited is the
one the programme speaks from.

Delivery is not guaranteed and X will not say in advance: the recipient may
not accept messages from people they do not follow. That is recorded when it
happens, so it is discovered once per person rather than every time.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable

# X takes up to 100 usernames per lookup. Fewer requests, same billed
# resources -- and a hundred names is one failure to retry rather than a
# hundred.
LOOKUP_BATCH = 100

# POST /2/lists/:id/members is 300 per user per 15 minutes, so a few hundred
# people is two windows and needs no pacing of its own beyond the cap.
LIST_ADD_WINDOW = 300

# Not from the documentation -- there is no published per-day DM cap for a
# normal account, and the limit that matters is a spam heuristic nobody
# publishes. So this is a judgement: a rate a person could plausibly type.
DM_PER_RUN = 25
DM_GAP_SECONDS = (45, 150)


def resolve_ids(client, handles: Iterable[str]) -> dict[str, str]:
    """Usernames to the numeric ids X actually addresses.

    Returns only what was found. A handle that has been changed, deleted or
    suspended since recon simply will not come back, and that absence is the
    answer rather than an error.
    """
    wanted = [h.lstrip("@") for h in handles if h]
    found: dict[str, str] = {}
    for i in range(0, len(wanted), LOOKUP_BATCH):
        chunk = wanted[i:i + LOOKUP_BATCH]
        resp = client.get("/2/users/by", {"usernames": ",".join(chunk)})
        if not resp.ok:
            continue
        for user in (resp.body or {}).get("data") or []:
            username = (user.get("username") or "").lower()
            if username and user.get("id"):
                found[username] = str(user["id"])
    return found


def ensure_list(client, name: str, description: str, private: bool = True) -> dict:
    """Create the list. Returns {"id": ...} or an explanation of why not."""
    resp = client.post("/2/lists", {
        "name": name[:25],          # X truncates at 25 characters; do it knowingly
        "description": description[:100],
        "private": private,
    })
    if resp.ok:
        return {"id": str(((resp.body or {}).get("data") or {}).get("id", ""))}
    return {"error": resp.detail or f"HTTP {resp.status}"}


def add_members(client, list_id: str, user_ids: Iterable[str],
                on_result: Callable[[str, bool, str], None] | None = None) -> dict:
    """Add people to the list, reporting each one rather than the batch.

    One membership failing -- a protected account, a suspension between the
    lookup and now -- must not cost the other two hundred.
    """
    added = failed = 0
    for user_id in user_ids:
        resp = client.post(f"/2/lists/{list_id}/members", {"user_id": str(user_id)})
        ok = resp.ok and bool(((resp.body or {}).get("data") or {}).get("is_member", True))
        if ok:
            added += 1
        else:
            failed += 1
        if on_result:
            on_result(str(user_id), ok, resp.detail)
    return {"added": added, "failed": failed}


def classify(resp) -> tuple[str, str]:
    """What a DM attempt actually means, in three words the database can hold.

    `refused` is the important one: the recipient does not accept messages from
    strangers, or has blocked us. It is a settled fact about that person, so it
    is recorded and they are not queued again. `error` is anything that might
    succeed on a different day and should be retried.
    """
    if resp.ok:
        return "sent", ""
    if resp.status in (403, 400):
        return "refused", resp.detail or f"HTTP {resp.status}"
    return "error", resp.detail or f"HTTP {resp.status}"


def send_dm(client, user_id: str, text: str) -> tuple[str, str]:
    resp = client.post(f"/2/dm_conversations/with/{user_id}/messages", {"text": text})
    return classify(resp)


def send_batch(client, targets: list[dict[str, Any]], *,
               limit: int = DM_PER_RUN,
               on_result: Callable[[dict, str, str], None] | None = None,
               sleep: Callable[[float], None] = time.sleep,
               jitter: Callable[[float, float], float] = random.uniform) -> dict:
    """Send to at most `limit` people, with a human-sized gap between each.

    The gap is the whole safety mechanism and it is not configurable to zero on
    purpose. Anything faster is a bulk send, and the account it costs is the one
    the programme speaks from.
    """
    counts = {"sent": 0, "refused": 0, "error": 0}
    for n, target in enumerate(targets[:limit]):
        if n:
            sleep(jitter(*DM_GAP_SECONDS))
        status, detail = send_dm(client, target["user_id"], target["text"])
        counts[status] = counts.get(status, 0) + 1
        if on_result:
            on_result(target, status, detail)
        if status == "error" and counts["error"] >= 3:
            # Three transport failures in a row is a condition, not bad luck.
            # Stopping leaves the rest of the queue untouched for a later run.
            break
    return counts
