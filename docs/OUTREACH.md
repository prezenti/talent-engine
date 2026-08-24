# Contacting scouted candidates

The scout produces handles; recon finds accounts; this is what gets said. It is
the only part of the pipeline a person does by hand, deliberately.

## The rule this operates under

From the programme terms, "Your data":

> **People found by scouting** are assessed only on public code activity that
> GitHub already publishes, and are contacted only **to invite an application**.

That is binding and it is also the strongest thing about the message. It means
the message may not become a newsletter, a product pitch, a partnership ask or
a follow-up sequence. One invitation, honestly sourced. If they do not reply,
that is the answer.

Two further constraints follow from the terms and are enforced by the tooling
rather than by memory:

- **Nobody who has already applied is messaged.** `tools/outreach.py` excludes
  them by join against `submissions`.
- **Nobody is messaged twice.** `outreach_hooks.sent_at` survives every rebuild
  of the list, because the hook is derived data and the fact that a human
  already wrote to somebody is not.

Sending happens two ways, and both draw from the same queue and write the same
column, so a person cannot be reached twice by two different routes.

- **The send console** — a person copies the message and presses a button.
- **`tools/x_dm.py`** — sends through X under a grant the account owner approved
  in a browser. X does not permit app-only authentication for Direct Messages,
  so every message is still attributed to a person rather than to a bot with its
  own identity. That is the correct shape for this, not a limitation worked
  around.

## Building the list

```bash
cd ~/talent-engine
set -a; . ~/talent-engine-runtime/intake.env; set +a
python3 tools/recon.py    --program prezenti-sponsorship-trial --limit 950 --budget 4200
python3 tools/outreach.py --program prezenti-sponsorship-trial
```

`outreach.csv` lands in `~/talent-engine-runtime/`, best-scored first, then most
recently discovered. After sending:

```bash
python3 tools/outreach.py --program prezenti-sponsorship-trial --mark handle1,handle2
```

## The send console

`/send/<token>.html` is the same queue rendered one person at a time: the
message already written, a copy button, a link to their profile, and a **Mark
sent** button that writes `sent_at` and drops them from the queue for good.

It exists because marking is the step that gets skipped when a batch runs long,
and skipping it is how somebody gets written to twice. Making the bookkeeping a
button rather than a command afterwards is the whole design.

- It renders **live**, not from a cron file. A board of applicants can be
  fifteen minutes stale; a send queue that is fifteen minutes stale hands you
  somebody you already wrote to.
- It shows the next 25 and says something once you are past 30 in a day.
- The mark endpoint answers `{"ok": true, "changed": false}` when the handle was
  already recorded, and the button then reads *already recorded* rather than
  *sent*. A control that reports success on a no-op teaches you to trust it when
  it has done nothing.
- It is the only surface here that writes, so it has its own token and can be
  revoked without touching the read feeds.

**Two buttons, and the second one matters as much as the first.** *Mark sent* is
a message that went. *DMs closed* is X refusing to carry one, which is a settled
fact about that person: it is written to `x_delivery` and they leave the queue
permanently. Without it every batch re-offers the same shut doors and you
re-check them by hand for as long as you keep going.

Closed is deliberately **not** recorded as sent. Nobody was written to, and the
column that says who was written to is what a conversion measure will read.

Once ten people have been through, the header reports the measured share of
accounts X will actually carry a message to. That number did not exist until
somebody worked a batch by hand, and it is the real size of this channel.

The same two outcomes from the terminal:

```bash
python3 tools/outreach.py --program prezenti-sponsorship-trial --mark a,b
python3 tools/outreach.py --program prezenti-sponsorship-trial --closed c,d
python3 tools/outreach.py --program prezenti-sponsorship-trial --unmark e
```

## The one line that must be true

Every row carries a **Why we found you** built from two facts already on
record: the repository (either the one the scorer cited, or their most recent
described original repo) and the scout channel that surfaced them.

| Channel | What it means | What to say |
|---|---|---|
| `contributors` | merged PR into a seed repo | a merged pull request into one of the repositories we watch |
| `originators` | their own repo matched the taxonomy | your own repositories, in the space we were searching |
| `adjacent` | contributor to a repo `originators` surfaced | your contributions to a small project in that space |

Where the channel is known but no repository is, the hook still says how they
arrived and **Basis** reads `scout channel only` — true, but naming no work,
which is most of what makes the message worth sending. Where neither is
available the column is empty and the drafted message is empty with it. **Send
that row without a hook or do not send it — do not invent one.** The entire argument of the programme
is that its claims can be checked; an opening line that cannot be is worse than
no message.

## The message

Lives in `talent_engine/modes/outreach.py`, version-controlled rather than in
somebody's notes, so a change to the programme's numbers changes what strangers
are told.

**It is written to not read as generated**, and the tell is shape rather than
vocabulary: a greeting, a value proposition, benefits enumerated in threes and a
call to action is recognisable as a template from the first line, after which
everything else is read as one. Someone who genuinely found your repository
writes three sentences and stops. So: contractions, no em dashes, no lists of
three, no sign-off flourish, one concrete thing and one link. There is a test
for the em dashes.

Four openings and two bodies, chosen by a stable hash of the handle rather than
at random. These people know each other — several contribute to the same
repositories — so two of them comparing notes should find two different
messages. The variants differ in **shape**, not in synonyms: rotating "came
across" / "stumbled on" / "found" yields three hundred messages that are
obviously one message, which is the problem rather than a solution to it.

Their own repository is named the way they name it: `metagraphed`, not
`JSONbored/metagraphed`, which said to JSONbored is a database field read aloud.
Somebody else's repository keeps its owner, because there the owner is the
point. First names only; a full name reads as a merge field.

The long version states the 2% ask **before** the benefits. Burying what we want
is the thing that would actually cost trust.

### The draft is not the message

The draft claims exactly one thing: this repository is what surfaced you. That
is true and checkable. It is **not** evidence that anybody read anything, and no
template can be.

So the console's message is a textarea, with the repository's own description
directly above it. One sentence of your own reacting to that description is
worth more than everything the draft says, and it takes about ten seconds. That
is the difference between a mailout and a message, and it is the reason this
part is done by a person.

## Things that would make this dishonest

- Claiming to have read something you have not. The hook names a repository;
  that is a claim that it surfaced, not that it was reviewed.
- Implying selection. Nobody is shortlisted by being messaged, and the message
  must not suggest otherwise — the scout channel is a discovery mechanism, not
  an assessment.
- Sending to somebody whose only listed account is an organisation or a bot.
  Recon takes X handles from the profile field, which sometimes holds a company
  account; the **Basis** column says where each one came from.
- Volume. 300 identical messages in an hour is a bulk send whatever the content
  says, and X will treat it as one.


## The X channel

Two things, in this order.

### The list first

```bash
python3 tools/x_list.py --program prezenti-sponsorship-trial \
    --create --name "Builders we found"
```

Private by default, and `--public` is deliberately awkward: adding somebody to
a public list **notifies them**, and several hundred strangers discovering they
are on a list called "candidates" is a worse first contact than the message
would have been.

Worth doing before any message goes out. It is a feed of what these people are
actually shipping, and replying to somebody's work before writing to them does
more for a reply rate than any wording of the message will.

`POST /2/lists/:id/members` allows 300 per user per fifteen minutes, so a few
hundred people is two runs. Numeric ids are stored rather than handles, because
a handle is a display name its owner can change under you.

### Then the messages

```bash
python3 tools/x_dm.py --program prezenti-sponsorship-trial              # dry run
python3 tools/x_dm.py --program prezenti-sponsorship-trial --send --limit 25
```

The dry run is the default and needs no credentials at all.

- **Paced.** 45–150 seconds between sends, jittered, not configurable to zero.
- **Capped.** 25 a run by default.
- **Stops on trouble.** Three transport errors in a row ends the batch and
  leaves the rest of the queue untouched.
- **Records refusals.** X will not say in advance whether somebody accepts
  messages from people they do not follow; you find out by trying. A refusal is
  written to `x_delivery` and that person is never queued again — discovered
  once rather than once per attempt. That number is worth having on its own: it
  is the real size of this channel.

### Authorising it

Once, and only the account owner can do it:

```bash
python3 tools/x_auth.py --start                    # prints a URL; approve it
python3 tools/x_auth.py --finish '<the address bar you land on>'
```

The redirect goes to a loopback address that is listening to nothing, so the
browser will say it cannot connect. That is expected — the address bar holds the
grant. A callback served on the public tunnel would mean a permanently public
endpoint accepting authorisation codes for the sake of one redirect that happens
once; a dead loopback leaks nothing and costs one paste.

Run `--finish` yourself rather than pasting the code into a chat. It is
single-use and short-lived, but short-lived is not harmless. `X_CLIENT_ID` (and
`X_CLIENT_SECRET` for a confidential app) live in `intake.env`; the grant lands
at `~/talent-engine-runtime/x-tokens.json`, written 0600 from the first byte.

X rotates the refresh token on every use, so the replacement is saved before the
next call goes out. A refresh whose result was not persisted leaves the stored
token already spent and the grant dead with no way back but the browser step.
