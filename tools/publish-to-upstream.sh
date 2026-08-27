#!/usr/bin/env bash
#
# Send engine work from this deployment back to P-U-C/talent-engine.
#
# sync-from-upstream.sh carries changes one way: engine -> deployment. There was
# no path the other way, so for weeks every improvement landed here and stopped:
# the 409-empty-repository fix, the stored applicant reference, the three read
# surfaces, the scout's recon pass, the deploy directory. Thirty-two commits, all
# of them engine work, none of them in the engine. The repository described as
# reusable had quietly become an ancestor of the real thing.
#
# The hazard is the same one as the other direction, mirrored. Some files belong
# to this deployment and must not travel: README.md is what applicants read
# before applying, docs/ENGINE.md exists only here, and deploy/deployment.env is
# this deployment's program, hostname and seed list. Merging without restoring
# them replaces upstream's engine documentation with a sponsorship landing page.
#
# The list was three files and should have been six. .gitattributes and both of
# these scripts are just as much this deployment's -- .gitattributes opens "This
# fork is a deployment of P-U-C/talent-engine" and declares merge=ours for two
# files upstream does not have, and sync-from-upstream.sh hard-codes this
# deployment's README marker and names upstream as the thing to merge from.
# They travelled in PR #2, where CI caught the second one by working exactly as
# designed: the workflow treats an executable sync-from-upstream.sh as "I am a
# deployment fork" and ran its --check against the engine, which is not one.
#
#   tools/publish-to-upstream.sh              # prepare the branch, run the tests
#   tools/publish-to-upstream.sh --push       # ...and push it, then print the PR command
#
# It opens nothing by itself. What lands upstream is a branch and a pull request
# for a human to read, because a public repository that other programmes may be
# running is not somewhere to fast-forward main from a script.
set -euo pipefail

UPSTREAM_URL="https://github.com/P-U-C/talent-engine.git"
UPSTREAM_REMOTE="upstream"
DEPLOYMENT_FILES=(README.md docs/ENGINE.md deploy/deployment.env \
                  .gitattributes tools/sync-from-upstream.sh \
                  tools/publish-to-upstream.sh)
# Of those, the only one upstream has a legitimate version of. The others exist
# here and must not travel at all -- deciding that by asking whether upstream
# already has a copy trusts the very state a previous leak corrupted, and would
# have "restored" this deployment's own .gitattributes and sync script back onto
# the branch that had just carried them upstream by mistake.
UPSTREAM_OWNS=(README.md)
BRANCH="from-deployment-$(git rev-parse --short HEAD)"
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

cd "$(dirname "$0")/.."

git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 || git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
git fetch -q "$UPSTREAM_REMOTE" main

ahead=$(git rev-list --count "$UPSTREAM_REMOTE/main..HEAD")
behind=$(git rev-list --count "HEAD..$UPSTREAM_REMOTE/main")
echo "this deployment is $ahead ahead, $behind behind $UPSTREAM_REMOTE/main"
if [ "$ahead" = "0" ]; then
  echo "nothing to send."
  exit 0
fi
if [ "$behind" != "0" ]; then
  echo "run tools/sync-from-upstream.sh first: upstream has work this deployment lacks." >&2
  exit 1
fi

start=$(git rev-parse --abbrev-ref HEAD)
cleanup() { git checkout -q "$start"; }
trap cleanup EXIT

git checkout -q -B "$BRANCH" "$UPSTREAM_REMOTE/main"
git merge -q --no-edit -X theirs "$start" || {
  echo "merge conflicts; resolve on branch $BRANCH by hand." >&2
  exit 1
}

# Give upstream its own copies back. This is the whole point of the script.
for f in "${DEPLOYMENT_FILES[@]}"; do
  if printf '%s\n' "${UPSTREAM_OWNS[@]}" | grep -qxF -- "$f" \
     && git cat-file -e "$UPSTREAM_REMOTE/main:$f" 2>/dev/null; then
    git checkout -q "$UPSTREAM_REMOTE/main" -- "$f"
    echo "  kept upstream's $f"
  else
    git rm -q --cached "$f" >/dev/null 2>&1 || true
    rm -f "$f"
    echo "  withheld $f (belongs to this deployment only)"
  fi
done
git diff --cached --quiet || git commit -q -m "keep upstream's own README, engine docs and deployment config"

if ! python3 -m pytest -q >/dev/null 2>&1; then
  echo "tests fail on the merged branch; not pushing." >&2
  exit 1
fi
echo "tests pass on $BRANCH"

if [ "$PUSH" = 1 ]; then
  git push -q "$UPSTREAM_REMOTE" "$BRANCH"
  echo "pushed $BRANCH to $UPSTREAM_URL"
  echo
  echo "open the pull request with:"
  echo "  gh pr create --repo P-U-C/talent-engine --base main --head $BRANCH \\"
  echo "    --title 'Engine work from the Prezenti deployment'"
else
  echo "prepared $BRANCH locally. Re-run with --push to send it."
fi
