#!/usr/bin/env bash
#
# Pull engine changes from P-U-C/talent-engine into this deployment.
#
# The two repositories deliberately disagree about a handful of files. Upstream
# README.md describes the reusable engine; this one is what applicants read
# before they apply. docs/ENGINE.md exists only here, and deploy/deployment.env
# is what makes this deployment this one -- the program, the hostname and the
# repositories the scout crawls. A plain `git merge` silently resolves them in
# upstream's favour, which replaced the live application front page with
# engineering documentation twice in one review -- once in each direction.
# Encoded here so it cannot be forgotten again.
#
#   tools/sync-from-upstream.sh            # merge and verify
#   tools/sync-from-upstream.sh --check    # verify only, no merge
#
set -euo pipefail

UPSTREAM_URL="https://github.com/P-U-C/talent-engine.git"
UPSTREAM_REMOTE="upstream"
DEPLOYMENT_FILES=(README.md docs/ENGINE.md deploy/deployment.env \
                  .gitattributes tools/sync-from-upstream.sh \
                  tools/publish-to-upstream.sh)
DEPLOYMENT_MARKER="Prezenti AI Builder Sponsorships"

cd "$(dirname "$0")/.."

verify() {
  local failed=0
  if ! head -n 5 README.md | grep -qF "$DEPLOYMENT_MARKER"; then
    echo "FAIL: README.md is not the deployment's. Upstream's copy won a merge." >&2
    failed=1
  fi
  for f in "${DEPLOYMENT_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
      echo "FAIL: $f is missing. It belongs to this deployment." >&2
      failed=1
    fi
  done
  if (( failed )); then
    echo >&2
    echo "Recover with:  git checkout <last-good-fork-commit> -- ${DEPLOYMENT_FILES[*]}" >&2
    return 1
  fi
  echo "ok: deployment-owned files intact"
}

if [[ "${1:-}" == "--check" ]]; then
  verify
  exit $?
fi

# `merge=ours` in .gitattributes is inert unless the driver is defined, and the
# definition is per-clone rather than committed. Setting it every run is
# idempotent and means a fresh clone is never one merge away from the bug.
git config merge.ours.driver true

git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 \
  || git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"

git fetch "$UPSTREAM_REMOTE" main
git merge --no-edit "$UPSTREAM_REMOTE/main"

# Belt and braces: the driver is easy to lose, so check the outcome rather
# than trusting the mechanism.
verify

if command -v python3 >/dev/null 2>&1; then
  python3 -m pytest -q
  python3 tools/render_rubric_weights.py --check
fi

echo
echo "Synced. Review the diff before pushing:  git log --oneline @{u}..HEAD"
