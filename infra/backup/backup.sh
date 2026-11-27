#!/usr/bin/env bash
# Coordinated backup of the Interview Assistant database and uploads (M-12, D-79).
#
# The database holds metadata that points at files in the uploads volume, so both are captured together
# and restored together. The backup is verified when it is created (archive readable, checksums written,
# row counts recorded), but only a RESTORE proves it works: run restore-drill.sh regularly.
#
# Configuration (environment):
#   BACKUP_DIR            where to write (required). Each run creates <BACKUP_DIR>/<UTC timestamp>/.
#   PGUSER, PGDATABASE    database identity (required). PGPASSWORD or ~/.pgpass for authentication.
#   PG_EXEC               prefix that runs a command next to the database, e.g.
#                         PG_EXEC="docker exec -i interview-assistant-prod-postgres-1"; default: run locally.
#   UPLOADS_VOLUME        Docker volume holding the uploads (default: none; set UPLOADS_DIR for a path).
#   UPLOADS_DIR           host path of the uploads when not using a Docker volume.
#   BACKUP_PASSPHRASE_FILE  REQUIRED. A regular file whose first line is a passphrase of >= 32 bytes, not
#                         readable by group/other. Backups are always encrypted and authenticated (format
#                         ia-backup-v2, see lib.sh); there is no unencrypted mode. Never written in clear text.
#
# Never put the passphrase, database password or backup contents in a shared place; restrict BACKUP_DIR.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

# All validation happens BEFORE anything is created under BACKUP_DIR.
: "${BACKUP_DIR:?set BACKUP_DIR}" "${PGUSER:?set PGUSER}" "${PGDATABASE:?set PGDATABASE}"
require_passphrase_file backup
[ -n "${UPLOADS_VOLUME:-}" ] || [ -n "${UPLOADS_DIR:-}" ] || die backup "set UPLOADS_VOLUME or UPLOADS_DIR (a database without its files is not a backup)"
PG_EXEC="${PG_EXEC:-}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="${BACKUP_DIR%/}/${stamp}"
mkdir -p "${BACKUP_DIR}"
mkdir "${dest}"
ok=0
# A failed run must not leave a partial directory behind.
trap '[ "${ok}" = "1" ] || rm -rf -- "${dest}"' EXIT

echo "[backup] database -> ${dest}/db.dump.enc"
# Custom format: compressed, selective restore, and `pg_restore --list` can check it. Grants are KEPT
# (the runtime role's privileges are part of the row-level-security design); ownership is not.
${PG_EXEC} pg_dump --format=custom --no-owner -U "${PGUSER}" -d "${PGDATABASE}" | encrypt > "${dest}/db.dump.enc"

echo "[backup] roles -> ${dest}/roles.sql.enc (definitions only, no password hashes)"
# The dump refers to roles (the owner, interview_app_runtime) that live in the cluster, not in the
# database, so a restore into a fresh cluster needs them. Passwords are NOT included: provision the
# login passwords from your secret manager.
${PG_EXEC} pg_dumpall --globals-only --no-role-passwords -U "${PGUSER}" | encrypt > "${dest}/roles.sql.enc"

echo "[backup] uploads -> ${dest}/uploads.tar.gz.enc"
if [ -n "${UPLOADS_VOLUME:-}" ]; then
  MSYS_NO_PATHCONV=1 docker run --rm -v "${UPLOADS_VOLUME}:/data:ro" busybox tar czf - -C /data . | encrypt > "${dest}/uploads.tar.gz.enc"
else
  tar czf - -C "${UPLOADS_DIR}" . | encrypt > "${dest}/uploads.tar.gz.enc"
fi

echo "[backup] verifying the archives are readable"
decrypt < "${dest}/db.dump.enc" | ${PG_EXEC} pg_restore --list >/dev/null
decrypt < "${dest}/uploads.tar.gz.enc" | tar tzf - >/dev/null

echo "[backup] recording the authenticated manifest (counts, schema version, file lengths and digests: no content)"
{
  echo "format=${BACKUP_FORMAT}"
  echo "created_utc=${stamp}"
  echo "alembic_version=$(${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -At -c 'SELECT version_num FROM alembic_version')"
  for table in users resumes documents ingestion_jobs chunks chunk_embeddings sessions detected_questions generated_answers; do
    echo "rows_${table}=$(${PG_EXEC} psql -U "${PGUSER}" -d "${PGDATABASE}" -At -c "SELECT count(*) FROM ${table}")"
  done
  for f in "${BACKUP_FILES[@]}"; do
    echo "file=${f} $(wc -c < "${dest}/${f}" | tr -d ' ') $(sha256sum < "${dest}/${f}" | cut -d' ' -f1)"
  done
} > "${dest}/MANIFEST"
write_manifest_mac "${dest}"
ok=1
echo "[backup] done: ${dest}"
echo "${dest}"
