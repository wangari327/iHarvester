#!/usr/bin/env bash
# Create one local-only human-admin recovery session via a Telegram QR login.

set -euo pipefail

session_name="${1:-}"
if [[ ! "$session_name" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
  echo "Usage: bash scripts/authorize_owner_qr.sh account-1" >&2
  exit 2
fi

read -r -p "Telegram API ID: " TELEGRAM_API_ID
read -r -s -p "Telegram API hash: " TELEGRAM_API_HASH
echo

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
session_dir="${IHARVESTER_RECOVERY_SESSION_DIR:-$HOME/iharvester-recovery/session}"
install -d -m 700 "$session_dir"

export TELEGRAM_API_ID TELEGRAM_API_HASH
trap 'unset TELEGRAM_API_ID TELEGRAM_API_HASH' EXIT

docker run --rm -it \
  --mount "type=bind,src=$repo_dir,dst=/workspace/repo,readonly" \
  --mount "type=bind,src=$session_dir,dst=/recovery/session" \
  -w /workspace/repo \
  -e TELEGRAM_API_ID \
  -e TELEGRAM_API_HASH \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  python:3.12-slim \
  sh -ec "python -m pip install --no-cache-dir -q -e . -r requirements-mtproto-recovery.txt && python scripts/login_mtproto_qr.py --session /recovery/session/$session_name"
