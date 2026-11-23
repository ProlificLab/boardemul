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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.setup_dialog import FIELD_PIXELS, fill_in  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, qemu_available, reset_trouble,
    run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


@pytest.fixture(scope="module")
def typed(tmp_path_factory) -> tuple[Run, dict]:
    """Boot, wait for the dialog, bind the keyboard by touch, type, commit."""
    seen: dict = {}

    def drive(outdir: Path) -> None:
        fill_in(outdir, "emu1", seen)

    # A ceiling, not a plan: the script stops the run as soon as it has what it
    # came for. It has to cover every wait of fill_in end to end, and under a
    # loaded suite each of them takes far longer in wall time than in the
    # guest's (ADR 0040).
    #
    # With --reboot, as the other Meshtastic boots run (ADRs 0114, 0124): a run
    # here once ended at eight guest seconds on ADR 0099's ipc1 canary, which
    # a board survives by restarting, and the dialog comes back after it.
    run = run_emulator(tmp_path_factory.mktemp("touch"), firmware=FIRMWARE_MESHTASTIC,
                       board="tdeck", timeout=760,
                       overlays=(f"0xc90000={MESHTASTIC_LITTLEFS}",),
                       extra=("--psram-octal", "--reboot"), script=drive)
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
    # A blanked screen darkens every pixel of the field, so counting ink alone
    # cannot tell typed text from the interface having gone to sleep.
    assert seen["ink_after"] < FIELD_PIXELS * 0.6, (
        f"the whole field is dark, which is a blanked screen and not typing: {seen}")


def test_the_ok_button_commits_and_the_firmware_restarts(typed) -> None:
    """A touch on OK saves the settings and the firmware restarts itself to
    apply them, announcing it on the panel. That notice is the whole chain
    working: bound by touch, typed, committed by touch."""
    _, seen = typed
    assert seen.get("dismissed"), (
        f"the keyboard never went away, so OK was never reachable: {seen}")
    assert seen.get("reboot_notice"), f"OK did not commit: no restart notice drawn: {seen}"


def test_it_restarts_only_from_the_forgiven_panic(typed) -> None:
    """Before the commit, a boot may end only in ADR 0099's ipc1 canary."""
    _, seen = typed
    text = seen.get("uart_before_commit", "")
    assert not reset_trouble(text), reset_trouble(text)
