"""PNG validation for screenshots, without decoding pixels and without a new dependency.

The bytes come from an untrusted client, so nothing here trusts them: the signature, a bounded
chunk walk with CRC checks, header dimensions (checked BEFORE the rest of the file is walked, so a
decompression-bomb header is refused cheaply), a strict chunk order and no trailing data. Only the
critical chunks (plus `tRNS`, which affects how palette/greyscale pixels are read) are re-emitted:
ancillary chunks such as text, EXIF, colour-profile and time stamps are dropped, so metadata never
reaches a vision provider. Error messages are fixed strings and never echo any content.
"""

import struct
import zlib
from dataclasses import dataclass, field

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
KEPT_CHUNKS = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})
MAX_CHUNKS = 20_000
MAX_CHUNK_LENGTH = 2**31 - 1
MAX_ANCILLARY_CHUNKS = 256
MIN_EDGE_PIXELS = 64

# colour type -> allowed bit depths (PNG specification, table 11.1)
_BIT_DEPTHS: dict[int, frozenset[int]] = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}

INVALID_IMAGE_MESSAGE = "The image is not a valid PNG within the allowed size."


class InvalidImage(Exception):
    """The bytes are not an acceptable PNG (the message is fixed and safe to show)."""

    def __init__(self) -> None:
        super().__init__(INVALID_IMAGE_MESSAGE)


@dataclass(frozen=True, slots=True)
class ValidatedPng:
    data: bytes = field(repr=False)  # normalised: critical chunks only
    width: int
    height: int


def validate_png(
    data: bytes, *, max_long_edge: int, max_pixels: int, min_edge: int = MIN_EDGE_PIXELS
) -> ValidatedPng:
    """Validate and normalise a PNG; raise `InvalidImage` for anything not acceptable."""
    if len(data) < len(PNG_SIGNATURE) + 25 or not data.startswith(PNG_SIGNATURE):
        raise InvalidImage
    pos = len(PNG_SIGNATURE)
    kept: list[bytes] = [PNG_SIGNATURE]
    width = height = 0
    color_type = -1
    seen_plte = seen_idat = seen_iend = False
    idat_finished = False
    chunks = ancillary = 0
    end = len(data)

    while pos < end:
        if seen_iend:  # trailing bytes after IEND
            raise InvalidImage
        chunks += 1
        if chunks > MAX_CHUNKS or pos + 12 > end:
            raise InvalidImage
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        if length > MAX_CHUNK_LENGTH or pos + 12 + length > end:
            raise InvalidImage
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in ctype):
            raise InvalidImage
        body = data[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise InvalidImage
        raw = data[pos : pos + 12 + length]
        pos += 12 + length

        if chunks == 1:
            if ctype != b"IHDR" or length != 13:
                raise InvalidImage
            width, height, bit_depth, color_type, compression, filt, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if (
                width < 1
                or height < 1
                or compression != 0
                or filt != 0
                or interlace not in (0, 1)
                or bit_depth not in _BIT_DEPTHS.get(color_type, frozenset())
            ):
                raise InvalidImage
            # Dimensions first, before anything else is walked or any provider is called.
            if (
                min(width, height) < min_edge
                or max(width, height) > max_long_edge
                or width * height > max_pixels
            ):
                raise InvalidImage
            kept.append(raw)
            continue

        if ctype == b"IHDR":
            raise InvalidImage
        if ctype == b"IEND":
            if length != 0 or not seen_idat:
                raise InvalidImage
            seen_iend = True
            kept.append(raw)
        elif ctype == b"IDAT":
            if idat_finished or (color_type == 3 and not seen_plte):
                raise InvalidImage
            seen_idat = True
            kept.append(raw)
        else:
            if seen_idat:
                idat_finished = True  # any other chunk ends the IDAT run
            if ctype == b"PLTE":
                if (
                    seen_plte
                    or seen_idat
                    or color_type in (0, 4)
                    or length == 0
                    or length % 3 != 0
                    or length > 768
                ):
                    raise InvalidImage
                seen_plte = True
                kept.append(raw)
            elif ctype == b"tRNS":
                if seen_idat or color_type in (4, 6):
                    raise InvalidImage
                kept.append(raw)
            elif ctype[0] & 0x20 == 0:  # an unknown CRITICAL chunk (upper-case first letter)
                raise InvalidImage
            else:
                ancillary += 1
                if ancillary > MAX_ANCILLARY_CHUNKS:
                    raise InvalidImage

    if not seen_iend or not seen_idat:
        raise InvalidImage
    return ValidatedPng(b"".join(kept), width, height)
