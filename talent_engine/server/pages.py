"""The public page, rendered from an explicit route table.

There is no path joining anywhere in here. A request path is looked up in a
dict of exact strings, and a miss is a 404. Directory traversal is not
defended against; it is unrepresentable, which is the only version of that
defence worth having on a host that also holds credentials.

The form itself stays on Tally, in an iframe. Nothing a member of the public
types ever reaches this process — the only thing that arrives here is a signed
webhook from Tally's servers after the fact.

**Everything is embedded.** The page's own CSP allows no external requests, so
the logo is inlined as SVG markup and the brand fonts as data URIs rather than
pulled from a CDN. That costs about 90KB and buys a page that renders
identically with no third party able to see who visited.

Palette and typography are Prezenti's own, read from their stylesheet rather
than eyeballed: dark forest #112122, cream #fef4ee, orange #eb4b24, mint
#68a9a3, with Outfit for titles and DM Sans for text.
"""

from __future__ import annotations

import base64
import html
import os
import re
import urllib.parse
from functools import lru_cache
from pathlib import Path

# The engine's own repository. A deployment that publishes its own fork sets
# `page.repo_url` in its program config — the applicant is told to clone this
# and reproduce their score, so it has to be the repository they can actually
# read the deployed rubric in.
REPO_URL = "https://github.com/prezenti/talent-engine"
ASSETS = Path(__file__).resolve().parent / "assets"
_INVITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


@lru_cache(maxsize=8)
def _asset_b64(name: str) -> str:
    try:
        return base64.b64encode((ASSETS / name).read_bytes()).decode()
    except OSError:
        return ""


@lru_cache(maxsize=4)
def _logo_svg() -> str:
    """The logo as inline markup, so CSS can recolour it for dark mode."""
    try:
        raw = (ASSETS / "prezenti-logo.svg").read_text()
    except OSError:
        return ""
    # Drop the XML prolog; it is invalid inside an HTML body.
    if raw.startswith("<?xml"):
        raw = raw.split("?>", 1)[-1]
    # The file hardcodes its fill; let the page control it instead.
    raw = raw.replace("fill: #112122;", "fill: currentColor;")
    return raw.replace("<svg ", '<svg class="logo" role="img" aria-label="Prezenti" ', 1)


def _font_face() -> str:
    dm = _asset_b64("dmsans-400.woff2")
    outfit = _asset_b64("outfit-500.woff2")
    blocks = []
    if outfit:
        blocks.append(
            "@font-face{font-family:'Outfit';font-style:normal;font-weight:400 700;"
            "font-display:swap;src:url(data:font/woff2;base64," + outfit + ") format('woff2');}"
        )
    if dm:
        blocks.append(
            "@font-face{font-family:'DM Sans';font-style:normal;font-weight:400 500;"
            "font-display:swap;src:url(data:font/woff2;base64," + dm + ") format('woff2');}"
        )
    return "".join(blocks)


# Prezenti's tokens, taken from prezenti.webflow.shared.css.
PAGE_CSS = """
:root {
  --forest: #112122;
  --cream: #fef4ee;
  --orange: #eb4b24;
  --orange-deep: #dc331a;
  --peach: #f9c7af;
  --peach-pale: #fce5d8;
  --mint: #68a9a3;
  --mint-light: #b4dbd4;
  --green-600: #346d6a;

  --bg: var(--cream);
  --surface: #ffffff;
  --ink: var(--forest);
  --muted: #4c5a5a;
  --line: #e8ded6;
  --accent: var(--orange-deep);
  --hero-bg: var(--forest);
  --hero-ink: #ffffff;
  --hero-muted: #b7c6c4;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0c1516;
    --surface: #142425;
    --ink: #f3ede8;
    --muted: #9fb0ae;
    --line: #22383a;
    --accent: #ff7d58;
    --hero-bg: #081011;
    --hero-ink: #fef4ee;
    --hero-muted: #9fb0ae;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0c1516; --surface: #142425; --ink: #f3ede8; --muted: #9fb0ae;
  --line: #22383a; --accent: #ff7d58; --hero-bg: #081011;
  --hero-ink: #fef4ee; --hero-muted: #9fb0ae; color-scheme: dark;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.7 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  -webkit-font-smoothing: antialiased;
}

/* --- hero ------------------------------------------------------------ */
/* Prezenti lead with a dark forest band and reversed type; the page opens
   the way their site does rather than as a white document with a logo on it. */
.hero { background: var(--hero-bg); color: var(--hero-ink); padding: 3.5rem 0 4.5rem; }
.hero .inner { max-width: 52rem; margin: 0 auto; padding: 0 1.5rem; }
.logo {
  width: min(19rem, 62vw); height: auto; display: block;
  color: var(--hero-ink); margin-bottom: 4rem;
}
h1 {
  font-family: 'Outfit', sans-serif;
  font-size: clamp(2.4rem, 6.4vw, 3.9375rem);
  line-height: 1.1;
  /* Their display scale carries positive tracking at weight 500 — the
     opposite of the usual big-heading instinct, and it is what makes the
     type read as theirs. */
  letter-spacing: 0.06em;
  font-weight: 500;
  margin: 0 0 1.5rem;
  max-width: 16ch;
}
.lede {
  font-size: clamp(1.1rem, 2.2vw, 1.3rem);
  line-height: 1.55; color: var(--hero-muted);
  margin: 0; max-width: 40ch;
}
.chips { margin: 2.5rem 0 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 0.5rem; }
.chips li {
  border: 1px solid rgba(255,255,255,0.28); color: var(--hero-muted);
  border-radius: 999rem; padding: 0.4rem 0.95rem; font-size: 0.9rem;
}

/* --- body ------------------------------------------------------------ */
.wrap { max-width: 52rem; margin: 0 auto; padding: 4rem 1.5rem 5rem; }
h2 {
  font-family: 'Outfit', sans-serif;
  font-size: clamp(1.6rem, 3.4vw, 2.4375rem);
  line-height: 1.1; letter-spacing: 0.02em; font-weight: 500;
  margin: 4rem 0 1.25rem;
}
h2:first-of-type { margin-top: 0; }
p { margin: 0 0 1.15rem; max-width: 46rem; }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }

/* Numbered signals: the four things that carry the weight. */
ol.signals { list-style: none; counter-reset: s; padding: 0; margin: 1.75rem 0 1.5rem; }
ol.signals li {
  counter-increment: s; position: relative;
  padding: 1.15rem 0 1.15rem 3.25rem;
  border-top: 1px solid var(--line);
}
ol.signals li:last-child { border-bottom: 1px solid var(--line); }
ol.signals li:before {
  content: counter(s, decimal-leading-zero);
  position: absolute; left: 0; top: 1.15rem;
  font-family: 'Outfit', sans-serif; font-size: 0.95rem; font-weight: 500;
  color: var(--mint); letter-spacing: 0.06em;
}
ol.signals strong { font-weight: 500; }

.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 1rem; padding: 1.75rem 1.85rem; margin: 2rem 0;
}
.card.accent { border-left: 4px solid var(--accent); }
.card h3 {
  font-family: 'Outfit', sans-serif; font-weight: 500; font-size: 1.2rem;
  letter-spacing: 0.02em; margin: 0 0 0.75rem;
}

code {
  font: 0.85em ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(104,169,163,0.18); padding: 0.15em 0.45em; border-radius: 4px;
}
pre {
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 1.1rem 1.2rem; overflow-x: auto; font-size: 0.85rem; margin: 1.1rem 0 0;
}
pre code { background: none; padding: 0; }

.note {
  border-left: 3px solid var(--mint); padding: 0.15rem 0 0.15rem 1.1rem;
  color: var(--muted); margin: 1.5rem 0;
}

.form-frame {
  width: 100%; min-height: 46rem; border: 1px solid var(--line);
  border-radius: 1rem; background: var(--surface); margin-top: 1.5rem;
}

footer { background: var(--hero-bg); color: var(--hero-muted); padding: 3rem 0; }
footer .inner { max-width: 52rem; margin: 0 auto; padding: 0 1.5rem; font-size: 0.95rem; }
footer a { color: var(--peach); }
footer .mark { width: 9rem; height: auto; color: var(--hero-ink); opacity: 0.85; margin-bottom: 1.5rem; display: block; }
"""


def _embed(form_id: str, terms_version: str = "") -> str:
    """The Tally iframe, or an honest placeholder when no form is configured.

    The version is still present in the URL for human/debug visibility, but the
    load-bearing value is in the checkbox option text generated from the same
    terms release. Tally exposes no hidden-field API.
    """
    if not form_id:
        return (
            '<div class="card"><p><strong>The application form is not connected '
            "yet.</strong> Set <code>TALLY_FORM_ID</code> on the intake service "
            "and this section becomes the form.</p></div>"
        )
    fid = html.escape(form_id, quote=True)
    version = ""
    if terms_version:
        version = "&terms_version=" + urllib.parse.quote(terms_version, safe="")
    return (
        f'<iframe class="form-frame" src="https://tally.so/embed/{fid}'
        "?alignLeft=1&hideTitle=1&transparentBackground=1&dynamicHeight=1"
        f'{version}" loading="lazy" title="Application form"></iframe>'
    )


DEFAULT_COPY = {
    "headline": "Sponsorship for people who ship",
    "lede": (
        "We back builders on evidence of what they have actually shipped — not "
        "on where they studied, who they know, or how many stars a repository "
        "has."
    ),
    "footer": "",
}


def _terms_block(overlay) -> str:
    """The terms, rendered from the policy the service validated at startup.

    Written once, in the policy, and displayed from there — so the page cannot
    say something the code does not enforce.
    """
    if overlay is None:
        return ""
    items = "".join(f"<li>{html.escape(line)}</li>" for line in overlay.terms_summary())
    uri = html.escape(getattr(overlay, "terms_uri", ""), quote=True)
    digest = html.escape(overlay.terms_digest())
    release = html.escape(str(overlay.terms_release.get("version", "")))
    link = (
        f'<p>Canonical terms: <a href="{uri}">{release}</a> '
        f'<code>terms-version: {digest}</code>.</p>'
        if uri and release
        else ""
    )
    return (
        '<h2>The terms</h2>\n<div class="card"><ul class="signals">'
        f"{items}</ul>"
        f"{link}"
        "<p>These are enforced by the code that runs this programme: a policy "
        "that takes equity, leaves the give-back uncapped, lets it run forever, "
        "or fails to state what we owe you will not load, and the service will "
        "not start.</p></div>"
    )


def _status_block(overlay, form_id: str, *, ignore_env_close: bool = False) -> str:
    """Say plainly whether applications are open, and show the form only if so.

    A page that renders an application form is a page that says "apply". If the
    programme is not taking applications, it must not look like it is.
    """
    if not ignore_env_close and _env_applications_closed():
        closed_at = html.escape(os.environ.get("TE_APPLICATIONS_CLOSED_AT", "").strip())
        suffix = f" This round closed at {closed_at}." if closed_at else ""
        return (
            '<div class="card accent"><h3>Applications are closed</h3>'
            f"<p>This round is not accepting new applications.{suffix} The "
            "rubric and the code remain public, and you can still reproduce "
            "your own score.</p></div>"
        )
    if overlay is not None and not overlay.is_open:
        return (
            '<div class="card accent"><h3>Applications are closed</h3>'
            "<p>This round is not accepting applications. The rubric and the "
            "code remain public, and you can still reproduce your own score.</p>"
            "</div>"
        )
    closing = ""
    close_label = "" if ignore_env_close else _applications_close_label(overlay)
    if close_label:
        closing = (
            f'<p class="note">Applications close when the fifth place is '
            f"filled, or {html.escape(close_label)}, whichever "
            "comes first.</p>"
        )
    version = overlay.terms_digest() if overlay is not None else ""
    return closing + _embed(form_id, version)


def _env_applications_closed() -> bool:
    value = os.environ.get("TE_APPLICATIONS_CLOSED", "").strip().lower()
    return value in {"1", "true", "yes", "closed"}


def _applications_close_label(overlay) -> str:
    override = os.environ.get("TE_APPLICATIONS_CLOSE_AT", "").strip()
    if override:
        return override
    if overlay is not None and overlay.applications_close:
        return str(overlay.applications_close)
    return ""


def _invite_path() -> str:
    token = os.environ.get("TE_INVITE_APPLY_TOKEN", "").strip()
    if not token or not _INVITE_TOKEN_RE.fullmatch(token):
        return ""
    return f"/invite/{token}.html"


def landing_page(
    program_name: str,
    form_id: str,
    copy: dict[str, str] | None = None,
    overlay=None,
    invite: bool = False,
) -> bytes:
    """Render the application page.

    `copy` comes from the program config so the running organisation owns the
    words it publishes under its own domain, and can change them without a code
    change. Everything is escaped: the copy is trusted-ish, but it is read from
    a file on disk and rendered into a public page, which is not a combination
    to be casual about.
    """
    text = {**DEFAULT_COPY, **(copy or {})}
    headline = html.escape(text["headline"])
    lede = html.escape(text["lede"])
    extra_footer = f"<p>{html.escape(text['footer'])}</p>" if text.get("footer") else ""
    program = html.escape(program_name)
    # A deployment publishing its own fork must point applicants at that fork:
    # cloning the upstream engine and running the deployed programme's key
    # against it reproduces a different number.
    repo_url = html.escape(text.get("repo_url") or REPO_URL, quote=True)
    program_key = html.escape(
        getattr(overlay, "scoring_program", "") or getattr(overlay, "key", "") or "PROGRAM"
    )
    invite_note = (
        '<p class="note"><strong>Private invitation.</strong> The public round '
        "is closed; use this form only if this link was sent to you directly.</p>"
        if invite
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{program} — apply</title>
<meta name="description" content="{headline}. Scored on public code activity
against a rubric you can read and reproduce.">
<style>{_font_face()}{PAGE_CSS}</style>
</head>
<body>

<section class="hero">
  <div class="inner">
    {_logo_svg()}
    <h1>{headline}</h1>
    <p class="lede">{lede}</p>
    <ul class="chips">
      <li>Open to builders anywhere</li>
      <li>Public rubric</li>
      <li>Evidence-linked scoring</li>
    </ul>
  </div>
</section>

<div class="wrap">

<h2>How you are assessed</h2>
<p>Your public code activity is scored against a fixed rubric. Four signals
carry most of the weight.</p>
<ol class="signals">
  <li><strong>Origination.</strong> You create things that did not exist.
      Forking is free; originating is not.</li>
  <li><strong>Finishing.</strong> Most side projects die around commit three.
      Releases, documentation, a deployed URL — the last ten percent, the part
      with no dopamine in it.</li>
  <li><strong>Cadence.</strong> You keep showing up. Distinct weeks of
      activity, which is much harder to inflate than a commit count.</li>
  <li><strong>Acceptance.</strong> Other people merged your work, across
      several different projects.</li>
</ol>
<p class="note">Stars and follower counts are not scored at any weight. They
measure access, and access is exactly what this is built to look past.</p>

<div class="card accent">
<h3>You can check our work</h3>
<p>The rubric is published in full, along with the code that applies it. Clone
it and reproduce your own score:</p>
<pre><code>git clone {repo_url}
cd talent-engine
python3 -m talent_engine.cli score \\
    --program {program_key} --handles YOUR_GITHUB_HANDLE</code></pre>
<p style="margin-top:1.1rem">If a number looks wrong to you, you can show us
exactly where — which is the point of publishing it. Every score comes with
linked evidence for each component; a score with no evidence behind it cannot
be produced.</p>
</div>

<h2>A score is not a decision</h2>
<p>The automated score produces a shortlist. People decide, after reading the
evidence and talking to you. No one is funded or refused by a number.</p>
<p>The checks that flag manipulated profiles are published alongside the
rubric — including an honest account of what they do not yet catch. We would
rather tell you that than imply a precision the method does not have.</p>

<h2>What we collect</h2>
<p>Public code activity only. Nothing about your background is inferred —
there is nowhere in the system to record an inferred trait. Anything about
your circumstances enters because you chose to tell us. Your contact details
are stored separately from everything used for assessment, and never appear in
an assessment record.</p>

{_terms_block(overlay)}

<h2>Apply</h2>
{invite_note}
{_status_block(overlay, form_id, ignore_env_close=invite)}

</div>

<footer>
  <div class="inner">
    {_logo_svg().replace('class="logo"', 'class="mark"', 1)}
    {extra_footer}
    <p>Scoring engine: <a href="{REPO_URL}">talent-engine</a> — open source,
    including the rubric, the anti-gaming checks, and their known limits.</p>
  </div>
</footer>

</body>
</html>
""".encode()


def routes(
    program_name: str,
    form_id: str | None = None,
    copy: dict[str, str] | None = None,
    overlay=None,
) -> dict[str, tuple[str, bytes]]:
    """Exact path -> (content type, body). Anything not in here is a 404."""
    form_id = form_id if form_id is not None else os.environ.get("TALLY_FORM_ID", "")
    page = landing_page(program_name, form_id, copy, overlay)
    table = {
        "/": ("text/html; charset=utf-8", page),
        "/apply": ("text/html; charset=utf-8", page),
    }
    invite_path = _invite_path()
    if invite_path:
        invite_form_id = os.environ.get("TE_INVITE_TALLY_FORM_ID", "").strip() or form_id
        table[invite_path] = (
            "text/html; charset=utf-8",
            landing_page(program_name, invite_form_id, copy, overlay, invite=True),
        )
    return table
