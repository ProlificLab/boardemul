#!/usr/bin/env python3
"""Produce a Meshtastic filesystem that already has a region set.

A node with no region never transmits, so two of them have nothing to say to
each other. The setup dialog is how a person gives it one, and this does that:
boot once, drive the dialog, let the firmware save and restart, then take the
filesystem partition back out of the flash it was saved to.

The result is a derived artefact and lives in `build/`, not `firmware/`, which
holds vendor images pinned by hash. It is rebuilt by running this again; there
is no state here that cannot be regenerated from the vendor image plus the
steps below, which is the point -- a demonstration that only works because of
a file nobody can reproduce is not a demonstration.

    python3 tools/make_configured_fs.py [-o build/littlefs-configured.bin]

The geometry of the dialog is the same as the touch test's and for the same
reasons; see tests/test_meshtastic_touch.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, count_pixels, pixel,
)
from tools.mkflash import find_partition_table  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000

SHORT_ICON = (277, 117)
SHORT_FIELD = (140, 106, 218, 125)
OK_BUTTON = (141, 207)
OK_FACE = (117, 190, 167, 225)
NOTICE = (100, 70, 300, 140)

BLUE = lambda r, g, b: b > 150 and r < 110 and g < 140        # noqa: E731
WHITE = lambda r, g, b: r > 230 and g > 230 and b > 230       # noqa: E731
RED = lambda r, g, b: r > 150 and g < 90 and b < 90           # noqa: E731


def is_setup_dialog(frame: Path) -> bool:
    return (all(c < 100 for c in pixel(frame, 2, 2))
            and count_pixels(frame, SHORT_FIELD, WHITE) > 500
            and count_pixels(frame, OK_FACE, BLUE) > 500)


def wait_for(frames: Path, predicate, timeout: float) -> bool:
    latest = frames / "latest.png"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if latest.exists():
            try:
                if predicate(latest):
                    return True
            except Exception:
                pass            # caught mid-write; look again
        time.sleep(1.0)
    return False


def send(events: Path, event: dict) -> None:
    with events.open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def tap(events: Path, where: tuple[int, int], hold: float = 1.2) -> None:
    send(events, {"type": "touch", "x": where[0], "y": where[1], "down": True})
    time.sleep(hold)
    send(events, {"type": "touch", "down": False})


def carve_filesystem(flash: Path, out: Path) -> int:
    """Take the filesystem partition out of a flash image, by the table in it
    rather than by a constant, so a firmware that moves it still works."""
    data = flash.read_bytes()
    _, parts = find_partition_table(data)
    for part in parts:
        if part.label in ("spiffs", "littlefs", "storage"):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data[part.offset:part.offset + part.size])
            return part.size
    raise SystemExit(f"no filesystem partition in {flash}: "
                     f"found {[p.label for p in parts]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--name", default="emu1", help="short name to set")
    ap.add_argument("--timeout", type=float, default=420.0)
    args = ap.parse_args(argv)

    for needed in (FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS):
        if not needed.exists():
            raise SystemExit(f"missing {needed}; see firmware/MANIFEST.toml")

    work = Path(tempfile.mkdtemp(prefix="configured-"))
    build = work / "build"
    frames = work / "frames"
    events = work / "input.jsonl"
    print(f"working in {work}", flush=True)

    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal",
         "--builddir", str(build), "--outdir", str(work), "--tag", "cfg",
         "--overlay", f"0x{OVERLAY_AT:x}={MESHTASTIC_LITTLEFS}",
         "--timeout", str(args.timeout), "--stall", str(args.timeout)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_for(frames, is_setup_dialog, timeout=args.timeout * 0.6):
            raise SystemExit("the setup dialog never appeared")
        print("dialog is up; setting the name and accepting the region", flush=True)

        tap(events, SHORT_ICON)              # binds the keypad to the field
        time.sleep(6)
        for character in args.name:
            send(events, {"type": "key", "key": character, "down": True})
            time.sleep(2.5)
        time.sleep(4)
        tap(events, SHORT_ICON)              # put the keyboard away
        time.sleep(6)
        tap(events, OK_BUTTON)               # commits; the firmware restarts

        if not wait_for(frames, lambda f: count_pixels(f, NOTICE, RED) > 200, 60):
            raise SystemExit("OK did not commit: no restart notice drawn")
        print("saved; letting the firmware write it out", flush=True)
        time.sleep(25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    flash = build / FIRMWARE_MESHTASTIC.stem / "flash.bin"
    if not flash.exists():
        raise SystemExit(f"no flash image at {flash}")
    size = carve_filesystem(flash, args.out)
    print(f"wrote {args.out} ({size} bytes)")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
