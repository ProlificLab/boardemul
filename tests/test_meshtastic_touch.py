#!/usr/bin/env python3
"""Meshtastic's setup dialog, driven by touch and the keyboard.

This is the interaction the emulator exists to support: unmodified vendor
firmware, operated the way a person operates the device. It is one boot, and
every assertion reads the panel the firmware drew.

The geometry is measured from a rendered 320x240 dialog and cross-checked
against the device-ui's generated layout. What matters is not the numbers but
the two facts they encode, both of which cost real time to find:

- The keypad binds to a field only when the small keyboard icon beside it is
  clicked (`ui_event_KeyboardButton`). Tapping the field does nothing useful,
  which is the interface's design, not a defect here.
- A tap has to arrive where it was aimed, which needs the controller's own
  raster and the mounting (ADR 0030) and an interrupt that gives edges
  (ADR 0031). With either wrong the firmware reads touch and ignores it, so a
  test that only counted touch reports would have passed throughout.

Controls are identified by counting a region, never by sampling one pixel: the
middle of a button is its label, so the blue OK button reads back white there.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, count_pixels, dark_pixels,
    pixel, qemu_available, run_emulator, send_key, send_touch, wait_for_panel,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

SHORT_ICON = (277, 117)                 # the keyboard icon beside Short Name
SHORT_FIELD = (140, 106, 218, 125)      # its text area, white until typed into
OK_BUTTON = (141, 207)
OK_FACE = (117, 190, 167, 225)
NOTICE = (100, 70, 300, 140)            # where the firmware draws its restart notice

BLUE = lambda r, g, b: b > 150 and r < 110 and g < 140        # noqa: E731
WHITE = lambda r, g, b: r > 230 and g > 230 and b > 230       # noqa: E731
RED = lambda r, g, b: r > 150 and g < 90 and b < 90           # noqa: E731


def _is_setup_dialog(frame: Path) -> bool:
    """The dialog itself, not merely a dark screen: a dark sidebar, a white
    short-name field and a blue OK button. A looser test matches a black frame
    during boot, and then the taps go nowhere."""
    return (all(c < 100 for c in pixel(frame, 2, 2))
            and count_pixels(frame, SHORT_FIELD, WHITE) > 500
            and count_pixels(frame, OK_FACE, BLUE) > 500)


@pytest.fixture(scope="module")
def typed(tmp_path_factory) -> tuple[Run, dict]:
    """Boot, wait for the dialog, bind the keyboard by touch, type, commit."""
    seen: dict = {}

    def drive(outdir: Path) -> None:
        frame = outdir / "frames" / "latest.png"
        seen["dialog"] = wait_for_panel(outdir, _is_setup_dialog, timeout=220)
        if not seen["dialog"]:
            return
        seen["ink_before"] = dark_pixels(frame, SHORT_FIELD)

        # The interface blanks its panel after half a minute of its own time
        # with no input, so the first touch has to be prompt. Every input
        # afterwards resets that.
        send_touch(outdir, *SHORT_ICON)         # binds the keypad to the field
        time.sleep(6)
        for character in "emu1":
            send_key(outdir, character)
            time.sleep(2.5)
        time.sleep(4)
        seen["ink_after"] = dark_pixels(frame, SHORT_FIELD)

        send_touch(outdir, *SHORT_ICON)         # the same icon puts it away
        time.sleep(6)
        # Committing makes the firmware restart itself to apply the settings,
        # and it says so on the panel first. The notice is what to wait for:
        # the run is launched with -no-reboot, so the reset ends the guest and
        # no second banner is ever printed.
        send_touch(outdir, *OK_BUTTON)
        seen["reboot_notice"] = wait_for_panel(
            outdir, lambda f: count_pixels(f, NOTICE, RED) > 200, timeout=30)

    run = run_emulator(tmp_path_factory.mktemp("touch"), firmware=FIRMWARE_MESHTASTIC,
                       board="tdeck", timeout=340, icount=True,
                       overlays=(f"0xc90000={MESHTASTIC_LITTLEFS}",),
                       extra=("--psram-octal",), script=drive)
    return run, seen


def test_the_setup_dialog_is_reached(typed) -> None:
    """Everything after this depends on it, so it fails on its own terms."""
    _, seen = typed
    assert seen.get("dialog"), "the setup dialog never appeared"


def test_a_tap_on_the_icon_binds_the_keyboard_to_the_field(typed) -> None:
    """The typed characters land in the short-name field, which was empty. Ink
    in that box is the proof: the tap bound the keypad, and the keystrokes
    reached the field it bound."""
    _, seen = typed
    assert seen.get("ink_before") == 0, "the field was not empty to begin with"
    assert seen.get("ink_after", 0) > 0, "nothing was typed into the field"


def test_the_ok_button_commits_and_the_firmware_restarts(typed) -> None:
    """A touch on OK saves the settings and the firmware restarts itself to
    apply them, announcing it on the panel. That notice is the whole chain
    working: bound by touch, typed, committed by touch."""
    _, seen = typed
    assert seen.get("reboot_notice"), "OK did not commit: no restart notice drawn"
