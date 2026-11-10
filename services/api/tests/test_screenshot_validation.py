"""Phase 11A: PNG validation (pure, no database). Dimensions are checked before anything else."""

import struct
import zlib

import pytest

from app.screenshots.validation import INVALID_IMAGE_MESSAGE, InvalidImage, validate_png
from tests.png_helpers import SIGNATURE, chunk, header_only_png, idat_for, ihdr, make_png

LIMITS = {"max_long_edge": 4096, "max_pixels": 16_000_000}


def check(data: bytes) -> None:
    with pytest.raises(InvalidImage) as raised:
        validate_png(data, **LIMITS)
    assert str(raised.value) == INVALID_IMAGE_MESSAGE  # fixed message, nothing echoed


def test_a_valid_png_is_accepted_and_reports_its_dimensions() -> None:
    result = validate_png(make_png(128, 72), **LIMITS)
    assert (result.width, result.height) == (128, 72)
    assert result.data.startswith(SIGNATURE)
    assert "data=" not in repr(result)  # the bytes are never shown by repr


def test_ancillary_chunks_are_dropped_so_metadata_never_reaches_a_provider() -> None:
    meta = (
        chunk(b"tEXt", b"Comment\x00SECRET-METADATA-SENTINEL")
        + chunk(b"eXIf", b"GPS-SENTINEL")
        + chunk(b"iTXt", b"Author\x00\x00\x00\x00\x00SENTINEL-2")
        + chunk(b"tIME", struct.pack(">HBBBBB", 2026, 11, 10, 1, 2, 3))
    )
    original = make_png(extra_before_idat=meta, extra_after_idat=chunk(b"zTXt", b"k\x00\x00xx"))
    result = validate_png(original, **LIMITS)
    assert b"SENTINEL" not in result.data and b"eXIf" not in result.data
    assert len(result.data) < len(original)
    assert validate_png(result.data, **LIMITS).data == result.data  # idempotent


def test_only_critical_chunks_and_trns_are_kept() -> None:
    palette = chunk(b"PLTE", b"\x00\x00\x00" * 4)
    trns = chunk(b"tRNS", b"\xff\x00\xff\x00")
    raw = (b"\x00" + b"\x00" * 64) * 64
    data = (
        SIGNATURE
        + ihdr(64, 64, bit_depth=8, color_type=3)
        + palette
        + trns
        + chunk(b"gAMA", b"\x00\x00\xb1\x8f")
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND")
    )
    out = validate_png(data, **LIMITS).data
    assert b"PLTE" in out and b"tRNS" in out and b"gAMA" not in out


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a png at all",
        b"\x89PNG\r\n\x1a\n",  # signature only
        b"GIF89a" + b"\x00" * 64,
        b"\xff\xd8\xff\xe0" + b"\x00" * 64,  # JPEG
    ],
)
def test_non_png_input_is_refused(data: bytes) -> None:
    check(data)


def test_truncated_png_is_refused() -> None:
    good = make_png()
    for cut in (10, 30, len(good) // 2, len(good) - 1):
        check(good[:cut])


def test_a_bad_checksum_is_refused() -> None:
    good = bytearray(make_png())
    good[40] ^= 0xFF  # inside the IDAT region
    check(bytes(good))
    bad_crc = SIGNATURE + ihdr(64, 64) + chunk(b"IDAT", b"abc", crc=0) + chunk(b"IEND")
    check(bad_crc)


def test_trailing_bytes_after_iend_are_refused() -> None:
    check(make_png() + b"extra")
    check(make_png() + make_png())


def test_a_missing_iend_or_idat_is_refused() -> None:
    check(SIGNATURE + ihdr(64, 64) + idat_for(64, 64))  # no IEND
    check(SIGNATURE + ihdr(64, 64) + chunk(b"IEND"))  # no IDAT


def test_chunk_order_rules_are_enforced() -> None:
    body = idat_for(64, 64)
    # IHDR must be first and only once
    check(SIGNATURE + body + ihdr(64, 64) + chunk(b"IEND"))
    check(SIGNATURE + ihdr(64, 64) + ihdr(64, 64) + body + chunk(b"IEND"))
    # IDAT chunks must be contiguous
    check(SIGNATURE + ihdr(64, 64) + body + chunk(b"tEXt", b"a\x00b") + body + chunk(b"IEND"))
    # PLTE after IDAT; palette image without PLTE; PLTE on a colour type that forbids it
    check(SIGNATURE + ihdr(64, 64) + body + chunk(b"PLTE", b"\x00\x00\x00") + chunk(b"IEND"))
    check(SIGNATURE + ihdr(64, 64, color_type=3) + body + chunk(b"IEND"))
    check(
        SIGNATURE
        + ihdr(64, 64, color_type=0)
        + chunk(b"PLTE", b"\x00\x00\x00")
        + body
        + chunk(b"IEND")
    )
    # an unknown CRITICAL chunk (upper-case first letter) is refused; an ancillary one is fine
    check(SIGNATURE + ihdr(64, 64) + chunk(b"ABCD", b"x") + body + chunk(b"IEND"))
    validate_png(SIGNATURE + ihdr(64, 64) + chunk(b"abcd", b"x") + body + chunk(b"IEND"), **LIMITS)


def test_an_invalid_colour_type_or_bit_depth_is_refused() -> None:
    body = idat_for(64, 64)
    for bit_depth, color_type in ((8, 1), (3, 2), (16, 3), (1, 6), (8, 5)):
        check(
            SIGNATURE
            + ihdr(64, 64, bit_depth=bit_depth, color_type=color_type)
            + body
            + chunk(b"IEND")
        )


def test_a_chunk_length_that_lies_is_refused() -> None:
    lying = SIGNATURE + ihdr(64, 64) + struct.pack(">I", 2**31 - 1) + b"IDAT" + b"x" * 8
    check(lying)
    check(SIGNATURE + ihdr(64, 64) + struct.pack(">I", 10_000_000) + b"IDAT" + b"x" * 20)


def test_dimension_limits_are_enforced_from_the_header_alone() -> None:
    # A tiny file that CLAIMS a huge image is refused without walking or decoding anything else.
    check(header_only_png(20_000, 20_000))
    check(header_only_png(4097, 100))  # long edge over the limit
    check(header_only_png(100, 4097))
    check(header_only_png(4096, 4096))  # 16,777,216 px > 16,000,000
    check(header_only_png(63, 500))  # short edge below the minimum
    check(header_only_png(500, 10))
    check(SIGNATURE + ihdr(0, 100) + idat_for(1, 1) + chunk(b"IEND"))
    validate_png(header_only_png(4000, 4000), **LIMITS)  # 16,000,000 px exactly is allowed


def test_the_header_is_checked_before_the_rest_of_the_file_is_read() -> None:
    # Garbage after an over-limit header must not matter: refusal comes from the header.
    bomb = SIGNATURE + ihdr(60_000, 60_000) + b"garbage" * 100
    check(bomb)


def test_too_many_ancillary_chunks_are_refused() -> None:
    many = b"".join(chunk(b"tEXt", b"k\x00v") for _ in range(300))
    check(make_png(extra_before_idat=many))


def test_interlaced_png_is_accepted() -> None:
    data = SIGNATURE + ihdr(64, 64, interlace=1) + idat_for(64, 64) + chunk(b"IEND")
    assert validate_png(data, **LIMITS).width == 64
    check(SIGNATURE + ihdr(64, 64, interlace=2) + idat_for(64, 64) + chunk(b"IEND"))
