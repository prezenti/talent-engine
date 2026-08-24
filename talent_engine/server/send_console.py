"""One person at a time, with the message already written and one button to say it went.

The outreach CSV made the list sendable. It did not make it *workable*: the
operator was still tabbing between a spreadsheet, an X window and a terminal,
and the terminal step -- recording that a message went out -- is the one that
gets skipped when a batch runs long. Skipping it is how somebody gets written
to twice next month, which is the single mistake here that cannot be taken
back.

So the bookkeeping is the button. Copy, send, mark, next. Nothing here composes
or sends anything; it is a window onto the same queue the CSV serves, with the
one write that keeps the queue honest.

Rendered live from the database rather than from a file on a schedule. A board
of applicants can be fifteen minutes stale without costing anything; a send
queue that is fifteen minutes stale hands you somebody you already wrote to.
"""

from __future__ import annotations

import html
import json
import sqlite3

from ..modes import outreach

# Enough to work through in a sitting, few enough that the page stays a queue
# rather than a directory. The rest are not hidden -- they are tomorrow's.
BATCH = 25

# Not a rule, a reading of the room: several hundred identical messages in one
# afternoon is a bulk send however true each one is, and X reads the pattern
# rather than the content. The page says so once you are past it and otherwise
# stays out of the way.
SOFT_DAILY_CAP = 30

QUEUE = """
SELECT sc.handle,
       sc.channels,
       r.x_handle, r.name, r.location,
       h.hook, h.repo, h.repo_url, h.repo_desc, h.basis,
       (SELECT MAX(total) FROM scores s WHERE s.handle = sc.handle) AS total
  FROM scouted sc
  JOIN profile_recon r ON r.handle = sc.handle
  JOIN outreach_hooks h ON h.handle = sc.handle
 WHERE sc.program = ?
   AND r.x_handle != ''
   AND COALESCE(h.sent_at, '') = ''
   AND h.hook != ''
   AND NOT EXISTS (SELECT 1 FROM submissions su
                    WHERE LOWER(su.handle) = LOWER(sc.handle))
 ORDER BY COALESCE(total, -1) DESC, sc.first_seen DESC, sc.handle ASC
 LIMIT ?
"""

COUNTS = """
SELECT
  (SELECT COUNT(*) FROM outreach_hooks WHERE COALESCE(sent_at,'') != '') AS sent,
  (SELECT COUNT(*) FROM outreach_hooks
    WHERE substr(COALESCE(sent_at,''), 1, 10) = ?) AS sent_today,
  (SELECT COUNT(*) FROM outreach_hooks h
     JOIN profile_recon r ON r.handle = h.handle
    WHERE r.x_handle != '' AND COALESCE(h.sent_at,'') = '' AND h.hook != '') AS waiting
"""

CSS = """
:root{--bg:#faf9f7;--card:#fff;--ink:#1a1a1a;--dim:#6b6b6b;--line:#e6e3dd;
      --accent:#0f5132;--warn:#8a5a00;--done:#7a7a7a;
      --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#141414;--card:#1d1d1d;--ink:#ececec;--dim:#9a9a9a;--line:#2e2e2e;
  --accent:#7ec9a2;--warn:#e0b063;--done:#6a6a6a}}
:root[data-theme="dark"]{--bg:#141414;--card:#1d1d1d;--ink:#ececec;--dim:#9a9a9a;
  --line:#2e2e2e;--accent:#7ec9a2;--warn:#e0b063;--done:#6a6a6a}
*{box-sizing:border-box}
body{margin:0;padding:1.5rem 1rem 4rem;background:var(--bg);color:var(--ink);
  font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:44rem;margin:0 auto}
h1{font-size:1.15rem;margin:0 0 .2rem}
.sub{color:var(--dim);font-size:.85rem;margin:0 0 1.4rem}
.pace{background:var(--card);border:1px solid var(--warn);color:var(--warn);
  border-radius:.5rem;padding:.6rem .8rem;font-size:.85rem;margin:0 0 1.2rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:.6rem;
  padding:1rem 1.1rem;margin:0 0 1rem}
.card.done{opacity:.45}
.top{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline}
.who{font-weight:600}
.at{font-family:var(--mono);font-size:.85rem}
.at a{color:var(--accent)}
.meta{color:var(--dim);font-size:.78rem;margin:.35rem 0 .7rem}
.meta .sep{opacity:.5;padding:0 .35rem}
.weak{color:var(--warn)}
.desc{font-size:.85rem;color:var(--dim);margin:0 0 .7rem;font-style:italic}
textarea.msg{display:block;width:100%;resize:vertical;background:transparent;
  color:inherit;border:1px solid var(--line);border-radius:.4rem;padding:.75rem;
  margin:0 0 .7rem;font:14px/1.5 var(--mono)}
textarea.msg:focus{outline:none;border-color:var(--accent)}
.row{display:flex;flex-wrap:wrap;gap:.5rem}
button,a.btn{font:inherit;font-size:.85rem;padding:.4rem .8rem;border-radius:.4rem;
  border:1px solid var(--line);background:var(--card);color:var(--ink);
  cursor:pointer;text-decoration:none;display:inline-block}
button:hover,a.btn:hover{border-color:var(--accent)}
button.mark{border-color:var(--accent);color:var(--accent)}
button[disabled]{cursor:default;color:var(--done);border-color:var(--line)}
.empty{color:var(--dim);text-align:center;padding:3rem 0}
footer{color:var(--dim);font-size:.78rem;margin-top:2rem;border-top:1px solid var(--line);
  padding-top:.8rem}
"""


def _card(row: sqlite3.Row, mark_path: str) -> str:
    handle = row["handle"]
    message = outreach.render(
        outreach.SHORT_MESSAGE, handle=handle, name=row["name"] or "",
        hook=row["hook"] or "", repo=row["repo"] or "",
    )
    full = outreach.render(
        outreach.FULL_MESSAGE, handle=handle, name=row["name"] or "",
        hook=row["hook"] or "", repo=row["repo"] or "",
    )
    e = html.escape
    score = f'{row["total"]:.1f}' if row["total"] is not None else "unscored"
    bits = [e(score)]
    if row["location"]:
        bits.append(e(row["location"]))
    if row["basis"] == "scout channel only":
        bits.append('<span class="weak">no repo named — weaker opener</span>')
    elif row["basis"]:
        bits.append(e(row["basis"]))
    meta = '<span class="sep">·</span>'.join(bits)

    # Their repository and what it says about itself, put directly above the
    # box so the one sentence worth adding is in front of you while you type
    # it. The draft is true; it is not evidence that anybody read anything, and
    # only a person can supply that.
    repo_line = ""
    if row["repo"]:
        repo_line = (
            f'<div class="desc"><a href="{e(row["repo_url"])}" target="_blank" '
            f'rel="noopener noreferrer">{e(row["repo"])}</a>'
            + (f' · {e(row["repo_desc"])}' if row["repo_desc"] else "")
            + "</div>"
        )

    return f"""<article class="card" id="c-{e(handle)}">
  <div class="top">
    <span class="who">{e(row["name"] or handle)}</span>
    <span class="at"><a href="https://x.com/{e(row["x_handle"])}" target="_blank"
      rel="noopener noreferrer">@{e(row["x_handle"])}</a></span>
    <span class="at"><a href="https://github.com/{e(handle)}" target="_blank"
      rel="noopener noreferrer">{e(handle)}</a></span>
  </div>
  <div class="meta">{meta}</div>
  {repo_line}
  <textarea class="msg" id="m-{e(handle)}" rows="9"
    spellcheck="true">{e(message)}</textarea>
  <textarea class="msg" id="f-{e(handle)}" rows="14" hidden>{e(full)}</textarea>
  <div class="row">
    <button type="button" data-copy="m-{e(handle)}">Copy</button>
    <button type="button" data-swap="{e(handle)}">Longer version</button>
    <a class="btn" href="https://x.com/{e(row["x_handle"])}" target="_blank"
       rel="noopener noreferrer">Open on X</a>
    <button type="button" class="mark" data-mark="{e(handle)}">Mark sent</button>
  </div>
</article>"""


# Kept inline because the page is served as one file behind a token and a
# second request for a script is a second thing that can 404 at the edge.
SCRIPT = """
document.addEventListener('click', async (ev) => {
  const copy = ev.target.closest('[data-copy]');
  if (copy) {
    // The box, not the original: whatever was typed into it is the message.
    const card = copy.closest('.card');
    const el = card.querySelector('textarea.msg:not([hidden])');
    try { await navigator.clipboard.writeText(el.value); }
    catch (e) { el.focus(); el.select(); }  // no secure context; ctrl-c still works
    const was = copy.textContent; copy.textContent = 'copied';
    setTimeout(() => { copy.textContent = was; }, 1200);
    return;
  }
  const swap = ev.target.closest('[data-swap]');
  if (swap) {
    const card = swap.closest('.card');
    const boxes = card.querySelectorAll('textarea.msg');
    boxes.forEach(b => { b.hidden = !b.hidden; });
    swap.textContent = boxes[0].hidden ? 'Shorter version' : 'Longer version';
    return;
  }
  const mark = ev.target.closest('[data-mark]');
  if (!mark) return;
  const handle = mark.dataset.mark;
  mark.disabled = true; mark.textContent = 'marking…';
  try {
    const res = await fetch(MARK_PATH, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({handle: handle}),
    });
    if (!res.ok) throw new Error(await res.text());
    const out = await res.json();
    mark.textContent = out.changed ? 'sent' : 'already recorded';
    document.getElementById('c-' + handle).classList.add('done');
  } catch (e) {
    // Never leave a failed mark looking successful: an unrecorded send is how
    // somebody gets written to twice.
    mark.disabled = false; mark.textContent = 'failed — retry';
  }
});
"""


def page(db_path: str, program: str, mark_path: str, today: str) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(QUEUE, (program, BATCH)).fetchall()
        counts = conn.execute(COUNTS, (today,)).fetchone()
    finally:
        conn.close()

    pace = ""
    if counts["sent_today"] >= SOFT_DAILY_CAP:
        pace = (
            f'<p class="pace">{counts["sent_today"]} sent today. Several hundred '
            "identical messages in one day reads as a bulk send to X whatever "
            "each one says — worth stopping here and picking it up tomorrow.</p>"
        )

    cards = "\n".join(_card(r, mark_path) for r in rows) or (
        '<p class="empty">Nothing waiting. Either everyone reachable has been '
        "written to, or tonight's scout has not run yet.</p>"
    )

    return f"""<title>Send queue</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<style>{CSS}</style>
<div class="wrap">
<h1>Send queue</h1>
<p class="sub">{counts["waiting"]} waiting · {counts["sent"]} written to ·
  {counts["sent_today"]} today · showing the next {len(rows)}</p>
{pace}
{cards}
<footer>The draft is the true part: it names the repository that surfaced them
and nothing else. <strong>Add a sentence of your own before you send.</strong>
The box is editable, their repository and its description are just above it, and
one line showing you actually looked is worth more than everything the draft
says. Then mark it, which is what stops anyone being written to twice.</footer>
</div>
<script>const MARK_PATH = {json.dumps(mark_path)};{SCRIPT}</script>
"""


def mark(db_path: str, handle: str, when: str) -> bool:
    """Record a send. Returns False if the handle is not in the queue at all.

    A separate short-lived connection rather than the service's own: this runs
    on a request thread, and the one write on this page is not worth making the
    intake service's connection shared to accommodate.
    """
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        cur = conn.execute(
            "UPDATE outreach_hooks SET sent_at = ? "
            "WHERE handle = ? AND COALESCE(sent_at,'') = ''",
            (when, handle),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
