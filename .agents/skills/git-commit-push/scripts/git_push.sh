#!/bin/bash
set -e

MSG="$1"
if [ -z "$MSG" ]; then
  MSG="chore: update project files"
fi

BRANCH=$(git branch --show-current)
if [ -z "$BRANCH" ]; then
  BRANCH="main"
fi

git add -A
git commit -m "$MSG" || echo "Nothing new to commit."
git push origin "$BRANCH"
