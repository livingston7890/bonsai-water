#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
mkdir -p "$ROOT"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

upsert_env() {
  local name="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  awk -v name="$name" -v value="$value" '
    BEGIN{done=0}
    $0 ~ "^" name "=" { if(!done){ print name "=" value; done=1 } next }
    { print }
    END{ if(!done){ print name "=" value } }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

read_existing() {
  local name="$1"
  awk -F= -v name="$name" '$1==name {print substr($0, length(name)+2); exit}' "$ENV_FILE" 2>/dev/null || true
}

prompt_visible() {
  local name="$1"
  local prompt="$2"
  local default="${3:-}"
  local value
  if [[ -n "$default" ]]; then
    printf '%s [%s]: ' "$prompt" "$default"
  else
    printf '%s: ' "$prompt"
  fi
  IFS= read -r value
  if [[ -z "$value" && -n "$default" ]]; then value="$default"; fi
  if [[ -n "$value" ]]; then upsert_env "$name" "$value"; fi
}

prompt_secret() {
  local name="$1"
  local existing
  existing="$(read_existing "$name")"
  if [[ -n "$existing" ]]; then
    printf '%s already present (length %s). Press Enter to keep it, or paste replacement. Input is hidden.\n' "$name" "${#existing}"
  else
    printf 'Paste %s, then press Enter. Input is hidden.\n' "$name"
  fi
  printf '%s: ' "$name"
  stty -echo
  local value
  IFS= read -r value
  stty echo
  printf '\n'
  if [[ -n "$value" ]]; then upsert_env "$name" "$value"; fi
}

prompt_secret "BONSAI_TELEGRAM_BOT_TOKEN"
prompt_visible "BONSAI_HUB_BASE_URL" "Bonsai hub base URL" "$(read_existing BONSAI_HUB_BASE_URL || true)"
if [[ -z "$(read_existing BONSAI_HUB_BASE_URL || true)" ]]; then upsert_env "BONSAI_HUB_BASE_URL" "http://10.0.0.38:5000"; fi
prompt_visible "BONSAI_PI_SSH_TARGET" "Pi SSH target for exact 'reboot pi confirm' command, e.g. madmaestro@10.0.0.38" "$(read_existing BONSAI_PI_SSH_TARGET || true)"

TOKEN="$(read_existing BONSAI_TELEGRAM_BOT_TOKEN || true)"
ALLOWED="$(read_existing BONSAI_TELEGRAM_ALLOWED_USER_ID || true)"
if [[ -z "$ALLOWED" && -n "$TOKEN" ]]; then
  printf 'Trying to auto-detect BONSAI_TELEGRAM_ALLOWED_USER_ID from latest Telegram update...\n'
  DETECTED="$(python3 - "$TOKEN" <<'PY'
import json, sys, urllib.request
url=f"https://api.telegram.org/bot{sys.argv[1]}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        data=json.load(r)
    updates=data.get('result') or []
    for upd in reversed(updates):
        src=(upd.get('message') or upd.get('edited_message') or upd.get('callback_query') or {}).get('from') or {}
        if src.get('id') is not None:
            print(src['id']); break
except Exception:
    pass
PY
)"
  if [[ -n "$DETECTED" ]]; then
    upsert_env "BONSAI_TELEGRAM_ALLOWED_USER_ID" "$DETECTED"
    printf 'Saved BONSAI_TELEGRAM_ALLOWED_USER_ID from getUpdates.\n'
  else
    printf 'No Telegram user id found yet. Send your new bot any message, then rerun this script.\n'
  fi
else
  prompt_visible "BONSAI_TELEGRAM_ALLOWED_USER_ID" "Allowed Telegram numeric user id" "$ALLOWED"
fi

printf '\nSaved config to %s\n' "$ENV_FILE"
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import stat, sys
p=Path(sys.argv[1])
print('mode', oct(stat.S_IMODE(p.stat().st_mode)))
for key in ['BONSAI_TELEGRAM_BOT_TOKEN','BONSAI_TELEGRAM_ALLOWED_USER_ID','BONSAI_HUB_BASE_URL','BONSAI_PI_SSH_TARGET']:
    val=''
    for line in p.read_text(errors='replace').splitlines():
        if line.startswith(key+'='):
            val=line.split('=',1)[1]
            break
    print(f'{key}: present={bool(val)} length={len(val)}')
PY
