#!/usr/bin/env python3
"""Bluetooth on the air: what the controller staged, heard by someone else.

The link layer has scheduled advertising events since ADR 0025 and carried
nothing. What it advertises turned out not to be copied anywhere: the
controller maps the page the host's buffer sits in into exchange memory and
refers to it by offset (ADR 0044). So the packet was always reachable, through
the mapping the controller itself programmed.

This boots one board with a Bluetooth air and listens on it. The assertions are
about what arrives being real advertising data -- structures that describe
their own lengths and types, and the name this firmware advertises under --
because the point is that the payload comes from the controller rather than
from anything this emulator made up.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

BLUETOOTH_FS = REPO_ROOT / "build" / "littlefs-bluetooth.bin"
OVERLAY_AT = 0xC90000
RUN_SECONDS = 360
AD_COMPLETE_LOCAL_NAME = 0x09


@pytest.fixture(scope="module")
def bluetooth_filesystem() -> Path:
    """A filesystem with Bluetooth already switched on.

    The interface enables it when its boot logo is held (ADR 0026); doing that
    once and keeping the result is what makes this affordable to test at all.
    """
    if not BLUETOOTH_FS.exists():
        done = subprocess.run(
            [sys.executable, "tools/make_configured_fs.py", "--bluetooth",
             "-o", str(BLUETOOTH_FS)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=900)
        if done.returncode != 0 or not BLUETOOTH_FS.exists():
            pytest.fail("could not build a Bluetooth filesystem:\n"
                        + done.stdout[-2000:] + done.stderr[-2000:])
    return BLUETOOTH_FS


@pytest.fixture(scope="module")
def overheard(tmp_path_factory, bluetooth_filesystem: Path) -> list[bytes]:
    """Boot one board advertising, and listen until something arrives."""
    work = tmp_path_factory.mktemp("bleair")
    air = work / "air"
    air.mkdir()
    (work / "build").mkdir()

    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--icount",
         "--ble-air", str(air), "--builddir", str(work / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",
         "--outdir", str(work), "--tag", "ble",
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    listener = AirMedium(air, "listener")
    frames: list[bytes] = []
    try:
        deadline = time.monotonic() + RUN_SECONDS
        while time.monotonic() < deadline and len(frames) < 2:
            frames.extend(listener.poll())
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        listener.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    return frames


def structures(frame: bytes) -> list[tuple[int, bytes]]:
    """Advertising data describes itself: each block is a length, a type, and
    that many bytes. Walking it is how a payload is told from a run of bytes."""
    out, at = [], 0
    while at < len(frame):
        length = frame[at]
        if length == 0 or at + 1 + length > len(frame):
            break
        out.append((frame[at + 1], frame[at + 2:at + 1 + length]))
        at += 1 + length
    return out


def test_something_is_heard(overheard) -> None:
    """The link layer scheduled events for a long time before anything carried
    them. If nothing arrives, nothing below means anything."""
    assert overheard, "nothing was heard on the Bluetooth air"


def test_what_arrives_is_advertising_data(overheard) -> None:
    """Every byte accounted for by a structure that declares its own length.
    A frame that merely contained the name could be any accident of memory."""
    blocks = structures(overheard[0])
    assert blocks, f"not advertising data: {overheard[0].hex(' ')}"
    consumed = sum(2 + len(payload) for _, payload in blocks)
    assert consumed == len(overheard[0]), (
        f"{len(overheard[0]) - consumed} stray byte(s): {overheard[0].hex(' ')}")


def test_it_carries_the_name_the_firmware_advertises(overheard) -> None:
    """The one part of the payload that can be recognised from outside."""
    names = [payload for kind, payload in structures(overheard[0])
             if kind == AD_COMPLETE_LOCAL_NAME]
    assert names, f"no name in {overheard[0].hex(' ')}"
    assert names[0].startswith(b"Meshtastic"), names[0]
