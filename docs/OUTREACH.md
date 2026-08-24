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

Nothing is sent by automation. There is no X credential on this machine and
there should not be one: a cold message arriving under a bot is precisely what
the paragraph above promises these people is not happening.

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

Substitute `{name}` (fall back to their handle) and `{hook}` from the CSV.

### Full version

> Hi {name} — I'm zoz, from Prezenti. You came up through {hook}. Nothing here
> was scraped beyond what GitHub already publishes, and I'm writing for one
> reason: to invite you to apply.
>
> We back builders on evidence of what they've actually shipped. This is a small
> trial — 5 places, 4 months, $1,400 each in tooling: Claude Max 20x, ChatGPT
> Pro, and a $200 flexible allowance. We're looking for people building agent
> infrastructure, protocol engineering and on-chain tooling, deliberately
> including people who have never touched Celo.
>
> No equity, and you keep all IP. No exclusivity, withdraw at any time. In
> return we ask a good-faith 2% pledge on revenue and grants the sponsored work
> actually earns — capped at $14,000, expiring after 36 months, pro-rated by the
> months you take.
>
> The terms, the rubric you'd be scored against and the code that does the
> scoring are all public: github.com/prezenti/talent-engine. You can run it and
> reproduce your own number before deciding whether we're worth your time.
>
> Apply: sponsorships.prezenti.xyz
>
> Everyone who applies gets their score, the evidence behind it, and feedback —
> selected or not.
>
> — zoz

### Short version

For a first touch into a request inbox, where length reads as a sales sequence.

> Hi {name} — zoz from Prezenti. You came up through {hook}.
>
> We fund builders on shipped evidence rather than network: 5 places, 4 months,
> $1,400 each in tooling, for people building agent infrastructure, protocol and
> on-chain work. No equity, you keep all IP.
>
> Terms, rubric and the scoring code are public so you can check us before
> replying: sponsorships.prezenti.xyz
>
> Genuinely just an invitation to apply.

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
