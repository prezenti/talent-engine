#!/usr/bin/env bash
# Put the running system back on a box from this repository alone.
#
# Everything the pipeline needs outside the Python package used to live only on
# one machine: four shell scripts in ~/scripts, two systemd units in /etc, a
# tunnel ingress file, and a crontab nobody had written down. The code was in
# git and the system was not, so a rebuild depended on the box that would have
# been lost.
#
# This is idempotent and safe to re-run. It installs nothing secret: the
# credentials stay in ~/talent-engine-runtime/intake.env, which it will tell you
# about but never create.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${TALENT_ENGINE_RUNTIME:-$HOME/talent-engine-runtime}"
SCRIPTS="$HOME/scripts"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }

say "repo:    $REPO"
say "runtime: $RUNTIME"
say ""

# 1. The cron scripts, as symlinks back into the repo. Cron keeps its existing
#    paths and an edit to the repo copy is live immediately -- the alternative,
#    copying, gives you two files that drift and one of them is the one that
#    actually runs.
say "cron scripts -> $SCRIPTS"
run mkdir -p "$SCRIPTS"
for f in daily-scout.sh applicant-watch.sh backup-talent-engine.sh; do
  if [ -L "$SCRIPTS/$f" ] && [ "$(readlink -f "$SCRIPTS/$f")" = "$REPO/deploy/$f" ]; then
    say "  ok   $f"
  else
    [ -e "$SCRIPTS/$f" ] && [ ! -L "$SCRIPTS/$f" ] && run mv "$SCRIPTS/$f" "$SCRIPTS/$f.replaced-by-repo"
    run ln -sfn "$REPO/deploy/$f" "$SCRIPTS/$f"
    say "  link $f"
  fi
done

# 2. Runtime directory. The database and the caches live outside the repo
#    because they are state, not source, and 0700 because one of them holds
#    applicant contact details.
say ""
say "runtime directory"
run mkdir -p "$RUNTIME"
run chmod 700 "$RUNTIME"

# 3. Credentials. Never generated here, never guessed: an install script that
#    invents a secret leaves you unable to tell a fresh install from a silently
#    reset one.
say ""
if [ -f "$RUNTIME/intake.env" ]; then
  missing=""
  for key in TALLY_SIGNING_SECRET GITHUB_TOKEN SCORES_FEED_TOKEN SCOUT_FEED_TOKEN BOARD_TOKEN OUTREACH_FEED_TOKEN; do
    grep -q "^$key=." "$RUNTIME/intake.env" || missing="$missing $key"
  done
  if [ -n "$missing" ]; then
    say "intake.env is missing values for:$missing"
    say "  (see deploy/intake.env.example for what each one is for)"
  else
    say "intake.env: present, all required keys set"
  fi
else
  say "intake.env: MISSING -- copy deploy/intake.env.example to $RUNTIME/intake.env,"
  say "  fill it in, and chmod 600. Nothing runs without it."
fi

# 4. systemd units. Left to the operator to install, deliberately: this needs
#    root, and a script that sudo-writes into /etc as a side effect of "install"
#    is not something to run without reading it first.
say ""
say "systemd units (need root; run these yourself):"
for unit in talent-engine-intake cloudflared-sponsorships; do
  if [ -f "/etc/systemd/system/$unit.service" ] \
     && diff -q "$REPO/deploy/systemd/$unit.service" "/etc/systemd/system/$unit.service" >/dev/null 2>&1; then
    say "  ok       $unit.service matches the repo"
  else
    say "  sudo cp $REPO/deploy/systemd/$unit.service /etc/systemd/system/"
  fi
done
say "  sudo systemctl daemon-reload && sudo systemctl enable --now talent-engine-intake cloudflared-sponsorships"

# 5. Tunnel ingress. The template carries a placeholder for the tunnel this host
#    owns; the credentials JSON never leaves the box it was created on.
say ""
say "cloudflare tunnel ingress:"
say "  TUNNEL_ID=<uuid> envsubst < $REPO/deploy/cloudflared/sponsorships.yml \\"
say "    | sudo tee /etc/cloudflared/sponsorships.yml"
say "  sudo cloudflared tunnel --config /etc/cloudflared/sponsorships.yml ingress validate"
say "  # cloudflared does NOT reload its ingress on its own:"
say "  sudo systemctl restart cloudflared-sponsorships"

# 6. Cron. Printed rather than installed, so that adopting this never silently
#    rewrites a crontab that holds unrelated jobs.
say ""
say "crontab lines (add with 'crontab -e' if absent):"
sed 's|^|  |' "$REPO/deploy/cron.txt"

say ""
say "check it worked:"
say "  systemctl is-active talent-engine-intake cloudflared-sponsorships"
say "  curl -s localhost:8787/healthz"
say "  bash $SCRIPTS/applicant-watch.sh && ls -la $RUNTIME/board.html"
