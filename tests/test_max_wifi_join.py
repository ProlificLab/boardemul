#!/usr/bin/env python3
"""The Max joins a WPA2 network from its own screen (ADRs 0152, 0177).

Slow: boots the factory firmware with a protected access point on the air
and does what a person does: the WiFi icon, "WIFI Scan", Scan, the network's
row, the password typed on the keyboard, Connect. V3.1's password field types
keypad digits, two per key (ADR 0152), so the network's passphrase is the
digits `aaaa` types. Asserted from the access point's side: the station
authenticated, completed WPA2's four-way handshake and took an address.
Until ADR 0177 every frame arrived at 0 dBm and the station, which picks its
access point by signal, never picked one. Skips without a built QEMU or the
firmware.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE, qemu_available, read_png, run_emulator, send_touch, usb_text,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

AP_NAME = "BoardTest"
KEYS = "aaaa"
PASSPHRASE = "22222222"         # what KEYS types into V3.1's field
WIFI_ICON = (57, 152)
WIFI_SCAN_ENTRY = (120, 133)
SCAN_BUTTON = (35, 91)
NETWORK_ROW = (120, 133)        # the first result, under the Scan button
FIRST_ROW = (20, 120, 220, 146)
CONNECT = (70, 137)             # on the password screen


def frames(outdir: Path) -> list[Path]:
    return sorted((outdir / "frames").glob("frame-*.png"))


def settled(outdir: Path, since: int, timeout: float, quiet: float = 10.0) -> int:
    """How many frames there are once the panel has changed and rested: an
    e-paper redraw comes in more than one pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(frames(outdir)) <= since:
        time.sleep(1)
    last, still = len(frames(outdir)), time.monotonic()
    while time.monotonic() - still < quiet and time.monotonic() < deadline:
        time.sleep(1)
        if len(frames(outdir)) != last:
            last, still = len(frames(outdir)), time.monotonic()
    return last


def dark_pixels(frame: Path, box: tuple[int, int, int, int]) -> int:
    width, height, pixels = read_png(frame)
    step = len(pixels) // (width * height)
    x0, y0, x1, y1 = box
    return sum(1 for y in range(y0, y1) for x in range(x0, x1)
               if pixels[(y * width + x) * step] < 128)


def key(outdir: Path, name: str, hold: float = 0.15) -> None:
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "key", "key": name, "down": True}) + chr(10))
    time.sleep(hold)
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "key", "key": name, "down": False}) + chr(10))


def bus_log(outdir: Path) -> str:
    path = outdir / "t.bus.log"
    return path.read_text(errors="replace") if path.exists() else ""


@pytest.fixture(scope="module")
def joined(tmp_path_factory) -> dict:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    outdir = tmp_path_factory.mktemp("maxjoin")
    seen: dict = {}

    def drive(out: Path) -> None:
        n = settled(out, 2, 240.0, quiet=5.0)       # the launcher
        send_touch(out, *WIFI_ICON, hold=0.8)
        n = settled(out, n, 90.0)
        send_touch(out, *WIFI_SCAN_ENTRY, hold=0.8)
        n = settled(out, n, 90.0)
        send_touch(out, *SCAN_BUTTON, hold=0.8)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if frames(out) and dark_pixels(frames(out)[-1], FIRST_ROW) >= 50:
                break
            time.sleep(2)
        seen["listed"] = bool(frames(out)) and dark_pixels(frames(out)[-1], FIRST_ROW) >= 50
        n = settled(out, len(frames(out)) - 1, 30.0)
        send_touch(out, *NETWORK_ROW, hold=0.8)
        n = settled(out, n, 90.0)
        for k in KEYS:
            key(out, k)
            time.sleep(2.5)
        settled(out, n, 30.0, quiet=8.0)
        send_touch(out, *CONNECT, hold=0.8)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline and "dhcp ACK" not in bus_log(out):
            time.sleep(2)

    run_emulator(outdir, board="tdeck-max", wait_frames=3, timeout=900,
                 extra=("--wifi-ap", f"{AP_NAME},100,1", "--wifi-wpa2",
                        "--wifi-pass", PASSPHRASE),
                 script=drive)
    seen["bus"] = bus_log(outdir)
    seen["usb"] = usb_text(outdir)
    return seen


def test_the_scan_lists_the_network(joined) -> None:
    assert joined["listed"], "the scan listed nothing under the Scan button"


def test_the_station_completes_the_handshake(joined) -> None:
    bus = joined["bus"]
    assert "authenticated" in bus, "the station never authenticated"
    assert "completed the four-way handshake" in bus, (
        "the station authenticated but the WPA2 handshake did not complete")


def test_the_station_takes_an_address(joined) -> None:
    assert "dhcp ACK to" in joined["bus"], "no DHCP lease after the handshake"


def test_it_found_its_network_rather_than_none(joined) -> None:
    assert "NO_AP_FOUND" not in joined["usb"], (
        "the station reported its network not found, as it did when every "
        "frame arrived at 0 dBm (ADR 0177)")
