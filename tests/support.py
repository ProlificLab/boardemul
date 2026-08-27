#!/usr/bin/env python3
"""Helpers shared by the tests.

Includes a minimal PNG reader, which the emulator itself has no need for -- it
only writes them -- but a test that asserts on what was drawn does. It also
holds the runner the slow tests drive the real emulator with, so that adding a
board means adding a test file rather than copying a subprocess invocation.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck-MAX_V3.1_20260313.bin"
FIRMWARE_TDECK = REPO_ROOT / "firmware" / "T-Deck_UnitTest_251113.bin"


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


# -- driving the real emulator ----------------------------------------------


# The emulator's --timeout bounds guest time; the process then still has to stop
# QEMU, drain the bus server and write its report. Waiting exactly --timeout for
# it to exit races that shutdown, and only firmware that runs to the timeout --
# rather than stalling early -- ever loses the race.
SHUTDOWN_GRACE = 30


def qemu_available() -> bool:
    local = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
    return bool(os.environ.get("BOARDEMUL_QEMU")
                or local.is_file()
                or shutil.which("qemu-system-xtensa"))


@dataclass
class Run:
    outdir: Path
    frames: list[Path] = field(default_factory=list)
    uart: str = ""

    def digests(self) -> list[str]:
        return [region_digest(f) for f in self.frames]


def _frames(outdir: Path) -> list[Path]:
    return sorted((outdir / "frames").glob("frame-*.png"))


def run_emulator(outdir: Path, *, firmware: Path = FIRMWARE,
                 board: str = "tdeck-max", tap: tuple[int, int] | None = None,
                 wait_frames: int = 0, timeout: int = 150,
                 settle: int = 25) -> Run:
    """Boot a firmware, optionally tapping the panel once it has drawn.

    A board with no panel model draws nothing, so waiting on frames is opt-in:
    wait_frames=0 means run to the timeout and report what the UART said.
    """
    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(firmware), "--bus", "--board", board,
         "--tag", "t", "--outdir", str(outdir), "--timeout", str(timeout),
         "--stall", str(timeout)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + timeout
        while wait_frames and time.monotonic() < deadline:
            if len(_frames(outdir)) >= wait_frames:
                break
            if proc.poll() is not None:
                break
            time.sleep(1)

        if tap is not None:
            events = outdir / "input.jsonl"
            with events.open("a") as fh:
                fh.write(json.dumps({"type": "touch", "x": tap[0],
                                     "y": tap[1], "down": True}) + "\n")
                fh.flush()
                time.sleep(0.8)
                fh.write(json.dumps({"type": "touch", "down": False}) + "\n")
                fh.flush()
            # A refresh takes a couple of seconds of emulated time; give the
            # firmware room to redraw before looking.
            time.sleep(settle)
        proc.wait(timeout=timeout + SHUTDOWN_GRACE)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    uart = ""
    for log in outdir.glob("*.uart.log"):
        uart = log.read_text(errors="replace")
    return Run(outdir=outdir, frames=_frames(outdir), uart=uart)
