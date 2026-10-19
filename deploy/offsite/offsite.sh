#!/bin/sh
# Encrypted off-server copies of the database backups, with restic (Stage 13).
#
# Runs as the opt-in "backup-offsite" service (deploy/compose.yaml, profile "offsite").
# Once a day at OFFSITE_AT (UTC) it sends the backups folder (the "backups" volume,
# mounted read-only at /backups) to the restic repository RESTIC_REPOSITORY, then applies
# retention. restic encrypts everything on this server (AES-256) with RESTIC_PASSWORD
# before it leaves: the storage provider never sees customer data. See
# docs/backup-and-restore.md, "Off-server copies".
#
#   offsite.sh            run forever (the service's command)
#   offsite.sh once       one copy now, then exit
#   offsite.sh check      health check: exit 0 healthy, 1 not
#
# Settings (deploy/environments/<env>/offsite.env): RESTIC_REPOSITORY, RESTIC_PASSWORD (or
# RESTIC_PASSWORD_FILE), the storage's credentials (e.g. AWS_ACCESS_KEY_ID and
# AWS_SECRET_ACCESS_KEY), OFFSITE_AT (HH:MM UTC, default 03:30: after the local backup),
# OFFSITE_KEEP_DAILY / _WEEKLY / _MONTHLY (default 14 / 8 / 6), OFFSITE_RUN_ON_START.
set -eu

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in offsite.env}"
if [ -z "${RESTIC_PASSWORD:-}" ] && [ -z "${RESTIC_PASSWORD_FILE:-}" ]; then
  echo "offsite: set RESTIC_PASSWORD or RESTIC_PASSWORD_FILE in offsite.env" >&2
  exit 2
fi
OFFSITE_AT="${OFFSITE_AT:-03:30}"
STATUS_OK=/tmp/offsite-last-success
STATUS_FAILED=/tmp/offsite-last-failed
STARTED=/tmp/offsite-started
DAY=86400

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | offsite | $*"; }

copy() {
  # Initialise the repository on first use (cat config fails on a missing one).
  if ! restic cat config >/dev/null 2>&1; then
    log "event=offsite_init repository not initialised yet: creating it"
    restic init >/dev/null
  fi
  restic backup /backups --tag chatbot-db --host support-chatbot \
    --exclude '.*.partial' --exclude '*-wal' --exclude '*-shm' --quiet
  restic forget --tag chatbot-db --host support-chatbot --prune --quiet \
    --keep-daily "${OFFSITE_KEEP_DAILY:-14}" \
    --keep-weekly "${OFFSITE_KEEP_WEEKLY:-8}" \
    --keep-monthly "${OFFSITE_KEEP_MONTHLY:-6}"
}

run_once() {
  if copy; then
    date -u +%s > "$STATUS_OK"
    rm -f "$STATUS_FAILED"
    snapshots=$(restic snapshots --tag chatbot-db --json 2>/dev/null | grep -o '"short_id"' | wc -l)
    log "event=offsite ok snapshots=$snapshots"
    return 0
  fi
  date -u +%s > "$STATUS_FAILED"
  log "event=offsite failed (see restic's message above)"
  return 1
}

seconds_until() {  # $1 = HH:MM (UTC): seconds until its next occurrence (never 0)
  hour=$(expr "${1%%:*}" + 0)
  minute=$(expr "${1##*:}" + 0)
  now=$(date -u +%s)
  wait=$(( (hour * 3600 + minute * 60 - now % DAY + DAY) % DAY ))
  [ "$wait" -eq 0 ] && wait=$DAY
  echo "$wait"
}

check() {
  now=$(date -u +%s)
  if [ -f "$STATUS_FAILED" ]; then echo "the last off-server copy failed"; return 1; fi
  if [ -f "$STATUS_OK" ]; then
    age=$(( now - $(cat "$STATUS_OK") ))
    [ "$age" -le $(( DAY + DAY / 2 )) ] && { echo "last copy ${age}s ago"; return 0; }
    echo "the last off-server copy is ${age}s old"; return 1
  fi
  [ -f "$STARTED" ] || { echo "not running"; return 1; }
  age=$(( now - $(cat "$STARTED") ))
  [ "$age" -le $(( DAY + DAY / 2 )) ] && { echo "waiting for the first copy"; return 0; }
  echo "no off-server copy has succeeded since the start"; return 1
}

case "${1:-serve}" in
  once) run_once ;;
  check) check ;;
  serve)
    date -u +%s > "$STARTED"
    trap 'log "event=offsite_stopped"; exit 0' TERM INT
    log "event=offsite_started at=${OFFSITE_AT} UTC"
    if [ "${OFFSITE_RUN_ON_START:-false}" = "true" ]; then run_once || true; fi
    while true; do
      wait_for=$(seconds_until "$OFFSITE_AT")
      log "event=offsite_scheduled in=${wait_for}s"
      sleep "$wait_for" &
      wait $!
      run_once || true
    done
    ;;
  *) echo "usage: offsite.sh [serve|once|check]" >&2; exit 2 ;;
esac
