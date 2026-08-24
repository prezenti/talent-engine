"""The outreach list as a sheet tab: who is reachable, and what to say to them.

The scouted feed answers "who did the scout find". This answers the narrower,
more actionable question: of those, who published a way to be reached, has not
already applied, and has not already been written to.

Deliberately not the same tab. The scouted feed is append-only because a hand-
typed outreach column sits beside it, and its ordering is a contract. This one
is a worklist: it re-sorts as scores arrive, shrinks as people are contacted,
and nothing should ever be typed beside it. `Contacted` is written by
`tools/outreach.py --mark`, so the state lives in the database where a second
pass can see it, not in a cell that a sort would strand.

The drafted message is not in this feed. A CSV cell containing four paragraphs
of newlines is a spreadsheet problem, and the local `outreach.csv` already
carries it for the operator who is actually sending.
"""

from __future__ import annotations

import csv
import io
import sqlite3

COLUMNS = [
    "Handle",          # the join key, first, as in every other feed
    "X",
    "Send to",
    "Why we found you",
    "What it is",
    "Basis",
    "Score",
    "Channels",
    "Name",
    "Location",
    "GitHub",
    "Contacted",
]

# Reachable, not yet applied, best-scored first. Unscored people are kept:
# only a fraction of the scouted set has ever been scored, and dropping the
# rest would be a decision about our API budget dressed up as a judgement
# about them.
SQL = """
SELECT sc.handle,
       sc.channels,
       sc.first_seen,
       r.x_handle, r.name, r.location,
       h.hook, h.repo, h.repo_desc, h.basis, h.sent_at,
       (SELECT MAX(total) FROM scores s WHERE s.handle = sc.handle) AS total,
       (SELECT COUNT(*) FROM submissions su
         WHERE LOWER(su.handle) = LOWER(sc.handle)) AS applied
  FROM scouted sc
  JOIN profile_recon r ON r.handle = sc.handle
  LEFT JOIN outreach_hooks h ON h.handle = sc.handle
 WHERE sc.program = ?
   AND r.x_handle != ''
 ORDER BY COALESCE(total, -1) DESC, sc.first_seen DESC, sc.handle ASC
"""


def csv_for(db_path: str, program: str, include_contacted: bool = True) -> str:
    """Everyone worth a message, most promising first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(SQL, (program,)).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        if r["applied"]:
            continue  # they are on the applicant board; inviting them is noise
        if not include_contacted and (r["sent_at"] or ""):
            continue
        w.writerow([
            r["handle"],
            f'@{r["x_handle"]}',
            f'https://x.com/{r["x_handle"]}',
            r["hook"] or "",
            r["repo_desc"] or "",
            r["basis"] or "",
            f'{r["total"]:.2f}' if r["total"] is not None else "",
            (r["channels"] or "").replace(",", "; "),
            r["name"] or "",
            r["location"] or "",
            f'https://github.com/{r["handle"]}',
            (r["sent_at"] or "")[:10],
        ])
    return buf.getvalue()
