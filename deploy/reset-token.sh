#!/usr/bin/env bash
# Reset the dashboard access token (data/token) to a value you choose.
# The supervisor caches the token at startup, so the service is restarted
# after the write; capture's chromium picks up the new token on its relaunch.
# Note: the token you pass ends up in your shell history.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"

TOKEN_FILE="$REPO/data/token"

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 <new-token>" >&2
  echo "  URL-safe characters only: letters, digits, dot, dash, underscore, tilde" >&2
  exit 2
fi
NEW="$1"

if [[ ! "$NEW" =~ ^[A-Za-z0-9._~-]+$ ]]; then
  echo "error: token must use URL-safe characters only (A-Za-z0-9 . _ ~ -)" >&2
  echo "  the token travels in ?token= query params, so spaces/&/% would break capture" >&2
  exit 2
fi

write_token() {
  printf '%s\n' "$NEW" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "token written to $TOKEN_FILE"
}

if [ -f /etc/systemd/system/thermallcd.service ]; then
  sudo -v   # authenticate before touching the token file, never half-apply
  write_token
  sudo systemctl restart thermallcd.service
  echo "thermallcd.service restarted — log in again with the new token:"
  echo "  http://localhost:8899  (token: $NEW)"
else
  write_token
  echo "note: thermallcd.service not installed; token file updated (run deploy/install.sh to start)"
fi
