#!/usr/bin/env python3
"""Helpers shared by the tests.

Includes a minimal PNG reader, which the emulator itself has no need for -- it
only writes them -- but a test that asserts on what was drawn does.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck-MAX_V3.1_20260313.bin"


def read_png(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit greyscale PNG to (width, height, pixels).

    Only the shape our own writer produces: one IDAT, no interlace, no palette.
    Anything else is a bug in the writer, so it raises rather than coping.
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")

    pos = 8
    width = height = 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if (depth, colour) != (8, 0):
                raise ValueError(f"expected 8-bit greyscale, got {depth}/{colour}")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break

    raw = zlib.decompress(idat)
    out = bytearray(width * height)
    prev = bytearray(width)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + width])
        pos += width
        if filter_type == 1:            # Sub
            for x in range(1, width):
                line[x] = (line[x] + line[x - 1]) & 0xFF
        elif filter_type == 2:          # Up
            for x in range(width):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif filter_type == 3:          # Average
            for x in range(width):
                left = line[x - 1] if x else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif filter_type == 4:          # Paeth
            for x in range(width):
                a = line[x - 1] if x else 0
                b = prev[x]
                c = prev[x - 1] if x else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG filter {filter_type}")
        out[y * width:(y + 1) * width] = line
        prev = line
    return width, height, bytes(out)


def ink_below_status_bar(path: Path, status_rows: int = 30) -> int:
    """Count dark pixels, ignoring the status bar.

    The clock and charge indicator change between refreshes, so a whole-frame
    hash is not a stable assertion. What sits below it is.
    """
    width, height, pixels = read_png(path)
    body = pixels[status_rows * width:]
    return sum(1 for p in body if p < 0x80)


def region_digest(path: Path, status_rows: int = 30) -> str:
    import hashlib

    width, height, pixels = read_png(path)
    return hashlib.sha256(pixels[status_rows * width:]).hexdigest()[:16]
