#!/usr/bin/env python3
"""A minimal PNG writer, greyscale or truecolour.

Split out of the e-paper model when a second panel arrived that has colour.
zlib and struct are in the allowed dependency set and are all a PNG needs; the
tests carry the matching reader, because the emulator never has cause to read
one back.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

GREY = 0
RGB = 2


def write_png(path: Path, width: int, height: int, pixels: bytes,
              colour: int = GREY) -> None:
    """Write `pixels` as a PNG. One byte per pixel for GREY, three for RGB."""
    data = encode_png(width, height, pixels, colour)
    # Written beside the real name and moved onto it, because the panel rewrites
    # `latest.png` continuously while a test reads it, and a reader that catches
    # a half-written file sees something that is not a PNG at all. A rename is
    # the one operation on a file that cannot be observed half done.
    path = Path(path)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def encode_png(width: int, height: int, pixels: bytes, colour: int = GREY,
               level: int = 9) -> bytes:
    """`pixels` as the bytes of a PNG file, for a file or for a Tk photo."""
    stride = width * (3 if colour == RGB else 1)
    expected = stride * height
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} bytes, got {len(pixels)}")

    # Filter type 0 on every row: the images are small and already compressed
    # well by zlib, so choosing filters per row would buy little for the code.
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride]
                   for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level))
            + chunk(b"IEND", b""))
