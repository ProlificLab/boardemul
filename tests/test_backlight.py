#!/usr/bin/env python3
"""The interface dims its own screen (ADR 0053).

Meshtastic turns the backlight up when it draws, and after half a minute with
nobody touching the device it fades the panel out and blanks it. That fade is
the busiest thing in this window -- a hundred and fifty-odd duty changes in a
single boot -- and until the controller was modelled every one of them landed
in a window that only remembered the last value, so the emulator could not have
told you the screen had gone dark.

Nothing here drives the board. The firmware decides, on its own clock, and the
run records what it decided.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, REPO_ROOT, Run, qemu_available, reset_trouble,
    run_emulator,
)

CONFIGURED = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000
# The interface blanks after thirty seconds of its own time with no input, and
# the guest runs slower than the wall, so this is a ceiling that has to cover
# the boot as well. There is nothing to stop early for: the fade is the last
# thing this test came to see.
RUN_SECONDS = 300

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not CONFIGURED.exists(), reason="no configured filesystem"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Run:
    """The board may reboot from the firmware's ipc1 overflow, as a T-Deck in
    a hand would (ADR 0125). The backlight is a part on the board, not in the
    chip, so its record spans both boots."""
    run = run_emulator(tmp_path_factory.mktemp("backlight"),
                       firmware=FIRMWARE_MESHTASTIC, board="tdeck",
                       timeout=RUN_SECONDS,
                       overlays=(f"0x{OVERLAY_AT:x}={CONFIGURED}",),
                       extra=("--psram-octal", "--reboot"))
    if run.boots > 1:
        warnings.warn(f"Meshtastic booted {run.boots} times (ADR 0125)")
    return run


@pytest.fixture(scope="module")
def booted(ran: Run) -> dict:
    manifest = next(iter(ran.outdir.glob("*.manifest.json")), None)
    assert manifest is not None, "the run left no manifest"
    return json.loads(manifest.read_text()).get("backlight", {})


def test_it_reboots_only_from_the_forgiven_panic(ran: Run) -> None:
    """A reset used to end this run early -- the screen lit, three duty
    changes, no fade -- without a word about why. Only ADR 0099's ipc1 canary
    is forgiven now, and anything else fails with its boot (ADR 0125)."""
    assert not reset_trouble(ran.uart), reset_trouble(ran.uart)


def test_the_firmware_drives_the_backlight(booted) -> None:
    """One channel, driving. Eight exist and a board uses one, so an empty
    report means the window swallowed the writes again."""
    assert booted, "nothing drove the backlight"
    assert len(booted) == 1, f"expected one channel to be driving: {booted}"


def test_it_lights_the_screen(booted) -> None:
    """Whatever else happens, it turns the panel on to draw on it."""
    channel = next(iter(booted.values()))
    assert channel["highest"] > 0.25, channel


def test_it_fades_the_screen_out_when_nobody_is_using_it(booted) -> None:
    """The feature this window was modelled for. Left alone, the interface
    ramps the backlight down to nothing -- and it does it in many small steps,
    which is a fade rather than a switch."""
    channel = next(iter(booted.values()))
    assert channel["lowest"] < 0.01, (
        f"the screen never went dark: {channel}. Either the run ended before "
        f"the interface's own timeout, or the fade was not seen.")
    assert channel["changes"] > 20, (
        f"only {channel['changes']} change(s); a fade is many small steps")
