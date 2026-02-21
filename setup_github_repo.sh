#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_NAME="bonsai-water"
DEFAULT_BRANCH="main"

echo "[SETUP] Project: ${APP_DIR}"
cd "${APP_DIR}"

if ! command -v gh >/dev/null 2>&1; then
  echo "[SETUP] GitHub CLI not found. Install with: brew install gh"
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[SETUP] Not a git repo. Initializing..."
  git init -b "${DEFAULT_BRANCH}"
  git config user.name "${USER:-bonsai}"
  git config user.email "${USER:-bonsai}@local"
fi

if [ -z "$(git rev-parse --verify HEAD 2>/dev/null || true)" ]; then
  echo "[SETUP] Creating initial commit..."
  git add -A
  git commit -m "Initial Pi Control Hub v2"
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "[SETUP] GitHub login required..."
  gh auth login -h github.com -p https -w
fi

read -r -p "Repo name [${DEFAULT_REPO_NAME}]: " REPO_NAME
REPO_NAME="${REPO_NAME:-$DEFAULT_REPO_NAME}"

read -r -p "Visibility (public/private) [public]: " VIS
VIS="${VIS:-public}"
if [ "${VIS}" != "public" ] && [ "${VIS}" != "private" ]; then
  echo "[SETUP] Invalid visibility. Use public or private."
  exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "[SETUP] Remote origin already exists:"
  git remote -v
else
  echo "[SETUP] Creating GitHub repo and pushing..."
  gh repo create "${REPO_NAME}" "--${VIS}" --source=. --remote=origin --push
fi

REPO_URL="$(gh repo view --json url --jq .url)"
BRANCH_NAME="$(git branch --show-current || echo "${DEFAULT_BRANCH}")"

echo
echo "[SETUP] GitHub repo ready."
echo "[SETUP] Repo URL to paste in Pi dashboard Update Modules prompt:"
echo "${REPO_URL}.git"
echo "[SETUP] Branch:"
echo "${BRANCH_NAME}"
echo
echo "[NEXT] In Pi Control Hub -> Update Modules:"
echo "       Repo URL: ${REPO_URL}.git"
echo "       Branch:   ${BRANCH_NAME}"

