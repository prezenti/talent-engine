"""Turn a scouted handle into something an operator can actually send.

Recon answers *where* somebody can be reached. This answers *what to say* --
specifically the one line that has to be true per person: how they came to our
attention. A cold message that opens "I saw your GitHub" is indistinguishable
from a scrape, because it is one. A cold message that names the repository and
says which channel surfaced it is a claim the recipient can check, and the
programme's whole argument is that it can be checked.

Nothing here is inferred or embellished. The channel comes from the scout's own
record of how the handle arrived; the repository comes either from the evidence
the scorer already cited, or from the person's own public repository list. If
neither yields anything, the hook is empty and the row says so rather than
inventing a reason.
"""

from __future__ import annotations

import json
from typing import Any

# How the scout found them, in the recipient's language rather than ours. Order
# matters: a handle can arrive through more than one channel, and the most
# specific true statement is the one worth making.
CHANNEL_PHRASE = [
    ("contributors", "a merged pull request into one of the repositories we watch"),
    ("originators", "your own repositories, in the space we were searching"),
    ("adjacent", "your contributions to a small project in that space"),
]

# Repositories that tell you nothing about somebody. A profile README, dotfiles
# and a personal site are the three things almost everyone has, so leading with
# one reads as though nothing was actually read.
_UNINTERESTING = {"dotfiles", "config", "configs", "blog", "portfolio", "website", "homepage"}


def channel_phrase(channels: str) -> str:
    """The most specific true description of how a handle arrived."""
    present = {c.strip() for c in (channels or "").split(",") if c.strip()}
    for key, phrase in CHANNEL_PHRASE:
        if key in present:
            return phrase
    return ""


def repo_from_evidence(payload: str | None) -> dict[str, str]:
    """The repository the scorer itself cited, if this person was scored.

    Preferred over a fresh lookup because it is the repository that actually
    earned the number -- so the sentence in the message and the sentence in the
    dossier are about the same work.
    """
    if not payload:
        return {}
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    for dim in doc.get("dimensions") or []:
        if dim.get("key") != "shipping_agency":
            continue
        for ev in dim.get("evidence") or []:
            url = (ev.get("url") or "").strip()
            if "/github.com/" not in url or url.rstrip("/").count("/") != 4:
                continue  # profile links and tab links, not a repository
            return {
                "repo": url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1],
                "repo_url": url,
                "repo_desc": (ev.get("detail") or "").strip(),
                "basis": "scored evidence",
            }
    return {}


def repo_from_profile(client, handle: str) -> dict[str, str]:
    """Their own most recently pushed original repository that says what it is.

    Forks are skipped because the programme does not count them, and a repo
    with no description is skipped because there is nothing to say about it.
    """
    repos = client.get(
        f"/users/{handle}/repos", {"type": "owner", "sort": "pushed", "per_page": 30}
    ) or []
    best = None
    for r in repos:
        if r.get("fork") or r.get("archived"):
            continue
        desc = (r.get("description") or "").strip()
        name = (r.get("name") or "").strip()
        if not desc or not name:
            continue
        if name.lower() in _UNINTERESTING or name.lower() == handle.lower():
            continue
        # Most recent wins; stars break ties, and only ties. Ranking by stars
        # would reintroduce exactly the popularity signal the rubric refuses.
        key = (r.get("pushed_at") or "", r.get("stargazers_count") or 0)
        if best is None or key > best[0]:
            best = (key, {
                "repo": r.get("full_name") or f"{handle}/{name}",
                "repo_url": r.get("html_url") or f"https://github.com/{handle}/{name}",
                "repo_desc": desc,
                "basis": "public repository list",
            })
    return best[1] if best else {}


def hook_for(client, handle: str, channels: str, payload: str | None) -> dict[str, Any]:
    """Everything needed to open a message to this person, or blanks."""
    found = repo_from_evidence(payload) or repo_from_profile(client, handle)
    phrase = channel_phrase(channels)
    out = {
        "handle": handle,
        "repo": found.get("repo", ""),
        "repo_url": found.get("repo_url", ""),
        "repo_desc": found.get("repo_desc", "")[:180],
        "basis": found.get("basis", ""),
        "channel_phrase": phrase,
        "hook": "",
    }
    # Written to follow "You came up through ", so it is a sentence fragment
    # rather than a label: channel first because that is the honest order --
    # the channel is how they arrived, the repository is which work it was.
    if out["repo"] and phrase:
        out["hook"] = f"{phrase} — specifically {out['repo']}"
    elif out["repo"]:
        out["hook"] = f"your work on {out['repo']}"
    elif phrase:
        out["hook"] = phrase
    return out


# The message itself, kept here rather than in the operator's notes so that it
# is version-controlled, reviewable in a diff, and rendered identically for
# every recipient. Both versions say the same true things; the short one exists
# because a long first message into a request inbox reads as a sales sequence
# regardless of what it contains.
#
# Every factual claim below is checkable against README.md and the terms. If
# the programme's numbers change, these change with them -- a message quoting
# superseded terms is a promise the programme is not making.
SHORT_MESSAGE = """Hi {name} — zoz from Prezenti. You came up through {hook}.

We fund builders on shipped evidence rather than network: 5 places, 4 months, $1,400 each in tooling, for people building agent infrastructure, protocol and on-chain work. No equity, you keep all IP.

Terms, rubric and the scoring code are public so you can check us before replying: sponsorships.prezenti.xyz

Genuinely just an invitation to apply."""

FULL_MESSAGE = """Hi {name} — I'm zoz, from Prezenti. You came up through {hook}. Nothing here was scraped beyond what GitHub already publishes, and I'm writing for one reason: to invite you to apply.

We back builders on evidence of what they've actually shipped. This is a small trial — 5 places, 4 months, $1,400 each in tooling: Claude Max 20x, ChatGPT Pro, and a $200 flexible allowance. We're looking for people building agent infrastructure, protocol engineering and on-chain tooling, deliberately including people who have never touched Celo.

No equity, and you keep all IP. No exclusivity, withdraw at any time. In return we ask a good-faith 2% pledge on revenue and grants the sponsored work actually earns — capped at $14,000, expiring after 36 months, pro-rated by the months you take.

The terms, the rubric you'd be scored against and the code that does the scoring are all public: github.com/prezenti/talent-engine. You can run it and reproduce your own number before deciding whether we're worth your time.

Apply: sponsorships.prezenti.xyz

Everyone who applies gets their score, the evidence behind it, and feedback — selected or not.

— zoz"""


def render(template: str, *, handle: str, name: str, hook: str) -> str:
    """Fill a message for one person, or return "" if there is nothing true to say.

    A missing hook is a refusal, not a blank to paper over: the opening line is
    the programme's claim that it found this person by reading public work, and
    a message that cannot make that claim should not be sent.
    """
    if not hook:
        return ""
    return template.format(name=(name or "").strip() or handle, hook=hook)
