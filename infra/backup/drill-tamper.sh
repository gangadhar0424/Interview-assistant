#!/usr/bin/env bash
# Backup tampering drill against a REAL PostgreSQL (SEC-10, SEC-11, D-82). Throwaway container only.
#
#   bash infra/backup/drill-tamper.sh
#
# Starts a disposable pgvector/postgres container (name ia-bkdrill-pg, bound to 127.0.0.1:55432), migrates a
# source database with the project's own migrations, adds sample rows, makes a real backup with backup.sh and
# then tries to restore it: once untouched (must succeed and every row count must match), and then after each
# kind of damage (must be REFUSED with a non-zero exit BEFORE any database or uploads write). Everything it
# creates is removed at the end. It never touches any other container or database.
#
# Needs: docker, bash, openssl, python, and `uv` with the API virtualenv (services/api) to run the migrations.
set -uo pipefail
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME=ia-bkdrill-pg
PORT=55432
WORK="$(mktemp -d)"
# Native Windows tools (openssl, docker) cannot resolve MSYS-style /tmp paths: use a path both sides understand.
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "${WORK}")"
PASS_FILE="${WORK}/passphrase"
fail_count=0
pass_count=0
cleanup() { docker rm -f "${NAME}" >/dev/null 2>&1 || true; rm -rf "${WORK}"; }
trap cleanup EXIT

ok()   { pass_count=$((pass_count + 1)); echo "PASS  $1"; }
bad()  { fail_count=$((fail_count + 1)); echo "FAIL  $1"; }
psqlx() { docker exec -i "${NAME}" psql -U bk -v ON_ERROR_STOP=1 "$@"; }
tables_in() { psqlx -d "$1" -At -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"; }

docker rm -f "${NAME}" >/dev/null 2>&1 || true
PGPW="$(python -c 'import secrets;print(secrets.token_hex(12))')"
docker run -d --name "${NAME}" -e POSTGRES_USER=bk -e "POSTGRES_PASSWORD=${PGPW}" -p "127.0.0.1:${PORT}:5432" pgvector/pgvector:pg16 >/dev/null
for _ in $(seq 1 40); do docker exec "${NAME}" pg_isready -U bk >/dev/null 2>&1 && break; sleep 1; done
sleep 2
psqlx -d postgres -c "CREATE DATABASE src" -c "CREATE DATABASE restore_ok" >/dev/null
psqlx -d src -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
( cd "${ROOT}/services/api" && env -u JWT_SECRET -u REDIS_URL DATABASE_URL="postgresql+asyncpg://bk:${PGPW}@127.0.0.1:${PORT}/src" APP_ENV=local uv run alembic upgrade head >/dev/null 2>&1 ) \
  && ok "source database migrated with the project's migrations" || { bad "could not migrate the source database"; exit 1; }
psqlx -d src -c "INSERT INTO users (email, password_hash) VALUES ('drill-1@example.com','x'),('drill-2@example.com','x'),('drill-3@example.com','x')" >/dev/null 2>&1 \
  && echo "      (3 sample users inserted)" || echo "      (sample users could not be inserted; counts will be zero)"

python -c "import secrets;print(secrets.token_urlsafe(48))" > "${PASS_FILE}"
chmod 600 "${PASS_FILE}" 2>/dev/null || true
mkdir -p "${WORK}/uploads-src" && head -c 200000 /dev/urandom > "${WORK}/uploads-src/resume-1.bin" && echo "sample upload" > "${WORK}/uploads-src/note.txt"

export PG_EXEC="docker exec -i ${NAME}" PGUSER=bk BACKUP_PASSPHRASE_FILE="${PASS_FILE}"
backup() { PGDATABASE=src BACKUP_DIR="$1" UPLOADS_DIR="${WORK}/uploads-src" bash "${ROOT}/infra/backup/backup.sh" 2>&1; }
restore_into() { # <db> <uploads dir> <backup dir> -> exit code of restore.sh
  PGDATABASE="$1" UPLOADS_DIR="$2" bash "${ROOT}/infra/backup/restore.sh" "$3" >"${WORK}/last.log" 2>&1
  return $?
}
newest() { ls -1d "$1"/*/ | head -n 1 | sed 's:/$::'; }

mkdir -p "${WORK}/b1" "${WORK}/b2"
backup "${WORK}/b1" | tail -1
B1="$(newest "${WORK}/b1")"
sleep 1.2
backup "${WORK}/b2" | tail -1
B2="$(newest "${WORK}/b2")"
[ -f "${B1}/MANIFEST.mac" ] && [ ! -e "${B1}/SHA256SUMS" ] && ok "backup is the authenticated v2 format (MANIFEST.mac present, no loose SHA256SUMS)" || bad "unexpected backup layout"
grep -q "encrypted=no" "${B1}/MANIFEST" && bad "manifest says encrypted=no" || ok "no plaintext mode: manifest never records encrypted=no"
head -c 8 "${B1}/db.dump.enc" | grep -q "Salted__" && ok "database dump is encrypted (OpenSSL salted header, no PGDMP magic)" || bad "dump does not look encrypted"
grep -aq "PGDMP" "${B1}/db.dump.enc" && bad "plaintext PGDMP marker found in the encrypted dump" || ok "no plaintext PGDMP marker inside the encrypted dump"

# 1. a genuine restore succeeds and every row count matches
restore_into restore_ok "${WORK}/uploads-ok" "${B1}"; code=$?
[ "${code}" = "0" ] && grep -q "OK: schema version and every recorded row count match" "${WORK}/last.log" && ok "untouched backup restores and the schema version and every row count match" || { bad "genuine restore failed (exit ${code})"; tail -5 "${WORK}/last.log"; }
[ "$(psqlx -d restore_ok -At -c 'SELECT count(*) FROM users')" = "$(psqlx -d src -At -c 'SELECT count(*) FROM users')" ] && ok "restored users table matches the source" || bad "restored users table differs"
cmp -s "${WORK}/uploads-src/resume-1.bin" "${WORK}/uploads-ok/resume-1.bin" && ok "restored upload is byte-identical to the original" || bad "restored upload differs"

# 2. every kind of damage is refused, with no database or uploads write
attack() { # <label> <expected exit> <function that damages a copy in $WORK/t>
  local label="$1" want="$2" damage="$3" db="t_$(echo "$1" | tr -c 'a-z0-9' '_' | cut -c1-20)"
  psqlx -d postgres -c "CREATE DATABASE ${db}" >/dev/null
  rm -rf "${WORK}/t" "${WORK}/uploads-t" && cp -r "${B1}" "${WORK}/t"
  ${damage}
  restore_into "${db}" "${WORK}/uploads-t" "${WORK}/t"; code=$?
  local tables; tables="$(tables_in "${db}")"
  if [ "${code}" = "${want}" ] && [ "${tables}" = "0" ] && [ ! -e "${WORK}/uploads-t" ]; then ok "${label}: refused (exit ${code}), database untouched (0 tables), no uploads written"
  else bad "${label}: exit ${code} (wanted ${want}), tables=${tables}, uploads dir exists=$([ -e "${WORK}/uploads-t" ] && echo yes || echo no)"; tail -3 "${WORK}/last.log"; fi
}
flip() { python - "$1" "$2" <<'PY'
import sys
p, off = sys.argv[1], int(sys.argv[2])
b = bytearray(open(p, "rb").read())
b[off % len(b)] ^= 0x01
open(p, "wb").write(b)
PY
}
d_flip_db()       { flip "${WORK}/t/db.dump.enc" 300; }
d_flip_roles()    { flip "${WORK}/t/roles.sql.enc" 40; }
d_flip_uploads()  { flip "${WORK}/t/uploads.tar.gz.enc" 500; }
d_flip_manifest() { sed -i 's/^rows_users=.*/rows_users=999/' "${WORK}/t/MANIFEST"; }
d_flip_mac()      { flip "${WORK}/t/MANIFEST.mac" 40; }
d_truncate_db()   { head -c 1000 "${WORK}/t/db.dump.enc" > "${WORK}/t/x" && mv "${WORK}/t/x" "${WORK}/t/db.dump.enc"; }
d_swap_db()       { cp "${B2}/db.dump.enc" "${WORK}/t/db.dump.enc"; }
d_swap_uploads()  { cp "${B2}/uploads.tar.gz.enc" "${WORK}/t/uploads.tar.gz.enc"; }
d_delete_mac()    { rm "${WORK}/t/MANIFEST.mac"; }
d_delete_db()     { rm "${WORK}/t/db.dump.enc"; }
d_extra_file()    { echo "x" > "${WORK}/t/extra.bin"; }
d_legacy()        { echo "0000" > "${WORK}/t/SHA256SUMS"; rm "${WORK}/t/MANIFEST.mac"; }
d_plain_dump()    { pg_dump_plain=1; docker exec -i "${NAME}" pg_dump --format=custom --no-owner -U bk -d src > "${WORK}/t/db.dump.enc"; }
attack "flipped bit in db dump"            4 d_flip_db
attack "flipped bit in roles"              4 d_flip_roles
attack "flipped bit in uploads"            4 d_flip_uploads
attack "edited manifest row count"         4 d_flip_manifest
attack "flipped bit in manifest mac"       4 d_flip_mac
attack "truncated db dump"                 4 d_truncate_db
attack "db dump swapped for other backup"  4 d_swap_db
attack "uploads swapped for other backup"  4 d_swap_uploads
attack "manifest mac deleted"              4 d_delete_mac
attack "db dump deleted"                   4 d_delete_db
attack "extra file added"                  4 d_extra_file
attack "old-format backup (no mac)"        4 d_legacy
attack "plaintext dump dropped in"         4 d_plain_dump

# 3. a wrong passphrase is refused before anything is written
psqlx -d postgres -c "CREATE DATABASE t_wrongpass" >/dev/null
python -c "import secrets;print(secrets.token_urlsafe(48))" > "${WORK}/wrong"; chmod 600 "${WORK}/wrong" 2>/dev/null || true
BACKUP_PASSPHRASE_FILE="${WORK}/wrong" restore_into t_wrongpass "${WORK}/uploads-w" "${B1}"; code=$?
[ "${code}" = "4" ] && [ "$(tables_in t_wrongpass)" = "0" ] && [ ! -e "${WORK}/uploads-w" ] && ok "wrong passphrase: refused (exit 4) before any write" || bad "wrong passphrase: exit ${code}"

# 4. restore still refuses a non-empty database and a non-empty uploads directory (existing safety)
restore_into restore_ok "${WORK}/uploads-ok2" "${B1}"; code=$?
[ "${code}" = "3" ] && ok "restoring over a database that already has tables is refused (exit 3)" || bad "non-empty database: exit ${code}"

# 5. backup refuses to run without a usable passphrase and writes nothing
mkdir -p "${WORK}/b3"
( unset BACKUP_PASSPHRASE_FILE; PGDATABASE=src BACKUP_DIR="${WORK}/b3" UPLOADS_DIR="${WORK}/uploads-src" bash "${ROOT}/infra/backup/backup.sh" >"${WORK}/last.log" 2>&1 ); code=$?
[ "${code}" != "0" ] && [ -z "$(ls -A "${WORK}/b3")" ] && ok "no passphrase: backup refuses to run (exit ${code}) and writes nothing" || bad "backup ran without a passphrase"
head -c 5 /dev/urandom | base64 > "${WORK}/short"; chmod 600 "${WORK}/short" 2>/dev/null || true
( BACKUP_PASSPHRASE_FILE="${WORK}/short" PGDATABASE=src BACKUP_DIR="${WORK}/b3" UPLOADS_DIR="${WORK}/uploads-src" bash "${ROOT}/infra/backup/backup.sh" >"${WORK}/last.log" 2>&1 ); code=$?
[ "${code}" != "0" ] && [ -z "$(ls -A "${WORK}/b3")" ] && ok "short passphrase: backup refuses to run (exit ${code}) and writes nothing" || bad "backup ran with a short passphrase"

echo
echo "SUMMARY: ${pass_count} PASS, ${fail_count} FAIL"
[ "${fail_count}" = "0" ]
