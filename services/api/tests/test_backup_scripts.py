"""Runs the REAL infra/backup shell scripts against stub pg_* commands (SEC-10, SEC-11).

No database is needed: PG_EXEC points at a tiny stub that emits known bytes. Needs bash, openssl,
tar and sha256sum; the tests FAIL (never skip) when they are missing.
Everything runs with relative paths from a temp directory (Git Bash `tar` reads ``C:`` as a host).
"""
# ruff: noqa: S603

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "infra" / "backup"
IS_WINDOWS = os.name == "nt"

STUB = r"""#!/usr/bin/env bash
# Stub for pg_dump / pg_dumpall / psql / pg_restore. Invoked as: bash stub.sh <tool> <args...>
tool="$1"; shift
case "$tool" in
  pg_dump) printf 'PGDMP-%s\n\000\377binary-tail' "${STUB_TAG:-A}" ;;
  pg_dumpall) printf 'CREATE ROLE stub_%s;\n' "${STUB_TAG:-A}" ;;
  pg_restore)
    if [ "${1:-}" = "--list" ]; then cat >/dev/null; else cat > "${STUB_OUT}/db.restored"; fi ;;
  psql)
    args="$*"
    case "$args" in
      *alembic_version*) echo 0011 ;;
      *pg_tables*) echo "${STUB_TABLES:-0}" ;;
      *) echo 7 ;;
    esac ;;
  *) echo "unknown stub tool $tool" >&2; exit 99 ;;
esac
"""


def _find_bash() -> str:
    if IS_WINDOWS:
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(cand).exists():
                return cand
    found = shutil.which("bash")
    if IS_WINDOWS and found and "system32" in found.lower():
        found = None  # the WSL launcher is not Git Bash
    assert found, "bash (Git Bash on Windows) is required to run the backup script tests"
    return found


BASH = _find_bash()


def sh(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    full = {**os.environ, "MSYS_NO_PATHCONV": "1", **(env or {})}
    return subprocess.run(
        [BASH, *args], cwd=cwd, env=full, capture_output=True, timeout=300, check=False
    )


def test_toolchain_present(tmp_path: Path) -> None:
    out = sh(tmp_path, "-c", "command -v openssl && command -v tar && command -v sha256sum")
    assert out.returncode == 0, "openssl, tar and sha256sum must be available to bash"
    ver = sh(tmp_path, "-c", "openssl version")
    assert b"OpenSSL 3" in ver.stdout, ver.stdout


def _base_env(passfile: str, tag: str = "A") -> dict[str, str]:
    return {
        "BACKUP_DIR": "bk",
        "PGUSER": "u",
        "PGDATABASE": "d",
        "PG_EXEC": "bash stub.sh",
        "UPLOADS_DIR": "uploads_src",
        "BACKUP_PASSPHRASE_FILE": passfile,
        "STUB_TAG": tag,
    }


def _write_pass(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    if not IS_WINDOWS:
        path.chmod(0o600)


def _workdir(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir()
    (d / "stub.sh").write_text(STUB, newline="\n")
    (d / "uploads_src").mkdir()
    (d / "uploads_src" / "a.txt").write_bytes(b"hello uploads")
    return d


def _backup(cwd: Path, passfile: str, tag: str = "A") -> Path:
    r = sh(cwd, str(SCRIPTS / "backup.sh"), env=_base_env(passfile, tag))
    assert r.returncode == 0, r.stderr.decode()
    dirs = sorted(p for p in (cwd / "bk").iterdir() if p.is_dir())
    return dirs[-1]


@pytest.fixture(scope="module")
def good(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Two valid backups (A, B) made with the same passphrase, plus a second passphrase file."""
    base = tmp_path_factory.mktemp("bk")
    w = _workdir(base, "w")
    _write_pass(w / "pass", os.urandom(24).hex().encode() + b"\n")
    _write_pass(w / "pass2", os.urandom(24).hex().encode() + b"\n")
    a = _backup(w, "pass", "A")
    time.sleep(1.1)
    b = _backup(w, "pass", "B")
    return {"work": w, "a": a, "b": b}


def _restore_env(passfile: str = "pass") -> dict[str, str]:
    return {
        "PGUSER": "u",
        "PGDATABASE": "d",
        "PG_EXEC": "bash stub.sh",
        "UPLOADS_DIR": "uploads_restored",
        "BACKUP_PASSPHRASE_FILE": passfile,
        "STUB_OUT": "out",
    }


def _restore(w: Path, src: Path, passfile: str = "pass") -> subprocess.CompletedProcess[bytes]:
    shutil.rmtree(w / "out", ignore_errors=True)
    shutil.rmtree(w / "uploads_restored", ignore_errors=True)
    (w / "out").mkdir()
    return sh(w, str(SCRIPTS / "restore.sh"), str(src), env=_restore_env(passfile))


def _assert_nothing_written(w: Path) -> None:
    assert not (w / "out" / "db.restored").exists(), "database restore must not have started"
    assert not (w / "uploads_restored").exists(), "uploads must not have been touched"


def _copy(good: dict[str, Path], key: str, name: str) -> Path:
    dst = good["work"] / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(good[key], dst)
    return dst


# ---- SEC-10: backup refuses to run without a usable passphrase file ----


def _refuses(w: Path, env_over: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    env = {**_base_env("pass"), **env_over}
    env = {k: v for k, v in env.items() if v is not None}
    r = sh(w, str(SCRIPTS / "backup.sh"), env=env)
    assert r.returncode != 0
    assert not (w / "bk").exists(), "nothing may be written under BACKUP_DIR on refusal"
    return r


def test_backup_refuses_unset_passphrase_file(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    env = {k: v for k, v in _base_env("x").items() if k != "BACKUP_PASSPHRASE_FILE"}
    full = {k: v for k, v in os.environ.items() if k != "BACKUP_PASSPHRASE_FILE"}
    r = subprocess.run(
        [BASH, str(SCRIPTS / "backup.sh")],
        cwd=w,
        env={**full, **env},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert r.returncode != 0
    assert b"BACKUP_PASSPHRASE_FILE is required" in r.stderr
    assert not (w / "bk").exists()


def test_backup_refuses_missing_file(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    r = _refuses(w, {"BACKUP_PASSPHRASE_FILE": "nope"})
    assert b"not a regular file" in r.stderr


def test_backup_refuses_directory_as_passphrase(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    (w / "pass").mkdir()
    _refuses(w, {})


def test_backup_refuses_empty_passphrase_file(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    _write_pass(w / "pass", b"")
    r = _refuses(w, {})
    assert b"at least 32 bytes" in r.stderr


def test_backup_refuses_short_passphrase_file(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    _write_pass(w / "pass", b"x" * 31 + b"\n")
    r = _refuses(w, {})
    assert b"at least 32 bytes" in r.stderr


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX mode bits are not meaningful under Git Bash/NTFS")
def test_backup_refuses_loose_permissions(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    _write_pass(w / "pass", b"y" * 40 + b"\n")
    (w / "pass").chmod(0o644)
    r = _refuses(w, {})
    assert b"group or others" in r.stderr


def test_backup_refuses_missing_uploads_config_without_writing(tmp_path: Path) -> None:
    w = _workdir(tmp_path, "w")
    _write_pass(w / "pass", b"z" * 40 + b"\n")
    env = _base_env("pass")
    del env["UPLOADS_DIR"]
    full = {k: v for k, v in os.environ.items() if k not in ("UPLOADS_DIR", "UPLOADS_VOLUME")}
    r = subprocess.run(
        [BASH, str(SCRIPTS / "backup.sh")],
        cwd=w,
        env={**full, **env},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert r.returncode != 0
    assert not (w / "bk").exists()


# ---- round trip + tamper detection ----


def test_round_trip_and_format(good: dict[str, Path]) -> None:
    a = good["a"]
    assert sorted(p.name for p in a.iterdir()) == [
        "MANIFEST",
        "MANIFEST.mac",
        "db.dump.enc",
        "roles.sql.enc",
        "uploads.tar.gz.enc",
    ]
    manifest = (a / "MANIFEST").read_text()
    assert "encrypted=no" not in manifest
    assert "format=ia-backup-v2" in manifest
    assert b"PGDMP" not in (a / "db.dump.enc").read_bytes()
    w = good["work"]
    r = _restore(w, a)
    assert r.returncode == 0, r.stderr.decode()
    assert b"OK: schema version and every recorded row count match" in r.stdout
    assert (w / "out" / "db.restored").read_bytes() == b"PGDMP-A\n\x00\xffbinary-tail"
    assert (w / "uploads_restored" / "a.txt").read_bytes() == b"hello uploads"


def test_wrong_passphrase_fails_closed(good: dict[str, Path]) -> None:
    w = good["work"]
    r = _restore(w, good["a"], "pass2")
    assert r.returncode != 0
    assert b"AUTHENTICATION FAILED" in r.stderr
    _assert_nothing_written(w)


@pytest.mark.parametrize(
    "name", ["db.dump.enc", "roles.sql.enc", "uploads.tar.gz.enc", "MANIFEST", "MANIFEST.mac"]
)
def test_flipped_bit_detected(good: dict[str, Path], name: str) -> None:
    d = _copy(good, "a", "tamper")
    data = bytearray((d / name).read_bytes())
    # MANIFEST.mac is text: flip a bit inside the hex digest line; others in the middle.
    pos = len(data) - 3 if name == "MANIFEST.mac" else len(data) // 2
    data[pos] ^= 0x01
    (d / name).write_bytes(bytes(data))
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0, r.stdout.decode()
    _assert_nothing_written(w)


@pytest.mark.parametrize("name", ["db.dump.enc", "roles.sql.enc", "uploads.tar.gz.enc", "MANIFEST"])
def test_truncated_file_detected(good: dict[str, Path], name: str) -> None:
    d = _copy(good, "a", "tamper")
    data = (d / name).read_bytes()
    (d / name).write_bytes(data[:-1])
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0
    _assert_nothing_written(w)


@pytest.mark.parametrize("name", ["db.dump.enc", "roles.sql.enc", "uploads.tar.gz.enc"])
def test_swap_with_validly_encrypted_file_from_other_backup(
    good: dict[str, Path], name: str
) -> None:
    d = _copy(good, "a", "tamper")
    shutil.copyfile(good["b"] / name, d / name)  # same passphrase, valid ciphertext, other backup
    assert (d / name).read_bytes() != (good["a"] / name).read_bytes()
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0
    assert b"AUTHENTICATION FAILED" in r.stderr
    _assert_nothing_written(w)


def test_swapped_manifest_pair_from_other_backup_detected(good: dict[str, Path]) -> None:
    # Replace MANIFEST + MANIFEST.mac with B's (a valid pair) while keeping A's data files.
    d = _copy(good, "a", "tamper")
    shutil.copyfile(good["b"] / "MANIFEST", d / "MANIFEST")
    shutil.copyfile(good["b"] / "MANIFEST.mac", d / "MANIFEST.mac")
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0
    _assert_nothing_written(w)


@pytest.mark.parametrize("name", ["db.dump.enc", "roles.sql.enc", "uploads.tar.gz.enc", "MANIFEST"])
def test_deleted_file_detected(good: dict[str, Path], name: str) -> None:
    d = _copy(good, "a", "tamper")
    (d / name).unlink()
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0
    _assert_nothing_written(w)


def test_extra_file_detected(good: dict[str, Path]) -> None:
    d = _copy(good, "a", "tamper")
    (d / "extra.bin").write_bytes(b"surprise")
    w = good["work"]
    r = _restore(w, d)
    assert r.returncode != 0
    assert b"missing or extra" in r.stderr
    _assert_nothing_written(w)


def test_old_plaintext_format_refused(good: dict[str, Path]) -> None:
    w = good["work"]
    d = w / "old_plain"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    (d / "db.dump").write_bytes(b"PGDMP plain")
    (d / "roles.sql").write_bytes(b"CREATE ROLE x;")
    (d / "uploads.tar.gz").write_bytes(b"x")
    (d / "MANIFEST").write_text(
        "created_utc=20200101T000000Z\nalembic_version=0011\nencrypted=no\n"
    )
    sh(d, "-c", "sha256sum db.dump roles.sql uploads.tar.gz MANIFEST > SHA256SUMS")
    r = _restore(w, d)
    assert r.returncode != 0
    assert b"old plaintext/unauthenticated format is refused" in r.stderr
    _assert_nothing_written(w)


def test_old_encrypted_without_mac_format_refused(good: dict[str, Path]) -> None:
    w = good["work"]
    d = w / "old_enc"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    for n in ("db.dump", "roles.sql", "uploads.tar.gz"):
        r0 = sh(
            w,
            "-c",
            f"echo data | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 "
            f"-pass file:pass > old_enc/{n}.enc",
        )
        assert r0.returncode == 0
    (d / "MANIFEST").write_text(
        "created_utc=20200101T000000Z\nalembic_version=0011\nencrypted=yes\n"
    )
    sh(d, "-c", "sha256sum db.dump.enc roles.sql.enc uploads.tar.gz.enc MANIFEST > SHA256SUMS")
    r = _restore(w, d)
    assert r.returncode != 0
    assert b"old plaintext/unauthenticated format is refused" in r.stderr
    _assert_nothing_written(w)


def test_restore_still_refuses_non_empty_database(good: dict[str, Path]) -> None:
    w = good["work"]
    shutil.rmtree(w / "out", ignore_errors=True)
    (w / "out").mkdir()
    shutil.rmtree(w / "uploads_restored", ignore_errors=True)
    env = {**_restore_env(), "STUB_TABLES": "5"}
    r = sh(w, str(SCRIPTS / "restore.sh"), str(good["a"]), env=env)
    assert r.returncode == 3
    _assert_nothing_written(w)


# ---- prune ----


def test_prune_only_touches_valid_new_format_backups(tmp_path: Path, good: dict[str, Path]) -> None:
    w = _workdir(tmp_path, "w")
    root = w / "store"
    root.mkdir()
    valid = root / "20200101T000000Z"
    shutil.copytree(good["a"], valid)
    old_fmt = root / "20200102T000000Z"
    old_fmt.mkdir()
    (old_fmt / "SHA256SUMS").write_text("")
    (old_fmt / "MANIFEST").write_text("encrypted=no\n")
    damaged = root / "20200103T000000Z"
    shutil.copytree(good["a"], damaged)
    (damaged / "db.dump.enc").unlink()
    env = {"BACKUP_DIR": "store", "RETENTION_DAYS": "1", "MIN_KEEP": "0"}
    dry = sh(w, str(SCRIPTS / "prune.sh"), env=env)
    assert dry.returncode == 0, dry.stderr.decode()
    assert valid.exists() and old_fmt.exists() and damaged.exists()
    assert b"would remove 20200101T000000Z" in dry.stdout
    assert b"20200102T000000Z" not in dry.stdout and b"20200103T000000Z" not in dry.stdout
    applied = sh(w, str(SCRIPTS / "prune.sh"), "--apply", env=env)
    assert applied.returncode == 0
    assert not valid.exists()
    assert old_fmt.exists() and damaged.exists()


def test_prune_default_floor_keeps_old_backups_and_says_so(
    tmp_path: Path, good: dict[str, Path]
) -> None:
    """SEC-30: the newest MIN_KEEP survive RETENTION_DAYS, and the output now says so."""
    w = _workdir(tmp_path, "w")
    root = w / "store"
    root.mkdir()
    names = ["20200101T000000Z", "20200102T000000Z", "20200103T000000Z"]
    for name in names:
        shutil.copytree(good["a"], root / name)
    env = {"BACKUP_DIR": "store", "RETENTION_DAYS": "1"}  # MIN_KEEP defaults to 3
    applied = sh(w, str(SCRIPTS / "prune.sh"), "--apply", env=env)
    assert applied.returncode == 0, applied.stderr.decode()
    # Reproduces the finding: years-old backups are still there with the stated 1-day retention.
    assert all((root / n).exists() for n in names)
    out = applied.stdout.decode()
    for name in names:
        assert f"NOTE: keeping {name}" in out
    # The documented way to get strict retention.
    strict = sh(w, str(SCRIPTS / "prune.sh"), "--apply", env={**env, "MIN_KEEP": "0"})
    assert strict.returncode == 0
    assert not any((root / n).exists() for n in names)
