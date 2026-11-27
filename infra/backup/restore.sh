#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database and an empty uploads location (M-12, D-79).
#
#   restore.sh <backup-directory>
#
# Configuration (environment):
#   PGUSER, PGDATABASE    target database identity (required); the database must already exist and be empty.
#   PG_EXEC               as in backup.sh (runs psql/pg_restore next to the TARGET database).
#   UPLOADS_VOLUME        Docker volume to restore the uploads into (must be empty), or
#   UPLOADS_DIR           a host directory (must be empty).
#   BACKUP_PASSPHRASE_FILE  required (same rules as backup.sh). The whole backup set is authenticated
#                         (MANIFEST.mac) BEFORE anything is decrypted or written; old-format backups
#                         (plaintext, or encrypted without a MAC) are refused.
#   RESTORE_ROLES=1       also create the roles recorded in roles.sql (needed on a brand-new cluster;
#                         "already exists" messages are expected and ignored; passwords are never restored).
#
# It refuses to touch a database that already has tables, so a restore can never silently overwrite
# live data. To restore over a live system, restore into a new database, verify it, then switch over.
#
# Roles are cluster-level and not part of the dump: before restoring, the target cluster needs the
# interview_app_runtime role (and the login roles) from docs/DEPLOYMENT.md.
set -euo pipefail
umask 077

src="${1:?usage: restore.sh <backup-directory>}"
: "${PGUSER:?set PGUSER}" "${PGDATABASE:?set PGDATABASE}"
PG_EXEC="${PG_EXEC:-}"
[ -d "${src}" ] || { echo "[restore] ERROR: ${src} is not a directory" >&2; exit 2; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

require_passphrase_file restore
echo "[restore] authenticating the backup set (nothing is decrypted or written until this passes)"
verify_backup "${src}" restore || exit 4
suffix=".enc"

tables="$(${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -At -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "${tables}" != "0" ]; then
  echo "[restore] ERROR: the target database is not empty (${tables} tables). Restore into a new, empty database." >&2
  exit 3
fi

if [ "${RESTORE_ROLES:-0}" = "1" ] && [ -f "${src}/roles.sql${suffix}" ]; then
  echo "[restore] roles (definitions only)"
  decrypt < "${src}/roles.sql${suffix}" | ${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -q -v ON_ERROR_STOP=0 >/dev/null 2>&1 || true
fi

echo "[restore] database"
decrypt < "${src}/db.dump${suffix}" | ${PG_EXEC} pg_restore --exit-on-error --no-owner -U "${PGUSER}" -d "${PGDATABASE}"

echo "[restore] uploads"
if [ -n "${UPLOADS_VOLUME:-}" ]; then
  empty="$(MSYS_NO_PATHCONV=1 docker run --rm -v "${UPLOADS_VOLUME}:/data" busybox sh -c 'ls -A /data | wc -l')"
  [ "${empty}" = "0" ] || { echo "[restore] ERROR: the uploads volume is not empty" >&2; exit 3; }
  decrypt < "${src}/uploads.tar.gz${suffix}" | MSYS_NO_PATHCONV=1 docker run --rm -i -v "${UPLOADS_VOLUME}:/data" busybox tar xzf - -C /data
elif [ -n "${UPLOADS_DIR:-}" ]; then
  mkdir -p "${UPLOADS_DIR}"
  [ -z "$(ls -A "${UPLOADS_DIR}")" ] || { echo "[restore] ERROR: ${UPLOADS_DIR} is not empty" >&2; exit 3; }
  decrypt < "${src}/uploads.tar.gz${suffix}" | tar xzf - -C "${UPLOADS_DIR}"
else
  echo "[restore] ERROR: set UPLOADS_VOLUME or UPLOADS_DIR" >&2
  exit 2
fi

echo "[restore] verifying against the manifest"
status=0
expected_version="$(sed -n 's/^alembic_version=//p' "${src}/MANIFEST")"
actual_version="$(${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -At -c 'SELECT version_num FROM alembic_version')"
if [ "${expected_version}" != "${actual_version}" ]; then
  echo "[restore] MISMATCH schema version: backup ${expected_version}, restored ${actual_version}" >&2
  status=1
fi
for line in $(grep '^rows_' "${src}/MANIFEST"); do
  table="${line%%=*}"; table="${table#rows_}"; want="${line#*=}"
  got="$(${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -At -c "SELECT count(*) FROM ${table}")"
  if [ "${want}" != "${got}" ]; then
    echo "[restore] MISMATCH ${table}: backup ${want}, restored ${got}" >&2
    status=1
  fi
done
[ "${status}" = "0" ] && echo "[restore] OK: schema version and every recorded row count match" || echo "[restore] FAILED verification" >&2
exit "${status}"
