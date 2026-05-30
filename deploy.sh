#!/usr/bin/env bash
set -euo pipefail

COMMIT_PATTERN='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-zA-Z0-9._-]+\))?!?: .{1,100}$'

die() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || die "git is not installed."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository. Run: git init"

if ! git remote get-url origin >/dev/null 2>&1; then
  die "Remote 'origin' is not configured. Run: git remote add origin <git@github.com:USER/REPO.git>"
fi

current_branch="$(git branch --show-current)"
if [ -z "$current_branch" ]; then
  die "Detached HEAD. Checkout a branch before deploying."
fi

default_branch="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
target_branch="${current_branch:-${default_branch:-main}}"

if ! git diff --quiet --exit-code || ! git diff --cached --quiet --exit-code || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "Detected changes:"
  git status --short
else
  echo "No changes to commit."
  git push -u origin "$target_branch"
  exit 0
fi

echo
echo "Conventional Commit examples:"
echo "  feat(scanner): add intraday filter"
echo "  fix(trading): handle missing order id"
echo "  docs: update setup guide"
echo

read -r -p "Commit message: " commit_msg
if [[ ! "$commit_msg" =~ $COMMIT_PATTERN ]]; then
  die "Invalid commit message. Use: type(optional-scope): summary"
fi

git add -A

if git diff --cached --quiet --exit-code; then
  echo "No staged changes after git add."
  exit 0
fi

git commit -m "$commit_msg"
created_commit="$(git rev-parse HEAD)"

rollback_commit() {
  echo "Push failed. Rolling back local commit $created_commit ..."
  if git rev-parse --verify --quiet HEAD~1 >/dev/null; then
    git reset --soft HEAD~1
  else
    git update-ref -d HEAD
  fi
  echo "Rollback complete. Your changes are still staged locally."
}

trap 'rollback_commit' ERR
git push -u origin "$target_branch"
trap - ERR

echo "Pushed $created_commit to origin/$target_branch"
