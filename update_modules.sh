#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/madmaestro/bonsai-water}"
BUNDLE_TGZ="${APP_DIR}/update_bundle.tgz"
INCOMING_DIR="${APP_DIR}/_incoming"

log() {
  printf '[UPDATE] %s\n' "$*"
}

copy_tree() {
  src_dir="$1"
  dst_dir="$2"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.git' "${src_dir}/" "${dst_dir}/"
  else
    (cd "${src_dir}" && tar cf - .) | (cd "${dst_dir}" && tar xpf -)
  fi
}

dir_has_files() {
  test -n "$(find "$1" -mindepth 1 -print -quit 2>/dev/null || true)"
}

log "Starting module update pipeline"
cd "${APP_DIR}"

updated_from="none"

if [ -d "${APP_DIR}/.git" ]; then
  log "Git checkout detected; running git pull --ff-only"
  if git pull --ff-only >>/tmp/pi_hub_update.log 2>&1; then
    updated_from="git"
    log "Git update completed"
  else
    log "Git update failed; see /tmp/pi_hub_update.log"
  fi
fi

if [ "${updated_from}" = "none" ] && [ -f "${BUNDLE_TGZ}" ]; then
  log "Applying update bundle: ${BUNDLE_TGZ}"
  tmp_dir="$(mktemp -d /tmp/pi_hub_bundle.XXXXXX)"
  trap 'rm -rf "${tmp_dir}"' EXIT
  tar -xzf "${BUNDLE_TGZ}" -C "${tmp_dir}"
  copy_tree "${tmp_dir}" "${APP_DIR}"
  rm -f "${BUNDLE_TGZ}"
  updated_from="bundle"
  log "Bundle applied"
fi

if [ "${updated_from}" = "none" ] && [ -d "${INCOMING_DIR}" ] && dir_has_files "${INCOMING_DIR}"; then
  log "Applying staged files from ${INCOMING_DIR}"
  copy_tree "${INCOMING_DIR}" "${APP_DIR}"
  updated_from="incoming"
  log "Staged files applied"
fi

if [ "${updated_from}" = "none" ]; then
  log "No update source found; running restart-only cycle"
else
  log "Update source: ${updated_from}"
fi

log "Update pipeline complete"
exit 0
