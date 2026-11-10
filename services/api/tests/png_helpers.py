"""Synthetic PNG builders for the Phase 11A screenshot tests (no real screenshots, no Pillow)."""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(ctype: bytes, body: bytes = b"", *, crc: int | None = None) -> bytes:
    checksum = zlib.crc32(ctype + body) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", checksum)


def ihdr(
    width: int, height: int, *, bit_depth: int = 8, color_type: int = 2, interlace: int = 0
) -> bytes:
    return chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    )


def idat_for(width: int, height: int, channels: int = 3) -> bytes:
    """A real, tiny zlib stream of an all-zero image (so the file is a genuine PNG)."""
    raw = (b"\x00" + b"\x00" * (width * channels)) * height
    return chunk(b"IDAT", zlib.compress(raw, 9))


def make_png(
    width: int = 64,
    height: int = 64,
    *,
    extra_before_idat: bytes = b"",
    extra_after_idat: bytes = b"",
) -> bytes:
    return (
        SIGNATURE
        + ihdr(width, height)
        + extra_before_idat
        + idat_for(width, height)
        + extra_after_idat
        + chunk(b"IEND")
    )


def header_only_png(width: int, height: int) -> bytes:
    """Claims large dimensions with a tiny image body: used to prove the header is checked first."""
    return (
        SIGNATURE
        + ihdr(width, height)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + chunk(b"IEND")
    )


def png_of_size(target_bytes: int) -> bytes:
    """A valid 64x64 PNG padded with an ancillary chunk to reach `target_bytes` (>= the base)."""
    base = make_png()
    padding = max(0, target_bytes - len(base) - 12)
    return make_png(extra_before_idat=chunk(b"tEXt", b"k\x00" + b"x" * padding))
