#!/usr/bin/env python3
"""Put everyone the scout found onto a private X list.

Worth doing before any message goes out. A list of the people building the
things you fund is a feed of what they are actually shipping, and replying to
somebody's work before writing to them does more for a reply rate than any
wording of the message.

Private by default and the flag to change that is deliberately awkward: adding
someone to a public list notifies them, and several hundred strangers finding
themselves in a list called "candidates" is a worse first contact than the
message would have been.

Usage:
  x_list.py --program prezenti-sponsorship-trial --name "Builders" --create
  x_list.py --program … --list-id 1234567890            # add to an existing one
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talent_engine.modes import x_outreach  # noqa: E402
from talent_engine.store.db import Store  # noqa: E402
from talent_engine.x import auth  # noqa: E402
from talent_engine.x.client import XClient  # noqa: E402

RUNTIME = Path.home() / "talent-engine-runtime"

# Everyone reachable on X, whether or not they have been written to: the list
# is for watching, and somebody already messaged is exactly who you want to
# keep an eye on.
TARGETS = """
SELECT sc.handle, r.x_handle, d.user_id, d.listed_at
  FROM scouted sc
  JOIN profile_recon r ON r.handle = sc.handle
  LEFT JOIN x_delivery d ON d.handle = sc.handle
 WHERE sc.program = ? AND r.x_handle != ''
 ORDER BY sc.first_seen ASC
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--program", required=True)
    ap.add_argument("--db", default=str(RUNTIME / "talent_engine.db"))
    ap.add_argument("--tokens", default=str(RUNTIME / "x-tokens.json"))
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--list-id", default="")
    ap.add_argument("--name", default="Builders we found")
    ap.add_argument("--description",
                    default="Scouted on public shipping evidence. Not an endorsement.")
    ap.add_argument("--public", action="store_true",
                    help="notifies everyone added; you almost certainly do not want this")
    ap.add_argument("--limit", type=int, default=x_outreach.LIST_ADD_WINDOW)
    args = ap.parse_args()

    client_id = os.environ.get("X_CLIENT_ID", "").strip()
    secret = os.environ.get("X_CLIENT_SECRET", "").strip()
    if not client_id:
        raise SystemExit("X_CLIENT_ID is not set — see tools/x_auth.py")
    store_tokens = auth.TokenStore(args.tokens)
    client = XClient(lambda: store_tokens.access_token(client_id, secret))

    store = Store(args.db)
    rows = [dict(r) for r in store.conn.execute(TARGETS, (args.program,)).fetchall()]

    # Resolve anybody we do not already have a numeric id for. Handles change;
    # ids do not, which is why the id is what gets stored.
    unknown = [r["x_handle"] for r in rows if not r["user_id"]]
    if unknown:
        found = x_outreach.resolve_ids(client, unknown)
        for r in rows:
            if r["user_id"]:
                continue
            uid = found.get(r["x_handle"].lower())
            if uid:
                store.save_x_id(r["handle"], r["x_handle"], uid)
                r["user_id"] = uid
        missing = sum(1 for r in rows if not r["user_id"])
        print(f"resolved {len(found)} of {len(unknown)}"
              + (f", {missing} still unknown (renamed, deleted or suspended)" if missing else ""))

    if args.create:
        made = x_outreach.ensure_list(
            client, args.name, args.description, private=not args.public
        )
        if "error" in made:
            raise SystemExit(f"could not create the list: {made['error']}")
        list_id = made["id"]
        print(f"created list {list_id} ({'public' if args.public else 'private'})")
    elif args.list_id:
        list_id = args.list_id
    else:
        raise SystemExit("pass --create or --list-id")

    pending = [r for r in rows if r["user_id"] and not (r["listed_at"] or "")][: args.limit]
    if not pending:
        print("everyone reachable is already on the list")
        store.close()
        return 0

    by_id = {r["user_id"]: r for r in pending}

    def remember(user_id: str, ok: bool, detail: str) -> None:
        row = by_id.get(user_id)
        if ok and row:
            store.mark_listed(row["handle"])
        elif row:
            print(f"  skip {row['handle']} (@{row['x_handle']}): {detail}", file=sys.stderr)

    result = x_outreach.add_members(
        client, list_id, [r["user_id"] for r in pending], on_result=remember
    )
    remaining = sum(1 for r in rows if r["user_id"] and not (r["listed_at"] or "")) - result["added"]
    print(f"added {result['added']}, failed {result['failed']}, "
          f"{max(0, remaining)} left for the next run ({client.calls} API calls)")
    print(f"https://x.com/i/lists/{list_id}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
