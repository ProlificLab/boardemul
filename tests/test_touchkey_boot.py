#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A glass touch key takes the vendor's interface back a screen.

Slow: boots the Max's factory firmware under QEMU, taps an icon to open a
sub-screen, presses one of the three touch keys printed under the glass, and
waits for the launcher to come back -- the firmware's own
factory_touch_key_event calling scr_mgr_pop (ADR 0131). Skips without a built
QEMU or the firmware, like every boot test.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE, Run, qemu_available, region_digest, run_emulator, send_touch,
    send_touchkey, wait_for_panel,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

# The middle icon of the launcher's top row, on a 240x320 panel (test_boot).
SETTING_ICON = (120, 75)
# The middle touch key, the speech bubble; the firmware treats all three alike.
BUBBLE = 1


@pytest.fixture(scope="module")
def pressed(tmp_path_factory) -> tuple[Run, dict[str, str]]:
    """Boot, open a sub-screen, press the key, and come back.

    The digests the interaction saw are handed back beside the run: the
    launcher before the tap, the sub-screen after it, and whatever the panel
    showed after the key.
    """
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    seen: dict[str, str] = {}

    def interaction(outdir: Path) -> None:
        latest = outdir / "frames" / "latest.png"
        # The launcher is the third picture: clear, splash, menu (test_boot).
        # Waiting for any frame grabs the splash, and a tap into the splash
        # is swallowed before the menu ever draws.
        def launcher_drawn(_p) -> bool:
            return len(list(outdir.glob("frames/frame-*.png"))) >= 3
        if not wait_for_panel(outdir, launcher_drawn, 300):
            return                      # no launcher; the assertions say so
        time.sleep(5)                   # let the menu's refresh finish
        seen["launcher"] = region_digest(latest)
        send_touch(outdir, *SETTING_ICON)
        if wait_for_panel(outdir, lambda p: region_digest(p) != seen["launcher"], 90):
            # The first differing frame is the icon's pressed state; the
            # sub-screen itself follows a full e-paper refresh later. Settle
            # before judging what is open, or the key pops the wrong moment.
            time.sleep(25)
            seen["sub"] = region_digest(latest)
        send_touchkey(outdir, BUBBLE)
        if wait_for_panel(outdir, lambda p: region_digest(p) == seen["launcher"], 90):
            seen["back"] = region_digest(latest)

    run = run_emulator(tmp_path_factory.mktemp("touchkey"), script=interaction,
                      timeout=480)
    return run, seen


def test_the_tap_opened_a_sub_screen(pressed: tuple[Run, dict[str, str]]) -> None:
    """The launcher drew, and the icon's tap navigated away from it."""
    run, seen = pressed
    assert len(run.frames) >= 3, "the launcher never drew"
    assert "sub" in seen, "tapping the icon changed nothing"
    assert seen["sub"] != seen["launcher"]


def test_the_touch_key_returns_to_the_launcher(pressed: tuple[Run, dict[str, str]]) -> None:
    """scr_mgr_pop, as the vendor's own callback runs it: the panel is back
    on the screen the tap left."""
    _run, seen = pressed
    assert "back" in seen, "pressing the touch key changed nothing"
    assert seen["back"] == seen["launcher"]
