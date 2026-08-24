#!/usr/bin/env bash
# Daily sourcing run for the sponsorship pipeline.
#
# Cron runs with cwd=$HOME and a bare environment, so everything here is
# absolute and the credentials are sourced explicitly — .bashrc returns early
# for non-interactive shells and would not export them.
set -euo pipefail

ENV_FILE=/home/ubuntu/talent-engine-runtime/intake.env
REPO=/home/ubuntu/talent-engine
LOG=/home/ubuntu/talent-engine-runtime/daily-scout.log

# What this deployment is: program, seeds and per-seed caps. Kept in one file
# so that a second deployment copies a config rather than forking this script.
DEPLOYMENT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/deployment.env"
[ -f "$DEPLOYMENT" ] || { echo "$(date -Is) no deployment config at $DEPLOYMENT" >>"$LOG"; exit 1; }
set -a
# shellcheck disable=SC1090
. "$DEPLOYMENT"
set +a

[ -f "$ENV_FILE" ] || { echo "$(date -Is) no env file at $ENV_FILE" >>"$LOG"; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

cd "$REPO"
{
  echo "--- $(date -Is) ---"
  /usr/bin/python3 "$REPO/tools/daily_scout.py" \
      --program "$PROGRAM" \
      --seeds "$SEEDS" \
      --caps "$CAPS" \
      --budget 900 \
      --score-top 12 \
      --limit 8 2>&1
} >>"$LOG" 2>&1

# Then find out how to reach the people it just found. A handle is not a way to
# contact anybody, and a promising name with no channel beside it is a lead that
# dies in the digest. Runs after the scout so tonight's names are looked up
# tonight; the limit also chews through the backlog a little each night.
{
  echo "--- recon $(date -Is) ---"
  /usr/bin/python3 "$REPO/tools/recon.py" \
      --program "$PROGRAM" \
      --limit 40 \
      --budget 200 2>&1
} >>"$LOG" 2>&1

# And work out what there is to say to whoever recon just made reachable. This
# only builds the line; nothing is sent. Cheap because a hook is computed once
# and kept -- the scorer's own cited repository costs no request at all, and a
# fresh lookup is one request for someone who has never had one.
{
  echo "--- outreach $(date -Is) ---"
  /usr/bin/python3 "$REPO/tools/outreach.py" \
      --program "$PROGRAM" \
      --budget 120 2>&1
} >>"$LOG" 2>&1

# Keep the log from growing without bound; a scout run is a few lines a day.
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
