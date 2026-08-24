#!/usr/bin/env python3
"""Build the outreach list: everyone scouted who published a way to reach them.

Recon found the accounts. This decides who is worth a message, works out the
one true sentence about each of them, and writes the list an operator sends
from. It sends nothing itself -- there is no X credential on this machine and
there should not be, because a cold message going out under automation is
exactly the thing the programme's terms promise these people is not happening.

Excluded on purpose:
  * anyone who has already applied -- they are in the applicant board, and a
    "come and apply" message to somebody who applied last week is an advert for
    not paying attention;
  * anyone already marked contacted, unless --resend;
  * anyone recon could not find an account for, since there is nowhere to send.

Usage:
  outreach.py --program prezenti-sponsorship-trial            # build + write
  outreach.py --program … --mark handle1,handle2               # record as sent
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talent_engine.github.auth import auth_from_env  # noqa: E402
from talent_engine.github.client import BudgetExhausted, GitHubClient, ResponseCache  # noqa: E402
from talent_engine.modes import outreach  # noqa: E402
from talent_engine.store.db import Store  # noqa: E402

RUNTIME = Path.home() / "talent-engine-runtime"

# Everyone reachable, best score first, then most recently discovered. The
# score is a tiebreak for attention rather than a gate: only a fraction of the
# scouted set has ever been scored, and refusing to message the unscored would
# throw away most of the list for a reason that is about our budget, not them.
QUEUE = """
SELECT sc.handle,
       sc.channels,
       sc.first_seen,
       r.x_handle,
       r.name,
       r.location,
       r.bio,
       (SELECT MAX(total) FROM scores s WHERE s.handle = sc.handle) AS total,
       (SELECT payload FROM scores s WHERE s.handle = sc.handle
         ORDER BY total DESC LIMIT 1) AS payload,
       (SELECT COUNT(*) FROM submissions su
         WHERE LOWER(su.handle) = LOWER(sc.handle)) AS applied,
       h.hook, h.repo, h.repo_url, h.repo_desc, h.basis, h.sent_at,
       d.dm_status
  FROM scouted sc
  JOIN profile_recon r ON r.handle = sc.handle
  LEFT JOIN outreach_hooks h ON h.handle = sc.handle
  LEFT JOIN x_delivery d ON d.handle = sc.handle
 WHERE sc.program = ?
   AND r.x_handle != ''
   AND COALESCE(d.dm_status, '') != 'refused'
 ORDER BY COALESCE(total, -1) DESC, sc.first_seen DESC
"""

COLUMNS = [
    "Handle", "X", "Send to", "Draft DM", "Why we found you", "Repo",
    "What it is", "Basis", "Score", "Channels", "Name", "Location",
    "GitHub", "Contacted",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--program", required=True)
    ap.add_argument("--db", default=str(RUNTIME / "talent_engine.db"))
    ap.add_argument("--cache", default=str(RUNTIME / "github-cache.sqlite"))
    ap.add_argument("--out", default=str(RUNTIME / "outreach.csv"))
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--limit", type=int, default=0, help="0 = everyone")
    ap.add_argument("--resend", action="store_true",
                    help="include people already marked contacted")
    ap.add_argument("--mark", default="",
                    help="comma-separated handles to record as contacted, then exit")
    ap.add_argument("--closed", default="",
                    help="comma-separated handles whose DMs are shut; they leave "
                         "the queue for good rather than being re-offered")
    ap.add_argument("--unmark", default="",
                    help="put handles back in the queue -- for a mis-tapped button, "
                         "not for writing to somebody a second time")
    args = ap.parse_args()

    store = Store(args.db)

    if args.mark:
        for handle in [h.strip().lstrip("@") for h in args.mark.split(",") if h.strip()]:
            store.mark_contacted(handle)
            print(f"marked contacted: {handle}")
        store.close()
        return 0

    if args.closed:
        for handle in [h.strip().lstrip("@") for h in args.closed.split(",") if h.strip()]:
            store.record_dm(handle, "refused", "DMs closed, checked by hand")
            if not store.x_delivery_for(handle):
                # record_dm only updates; somebody never resolved has no row yet.
                store.save_x_id(handle, "", "")
                store.record_dm(handle, "refused", "DMs closed, checked by hand")
            print(f"closed to DMs: {handle}")
        store.close()
        return 0

    if args.unmark:
        # The button will get mis-tapped. Without this the only remedy is
        # editing the database by hand, which is how a careful person ends up
        # doing something careless at speed.
        for handle in [h.strip().lstrip("@") for h in args.unmark.split(",") if h.strip()]:
            cur = store.conn.execute(
                "UPDATE outreach_hooks SET sent_at = '' WHERE handle = ?", (handle,)
            )
            store.conn.commit()
            print(f"back in the queue: {handle}" if cur.rowcount
                  else f"not found: {handle}")
        store.close()
        return 0

    rows = [dict(r) for r in store.conn.execute(QUEUE, (args.program,)).fetchall()]
    rows = [r for r in rows if not r["applied"]]
    if not args.resend:
        rows = [r for r in rows if not (r["sent_at"] or "")]
    if args.limit:
        rows = rows[: args.limit]

    client = GitHubClient(
        auth=auth_from_env(dict(os.environ)),
        cache=ResponseCache(args.cache),
        budget=args.budget,
    )

    built = 0
    for r in rows:
        if r["hook"]:
            continue  # already worked out; a repo does not change hourly
        try:
            found = outreach.hook_for(client, r["handle"], r["channels"], r["payload"])
        except BudgetExhausted as exc:
            print(f"stopped early: {exc}", file=sys.stderr)
            break
        except Exception as exc:
            print(f"skip {r['handle']}: {exc}", file=sys.stderr)
            continue
        store.save_hook(found)
        r.update({k: found.get(k, "") for k in
                  ("hook", "repo", "repo_url", "repo_desc", "basis")})
        built += 1

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([
                r["handle"],
                f'@{r["x_handle"]}',
                f'https://x.com/{r["x_handle"]}',
                outreach.render(
                    outreach.SHORT_MESSAGE,
                    handle=r["handle"], name=r["name"] or "",
                    hook=r["hook"] or "", repo=r["repo"] or "",
                ),
                r["hook"] or "",
                r["repo"] or "",
                r["repo_desc"] or "",
                r["basis"] or "",
                f'{r["total"]:.2f}' if r["total"] is not None else "",
                (r["channels"] or "").replace(",", "; "),
                r["name"] or "",
                r["location"] or "",
                f'https://github.com/{r["handle"]}',
                (r["sent_at"] or "")[:10],
            ])

    reachable = len(rows)
    with_hook = sum(1 for r in rows if r["hook"])
    print(f"{reachable} reachable, {with_hook} with a specific hook "
          f"({built} built this run, {client.stats})")
    print(f"wrote {out}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
