# Shared helpers for backup.sh / restore.sh / prune.sh (SEC-10, SEC-11). Sourced, not executed.
#
# FORMAT "ia-backup-v2" (authenticated; the only format these scripts accept):
#   db.dump.enc, roles.sql.enc, uploads.tar.gz.enc   AES-256-CTR, PBKDF2-HMAC-SHA256 (600000 iterations),
#                                                    fresh random 8-byte salt per file ("Salted__" header)
#   MANIFEST      plain text: format line, counts, and one `file=<name> <bytes> <sha256>` line per file
#   MANIFEST.mac  "ia-backup-mac-v2" / <16 hex salt> / <64 hex HMAC-SHA256 of MANIFEST>
#
# Design (encrypt-then-MAC with a manifest): the MAC key is derived from the passphrase with PBKDF2 and its
# own random salt, independent of every file's encryption key. One HMAC covers MANIFEST, and MANIFEST commits
# to every ciphertext by length and SHA-256, so changing, truncating, swapping (even for a validly encrypted
# file from another backup made with the same passphrase), adding, removing or reordering any file is
# detected before anything is decrypted. Trade-offs: CTR is malleable on its own, which is why nothing is
# decrypted before the manifest MAC verifies; the manifest is not secret (schema version, row counts, sizes);
# the MAC compare is not constant time (offline check, no oracle). Without gpg/age, so only openssl 3 + bash
# + coreutils are required.
#
# Secrets never reach argv: openssl reads the passphrase with `-pass file:`; the derived MAC key lives in a
# shell variable and HMAC is computed from the SHA-256 primitive (ipad/opad blocks written by the printf
# builtin into a pipe), because `openssl dgst -hmac` / `openssl mac -macopt` would put the key on argv.
# No temporary files are used, so nothing is decrypted to disk.

BACKUP_FORMAT="ia-backup-v2"
BACKUP_PBKDF2_ITER=600000
BACKUP_FILES=(db.dump.enc roles.sql.enc uploads.tar.gz.enc)

die() { echo "[$1] ERROR: $2" >&2; exit "${3:-2}"; }

# Require a usable passphrase file: regular, first line >= 32 bytes (openssl reads only the first line),
# and not accessible by group/other. Mode bits are meaningless under Git Bash/MSYS/Cygwin (NTFS ACLs),
# so the permission check is skipped there; protect the file with Windows ACLs instead.
require_passphrase_file() {
  local who="$1" f="${BACKUP_PASSPHRASE_FILE:-}"
  [ -n "${f}" ] || die "${who}" "BACKUP_PASSPHRASE_FILE is required (unencrypted backups are not supported)"
  [ -f "${f}" ] || die "${who}" "BACKUP_PASSPHRASE_FILE is not a regular file"
  local line
  line="$(LC_ALL=C head -n 1 "${f}" 2>/dev/null || true)"
  [ "$(LC_ALL=C; echo "${#line}")" -ge 32 ] || die "${who}" "BACKUP_PASSPHRASE_FILE must hold a passphrase of at least 32 bytes on its first line"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *)
      local mode
      mode="$(stat -c %a "${f}" 2>/dev/null || stat -f %Lp "${f}")"
      [ $(( 8#${mode} & 8#077 )) -eq 0 ] || die "${who}" "BACKUP_PASSPHRASE_FILE must not be accessible by group or others (chmod 600)"
      ;;
  esac
}

encrypt() { openssl enc -aes-256-ctr -salt -pbkdf2 -iter "${BACKUP_PBKDF2_ITER}" -pass "file:${BACKUP_PASSPHRASE_FILE}"; }
decrypt() { openssl enc -d -aes-256-ctr -pbkdf2 -iter "${BACKUP_PBKDF2_ITER}" -pass "file:${BACKUP_PASSPHRASE_FILE}"; }

# PBKDF2(passphrase, salt16hex-as-8-bytes) -> 64 hex chars (the 32-byte key) on stdout.
mac_key_hex() {
  openssl enc -aes-256-ctr -pbkdf2 -iter "${BACKUP_PBKDF2_ITER}" -S "$1" -pass "file:${BACKUP_PASSPHRASE_FILE}" -P </dev/null \
    | sed -n 's/^key=//p'
}

# A 64-byte HMAC pad block: key (32 bytes, hex) zero-padded, XOR $2 (0x36 inner / 0x5c outer), to stdout.
hmac_pad() {
  local hex="$1" x="$2" i out=""
  # Octal escapes (\0NNN) rendered by `printf %b`; the backslash is doubled so the inner printf keeps it.
  for ((i = 0; i < 32; i++)); do out+="$(printf '\\0%03o' $(( 0x${hex:$((i * 2)):2} ^ x )))"; done
  for ((i = 32; i < 64; i++)); do out+="$(printf '\\0%03o' $(( x )))"; done
  printf '%b' "${out}"
}

# HMAC-SHA256(key_hex, file) -> hex. Streams the file; the key never appears on a command line.
hmac_file() {
  local key="$1" file="$2"
  { hmac_pad "${key}" $((0x36)); cat "${file}"; } | openssl dgst -sha256 -binary \
    | { hmac_pad "${key}" $((0x5c)); cat; } | openssl dgst -sha256 -r | cut -d' ' -f1
}

# Write MANIFEST.mac for MANIFEST in directory $1.
write_manifest_mac() {
  local dir="$1" salt key mac
  salt="$(openssl rand -hex 8)"
  key="$(mac_key_hex "${salt}")"
  [ "${#key}" -eq 64 ] || die backup "could not derive the MAC key"
  mac="$(hmac_file "${key}" "${dir}/MANIFEST")"
  printf 'ia-backup-mac-v2\n%s\n%s\n' "${salt}" "${mac}" > "${dir}/MANIFEST.mac"
}

# Structural check (no passphrase needed): new-format markers present, every listed file present, nothing else.
# Prints a reason on stderr and returns 1 if not a new-format backup directory.
backup_structure_ok() {
  local dir="$1" who="$2"
  if [ -e "${dir}/SHA256SUMS" ] || [ ! -f "${dir}/MANIFEST.mac" ] || [ ! -f "${dir}/MANIFEST" ]; then
    echo "[${who}] ${dir}: not an ia-backup-v2 backup (old plaintext/unauthenticated format is refused; no downgrade)" >&2
    return 1
  fi
  [ "$(head -n 1 "${dir}/MANIFEST")" = "format=${BACKUP_FORMAT}" ] || { echo "[${who}] ${dir}: unknown MANIFEST format" >&2; return 1; }
  local expected actual
  expected="$(printf '%s\n' MANIFEST MANIFEST.mac "${BACKUP_FILES[@]}" | LC_ALL=C sort)"
  actual="$(ls -A "${dir}" | LC_ALL=C sort)"
  [ "${expected}" = "${actual}" ] || { echo "[${who}] ${dir}: file set differs from the expected set (missing or extra files)" >&2; return 1; }
  return 0
}

# Full authenticity check of a backup directory: MAC of MANIFEST first, then every file's length and SHA-256
# against the authenticated MANIFEST. Nothing is decrypted. Returns non-zero (with a message) on any problem.
verify_backup() {
  local dir="$1" who="$2"
  backup_structure_ok "${dir}" "${who}" || return 1
  local magic salt mac
  { IFS= read -r magic; IFS= read -r salt; IFS= read -r mac; } < "${dir}/MANIFEST.mac" || true
  [ "${magic}" = "ia-backup-mac-v2" ] && [[ "${salt}" =~ ^[0-9a-f]{16}$ ]] && [[ "${mac}" =~ ^[0-9a-f]{64}$ ]] \
    || { echo "[${who}] MANIFEST.mac is malformed" >&2; return 1; }
  local key actual
  key="$(mac_key_hex "${salt}")"
  [ "${#key}" -eq 64 ] || { echo "[${who}] could not derive the MAC key" >&2; return 1; }
  actual="$(hmac_file "${key}" "${dir}/MANIFEST")"
  [ "${actual}" = "${mac}" ] || { echo "[${who}] AUTHENTICATION FAILED: wrong passphrase or the backup was modified (MANIFEST)" >&2; return 1; }
  # MANIFEST is now authentic: its file lines must be exactly the expected names in order, and match the files.
  local listed
  listed="$(sed -n 's/^file=\([^ ]*\) .*/\1/p' "${dir}/MANIFEST")"
  [ "${listed}" = "$(printf '%s\n' "${BACKUP_FILES[@]}")" ] || { echo "[${who}] AUTHENTICATION FAILED: file list in MANIFEST is not the expected list" >&2; return 1; }
  local name size sum f line
  for f in "${BACKUP_FILES[@]}"; do
    line="$(grep -F "file=${f} " "${dir}/MANIFEST")"
    read -r name size sum <<< "${line}"
    [[ "${size}" =~ ^[0-9]+$ ]] && [[ "${sum}" =~ ^[0-9a-f]{64}$ ]] || { echo "[${who}] AUTHENTICATION FAILED: malformed file entry for ${f}" >&2; return 1; }
    [ -f "${dir}/${f}" ] && [ ! -L "${dir}/${f}" ] || { echo "[${who}] ${f} is not a regular file" >&2; return 1; }
    [ "$(wc -c < "${dir}/${f}" | tr -d ' ')" = "${size}" ] || { echo "[${who}] AUTHENTICATION FAILED: ${f} has the wrong length (truncated or replaced)" >&2; return 1; }
    [ "$(sha256sum < "${dir}/${f}" | cut -d' ' -f1)" = "${sum}" ] || { echo "[${who}] AUTHENTICATION FAILED: ${f} does not match the authenticated manifest (modified or swapped)" >&2; return 1; }
  done
  return 0
}
