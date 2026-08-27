#!/usr/bin/env bash
# Refresh the applicant board, and shout if anyone is stuck in the pipeline.
#
# 2026-08-19, launch day: two of five applications failed on a GitHub 409 and
# sat in `queued` forever. Nothing external retries a Tally submission, no email
# is sent for a submission that never scores, and the sheet shows the row
# regardless -- so a stranded applicant is invisible in every surface a human
# looks at. It reads exactly like nobody applied. One of the two was the highest
# scorer of the round.
#
# The alert is therefore on a count that must be ZERO, not on whether the intake
# service is up. Same principle as the staleness monitor: watch the outcome, not
# the process.
set -uo pipefail

DB=$HOME/talent-engine-runtime/talent_engine.db
ENV_FILE=$HOME/.claude/channels/telegram/.env
STATE=$HOME/.local/state/applicant-watch.state
GRACE_MIN=20            # a submission still scoring is normal; a stuck one is not

cd "$HOME/talent-engine" || exit 0
# Two copies, on purpose. The full one stays on this box and carries contact
# details; the served one is what stewards open, and contacts are absent from
# it rather than hidden in it -- they are already in the Tally tab of the
# stewards' sheet, and a URL anyone holding can open is no place for a second
# copy of eleven people's email addresses.
python3 tools/crm_report.py --contacts \
  --out "$HOME/talent-engine-runtime/crm-full.html" \
  --csv "$HOME/talent-engine-runtime/applicants.csv" >/dev/null 2>&1
python3 tools/crm_report.py --standalone \
  --out "$HOME/talent-engine-runtime/board.html.tmp" >/dev/null 2>&1 \
  && mv "$HOME/talent-engine-runtime/board.html.tmp" \
        "$HOME/talent-engine-runtime/board.html"

# Two more copies, both free, both for the same reason: a link Chad opens must
# be live. The claude.ai artifact was a one-off publish that froze the moment it
# was made, so it showed 5 applicants for a week while the board showed 27.
#   - ops-state/public/applicants.html is served on the tailnet beside the ops
#     console, so it is never more than 15 minutes old and costs nothing.
#   - board.artifact.html is body-only and ready to publish, so republishing the
#     artifact is one call with no conversion step to get wrong.
mkdir -p "$HOME/ops-state/public"
cp -f "$HOME/talent-engine-runtime/board.html" \
      "$HOME/ops-state/public/applicants.html" 2>/dev/null || true
python3 "$HOME/talent-engine/tools/board_to_artifact.py" >/dev/null 2>&1 || true

stuck=$(sqlite3 "$DB" "
  select count(*) from submissions
  where status <> 'scored'
    and received_at < datetime('now', '-$GRACE_MIN minutes');" 2>/dev/null)
[ -n "${stuck:-}" ] || exit 0

prev=$(cat "$STATE" 2>/dev/null || echo 0)
echo "$stuck" > "$STATE"

# Only on a change, so a stall that is already known does not nag nightly.
[ "$stuck" = "$prev" ] && exit 0
[ "$stuck" = "0" ] && exit 0

TOK=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
CHAT=$(grep -m1 '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
detail=$(sqlite3 "$DB" "
  select group_concat(handle || ' (' || status || ')', ', ')
  from submissions
  where status <> 'scored'
    and received_at < datetime('now', '-$GRACE_MIN minutes');" 2>/dev/null)
curl -s --max-time 20 "https://api.telegram.org/bot${TOK}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${stuck} application(s) stuck in the pipeline and not scored: ${detail}. They got no email and appear nowhere except the board. Needs a look." \
  -o /dev/null
