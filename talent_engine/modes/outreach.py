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
        # True, but it names no work -- which is most of what makes the message
        # worth sending. Said out loud in the Basis column rather than left as
        # a blank, so a weak opener is a choice the operator makes knowingly.
        out["hook"] = phrase
        out["basis"] = "scout channel only"
    return out


# The message. Kept here rather than in the operator's notes so that it is
# version-controlled, reviewable in a diff, and so that a change to the
# programme's numbers changes what strangers are told.
#
# Written to be short, because the tell is length. A cold message that opens
# with a greeting, states a value proposition, enumerates benefits in threes
# and closes with a call to action is recognisable as a template from the first
# line, and everything after that line is read as one. A person who genuinely
# found your repository writes three sentences and stops.
#
# So: no em dashes, no lists of three, no "genuinely", no sign-off flourish.
# Contractions. One concrete thing (the repository), one offer, one link. The
# reader can ask for the rest, and if they do not, the rest would not have
# helped.
#
# Every factual claim is checkable against README.md and the terms.

# Four openings that differ in shape rather than in synonyms. Rotating
# "came across" / "stumbled on" / "found" produces 300 messages that are
# obviously one message, which is the problem being solved rather than a
# solution to it. These start in different places: the work, the ask, the
# programme, the reason.
OPENINGS = [
    "Hi {name}, found you through {repo}.",
    "Hi {name}, cold message, sorry. I found you through {repo}.",
    "Hi {name}, {repo} came up while I was looking at what people are shipping in agent infra.",
    "Hi {name}, I went looking for people building agent infrastructure and ended up at {repo}.",
]

# Two bodies rather than one, for the same reason as the openings: the body is
# the longest part and therefore the part that gives a template away when two
# recipients compare messages.
BODIES = [
    """We're funding 5 people for 4 months. About $1,400 each in tooling, Claude Max and ChatGPT Pro plus some cash. No equity, we take no IP, and you can leave whenever you want.

sponsorships.prezenti.xyz""",
    """We've got 5 sponsorships going. 4 months each, roughly $1,400 of tooling per person (Claude Max, ChatGPT Pro, and a bit of cash on top). We take no equity and no IP, and you can pull out at any point.

sponsorships.prezenti.xyz""",
]

CLOSINGS = [
    "The terms and the code that does the scoring are public, if you'd rather check us before replying.",
    "Happy to answer anything. The scoring code is public if you want to see how it works.",
    "You can read the terms and reproduce your own score before deciding we're worth the time.",
    "No pressure either way.",
]

# The longer one, for somebody who replies asking what the catch is. Still not a
# landing page: the catch is stated first because burying it is the thing that
# would actually cost trust.
FULL_MESSAGE = """{opening}

We're funding 5 people for 4 months, about $1,400 each in tooling: Claude Max 20x, ChatGPT Pro, and $200 to spend on whatever else. It's a trial, so it's small on purpose.

The catch, stated up front: we ask a good-faith 2% pledge on revenue and grants the funded work actually earns. Capped at $14,000, expires after 36 months, pro-rated if you leave early. No equity, no IP, no exclusivity, and you can withdraw at any point.

We're looking for agent infrastructure, protocol work and on-chain tooling. You don't need to have touched Celo, though we'd want a credible plan for it if you got a place.

Everything is public, including the rubric you'd be scored against and the code that does the scoring: github.com/prezenti/talent-engine. You can run it yourself and see your own number before you decide.

sponsorships.prezenti.xyz"""

SHORT_MESSAGE = """{opening}

{body}

{closing}"""


def variant(handle: str, count: int) -> int:
    """Which opening this person gets. Stable, so a rebuild does not reshuffle.

    Chosen by the handle rather than at random because these people know each
    other: several work on the same repositories, and two of them comparing
    notes should find two different messages, not the same one twice.
    """
    return sum(handle.encode()) % max(1, count)


def short_repo(repo: str, handle: str) -> str:
    """`owner/name` is how a machine refers to a repository. Their own repo is
    just its name, and saying "JSONbored/metagraphed" to JSONbored is the sound
    of something reading a database field aloud."""
    if "/" in repo:
        owner, _, name = repo.partition("/")
        if owner.lower() == handle.lower():
            return name
    return repo


def render(template: str, *, handle: str, name: str, hook: str = "",
           repo: str = "") -> str:
    """Fill a message for one person, or return "" if there is nothing true to say.

    Nothing to say means no repository and no channel: the opening line is the
    programme's claim that it found this person by reading public work, and a
    message that cannot make that claim should not be sent.
    """
    subject = short_repo(repo, handle) if repo else hook
    if not subject:
        return ""
    who = (name or "").strip() or handle
    # A full name reads as a mail merge; a first name reads as a person typing.
    who = who.split()[0] if " " in who else who
    opening = OPENINGS[variant(handle, len(OPENINGS))].format(name=who, repo=subject)
    return template.format(
        opening=opening,
        body=BODIES[variant(handle + "b", len(BODIES))],
        closing=CLOSINGS[variant(handle[::-1], len(CLOSINGS))],
        name=who,
        repo=subject,
    )
