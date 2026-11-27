#!/usr/bin/env bash
# Retention for backups made by backup.sh (M-12, D-79). DRY RUN unless --apply is given.
#
#   BACKUP_DIR=/srv/backups RETENTION_DAYS=30 prune.sh [--apply]
#
# Only directories directly under BACKUP_DIR whose names are exactly a UTC timestamp
# (YYYYMMDDTHHMMSSZ) and that are structurally valid ia-backup-v2 backups (MANIFEST, MANIFEST.mac and exactly the
# three encrypted files; old-format or damaged directories are never touched) are ever considered, and the newest
# MIN_KEEP (default 3) are always kept, so a clock error or an empty value can never empty the folder.
# CONSEQUENCE (SEC-30): that floor overrides RETENTION_DAYS, so up to MIN_KEEP backups can outlive it; the
# script prints a NOTE for each one. Where deletion deadlines matter (docs/DATA_LIFECYCLE.md), set MIN_KEEP=0.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

: "${BACKUP_DIR:?set BACKUP_DIR}" "${RETENTION_DAYS:?set RETENTION_DAYS (days to keep)}"
MIN_KEEP="${MIN_KEEP:-3}"
apply=0
[ "${1:-}" = "--apply" ] && apply=1
case "${RETENTION_DAYS}" in ''|*[!0-9]*) echo "RETENTION_DAYS must be a whole number" >&2; exit 2;; esac
[ "${RETENTION_DAYS}" -ge 1 ] || { echo "RETENTION_DAYS must be at least 1" >&2; exit 2; }
case "${BACKUP_DIR}" in ''|/|.|..) echo "refusing an unsafe BACKUP_DIR" >&2; exit 2;; esac
[ -d "${BACKUP_DIR}" ] || { echo "BACKUP_DIR does not exist" >&2; exit 2; }

cutoff="$(date -u -d "-${RETENTION_DAYS} days" +%Y%m%dT%H%M%SZ)"
mapfile -t all < <(ls -1 "${BACKUP_DIR}" | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort)
total="${#all[@]}"
echo "[prune] ${total} backup(s); cutoff ${cutoff}; always keeping the newest ${MIN_KEEP}"
removable=()
for i in "${!all[@]}"; do
  name="${all[$i]}"
  backup_structure_ok "${BACKUP_DIR%/}/${name}" prune 2>/dev/null || continue
  if [ "${i}" -ge $((total - MIN_KEEP)) ]; then
    # SEC-30: the newest MIN_KEEP are never removed, so real retention can exceed RETENTION_DAYS.
    # Say so instead of letting the stated retention be silently wrong.
    [[ "${name}" < "${cutoff}" ]] && echo "[prune] NOTE: keeping ${name}, older than ${RETENTION_DAYS} day(s), because it is one of the newest ${MIN_KEEP} (set MIN_KEEP=0 to disable that floor)"
    continue
  fi
  [[ "${name}" < "${cutoff}" ]] && removable+=("${name}")
done
for name in "${removable[@]:-}"; do
  [ -n "${name}" ] || continue
  if [ "${apply}" = "1" ]; then
    echo "[prune] removing ${name}"
    rm -r -- "${BACKUP_DIR%/}/${name}"
  else
    echo "[prune] would remove ${name} (dry run; pass --apply)"
  fi
done
echo "[prune] done (${#removable[@]} candidate(s))"
