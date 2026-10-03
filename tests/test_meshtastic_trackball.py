#!/usr/bin/env python3
"""Meshtastic navigates by the T-Deck's trackball (ADR 0128).

Its own trackball code counts falling edges on the direction lines and starts
a click on the boot pin's, and both are what the board now drives. A notch of
roll has to move the interface's focus, and a click has to open what is
focused, from the home screen of a configured node.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, REPO_ROOT, Run, pixel, qemu_available, read_png,
    reset_trouble, run_emulator, wait_for_panel,
)

CONFIGURED = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not CONFIGURED.exists(), reason="no configured filesystem"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

SIDEBAR = (0, 0, 40, 240)               # the column of panel buttons, and the focus
MAIN = (45, 30, 320, 240)               # what the focused button opens


def _is_home(frame: Path) -> bool:
    """A lit header over the grey sidebar -- not the green boot logo, which a
    looser test takes for a screen and then sends input into."""
    header = sum(pixel(frame, 160, 8))
    r, g, b = pixel(frame, 5, 230)
    return header > 120 and max(r, g, b) - min(r, g, b) < 20 and max(r, g, b) < 150


def _crop(frame: Path, box: tuple[int, int, int, int]) -> bytes:
    width, _, pixels = read_png(frame)
    x0, y0, x1, y1 = box
    return b"".join(pixels[3 * (y * width + x0):3 * (y * width + x1)] for y in range(y0, y1))


def _changed(a: bytes, b: bytes) -> float:
    """The share of pixels that differ between two crops."""
    n = len(a) // 3
    return sum(a[3 * i:3 * i + 3] != b[3 * i:3 * i + 3] for i in range(n)) / n


def _send(outdir: Path, event: dict) -> None:
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps(event) + "\n")


@pytest.fixture(scope="module")
def steered(tmp_path_factory) -> tuple[Run, dict]:
    seen: dict = {}

    def drive(outdir: Path) -> None:
        frame = outdir / "frames" / "latest.png"
        seen["home"] = wait_for_panel(outdir, _is_home, timeout=480)
        if not seen["home"]:
            return
        time.sleep(3)
        sidebar, main = _crop(frame, SIDEBAR), _crop(frame, MAIN)

        _send(outdir, {"type": "trackball", "roll": "down", "steps": 6})
        seen["moved"] = wait_for_panel(
            outdir, lambda f: _crop(f, SIDEBAR) != sidebar, timeout=90)
        if not seen["moved"]:
            return

        _send(outdir, {"type": "trackball", "press": True})
        time.sleep(0.3)
        _send(outdir, {"type": "trackball", "press": False})
        # The home card's clock ticks every second, so "changed at all" would
        # pass on its own; a new panel changes most of the area.
        seen["opened"] = wait_for_panel(
            outdir, lambda f: _changed(_crop(f, MAIN), main) > 0.3, timeout=90)

    run = run_emulator(tmp_path_factory.mktemp("trackball"),
                       firmware=FIRMWARE_MESHTASTIC, board="tdeck", timeout=600,
                       script=drive, overlays=(f"0x{OVERLAY_AT:x}={CONFIGURED}",),
                       extra=("--psram-octal", "--reboot"))
    if run.boots > 1:
        warnings.warn(f"Meshtastic booted {run.boots} times (ADR 0125)")
    return run, seen


def test_it_reboots_only_from_the_forgiven_panic(steered) -> None:
    run, _seen = steered
    assert not reset_trouble(run.uart), reset_trouble(run.uart)


def test_the_home_screen_came_up(steered) -> None:
    _run, seen = steered
    assert seen.get("home"), "never reached the home screen"


def test_a_notch_of_roll_moves_the_focus(steered) -> None:
    """Three falling edges inside a second: Meshtastic's T-Deck threshold."""
    _run, seen = steered
    assert seen.get("moved"), "the sidebar did not change after a notch down"


def test_a_click_opens_what_is_focused(steered) -> None:
    _run, seen = steered
    assert seen.get("opened"), "a click on the trackball opened nothing"
