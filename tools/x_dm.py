#!/usr/bin/env python3
"""Send the outreach messages through X, slowly, and record what happened.

This is the only part of the system that speaks to a stranger without a person
pressing the button each time, so it is built to be boring: a small batch, a
human-sized gap between sends, a dry run that is the default, and a written
record of every outcome including the refusals.

The refusals are the point. X will not tell you in advance whether somebody
accepts messages from people they do not follow -- you find out by trying. Sent
by hand that is a shrug; sent here it is a row in `x_delivery` saying that this
person cannot be reached this way, so nobody wastes a second attempt on them.

Usage:
  x_dm.py --program prezenti-sponsorship-trial              # dry run, prints what it would do
  x_dm.py --program … --send --limit 25                     # actually send
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talent_engine.modes import outreach, x_outreach  # noqa: E402
from talent_engine.store.db import Store  # noqa: E402
from talent_engine.x import auth  # noqa: E402
from talent_engine.x.client import XClient  # noqa: E402

RUNTIME = Path.home() / "talent-engine-runtime"

# Never written to, not an applicant, has something true to say, and has not
# already been found unreachable. `dm_status` of 'error' is allowed back in --
# that one might work tomorrow; 'refused' is a settled fact and is not.
QUEUE = """
SELECT sc.handle, r.x_handle, r.name,
       h.hook, d.user_id, d.dm_status,
       (SELECT MAX(total) FROM scores s WHERE s.handle = sc.handle) AS total
  FROM scouted sc
  JOIN profile_recon r ON r.handle = sc.handle
  JOIN outreach_hooks h ON h.handle = sc.handle
  LEFT JOIN x_delivery d ON d.handle = sc.handle
 WHERE sc.program = ?
   AND r.x_handle != ''
   AND h.hook != ''
   AND COALESCE(h.sent_at, '') = ''
   AND COALESCE(d.dm_status, '') != 'refused'
   AND COALESCE(d.dm_status, '') != 'sent'
   AND NOT EXISTS (SELECT 1 FROM submissions su
                    WHERE LOWER(su.handle) = LOWER(sc.handle))
 ORDER BY COALESCE(total, -1) DESC, sc.first_seen DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--program", required=True)
    ap.add_argument("--db", default=str(RUNTIME / "talent_engine.db"))
    ap.add_argument("--tokens", default=str(RUNTIME / "x-tokens.json"))
    ap.add_argument("--limit", type=int, default=x_outreach.DM_PER_RUN)
    ap.add_argument("--full", action="store_true",
                    help="send the long version instead of the short one")
    ap.add_argument("--send", action="store_true",
                    help="actually send; without it this only shows the queue")
    args = ap.parse_args()

    store = Store(args.db)
    rows = [dict(r) for r in store.conn.execute(QUEUE, (args.program,)).fetchall()]
    template = outreach.FULL_MESSAGE if args.full else outreach.SHORT_MESSAGE

    targets = []
    for r in rows:
        text = outreach.render(
            template, handle=r["handle"], name=r["name"] or "", hook=r["hook"] or ""
        )
        if text:
            targets.append({**r, "text": text})

    if not args.send:
        print(f"{len(targets)} in the queue; would send {min(len(targets), args.limit)}.\n")
        for t in targets[: args.limit]:
            score = f'{t["total"]:.1f}' if t["total"] is not None else "unscored"
            print(f'  @{t["x_handle"]:<20} {t["handle"]:<20} {score:>8}'
                  f'{"" if t["user_id"] else "   (id not resolved yet)"}')
        print("\nnothing sent — pass --send")
        store.close()
        return 0

    client_id = os.environ.get("X_CLIENT_ID", "").strip()
    secret = os.environ.get("X_CLIENT_SECRET", "").strip()
    if not client_id:
        raise SystemExit("X_CLIENT_ID is not set — see tools/x_auth.py")
    tokens = auth.TokenStore(args.tokens)
    client = XClient(lambda: tokens.access_token(client_id, secret))

    # Resolve ids for whoever is actually about to be written to, and no one
    # else: a user read is billed, and resolving four hundred people to send
    # twenty-five messages is paying for the other three hundred and seventy-five.
    batch = targets[: args.limit]
    unknown = [t["x_handle"] for t in batch if not t["user_id"]]
    if unknown:
        found = x_outreach.resolve_ids(client, unknown)
        for t in batch:
            if not t["user_id"]:
                uid = found.get(t["x_handle"].lower())
                if uid:
                    store.save_x_id(t["handle"], t["x_handle"], uid)
                    t["user_id"] = uid
    sendable = [t for t in batch if t["user_id"]]
    skipped = len(batch) - len(sendable)

    def remember(target: dict, status: str, detail: str) -> None:
        store.record_dm(target["handle"], status, detail)
        if status == "sent":
            # The same column the console writes, so one queue governs both
            # channels and nobody is written to twice by two different routes.
            store.mark_contacted(target["handle"])
        marker = {"sent": "→", "refused": "×", "error": "!"}.get(status, "?")
        print(f'  {marker} @{target["x_handle"]:<20} {status}'
              + (f"  {detail}" if detail else ""))

    counts = x_outreach.send_batch(client, sendable, limit=args.limit, on_result=remember)
    print(f"\nsent {counts['sent']}, refused {counts['refused']}, "
          f"errors {counts['error']}"
          + (f", {skipped} had no resolvable account" if skipped else "")
          + f" ({client.calls} API calls)")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
