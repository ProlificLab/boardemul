#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Meshtastic's WiFi set on the device's own screen, as a person sets it.

The node's region is set through its serial API first, which takes it past
its setup dialog to its home screen (the dialog is `test_meshtastic_touch`'s
to drive). From there: the settings gear, the WiFi row, the network's name
and key typed into the dialog's two fields with the keyboard each one's icon
binds, and OK. The node says it took the network, restarts on it, and its
next boot starts the WiFi side: it checks for the TLS certificate it keeps
only when WiFi is on. Joining is left out: making that certificate takes
from one to fifteen minutes of the node's time on its first WiFi boot, and
the join itself is measured in ADR 0164.

Until ADR 0172 the interface kept asking its node for the configuration
("Resync") and a WiFi setting entered here never reached flash.

    python3 -m pytest tests/test_meshtastic_wifi_screen.py -m slow
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, count_pixels, qemu_available,
    run_emulator, send_text, send_touch, send_trackball_press, unforgiven,
    wait_for_panel,
)
from tests.setup_dialog import BLUE, WHITE, blank as _blank  # noqa: E402
from tools import mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

SSID, KEY = "meshlab", "labpass123"
# Measured off the 320x240 panel, Meshtastic 2.7.10's colour interface.
GEAR = (18, 215)                    # the side bar's settings button
WIFI_ROW = (175, 208)               # "WiFi: <not set>" in Settings & Tools
SSID_ICON = (275, 75)               # the keyboard icons beside the two fields
KEY_ICON = (275, 131)
DIALOG_OK = (140, 175)
SSID_FIELD = (66, 64, 258, 88)      # white until typed into
DIALOG_OK_FACE = (116, 160, 164, 190)
TOP, BOTTOM = (0, 0, 320, 112), (0, 130, 320, 240)     # where a keyboard can be
# The side bar lights the screen it is on in green: the home button at its
# top, the settings gear at its foot. Counted, not compared: the boot splash
# floods the gear's corner with the same green, and a screen's title bar
# changes on its own (its battery icon).
HOME_BUTTON, GEAR_BUTTON = (0, 0, 42, 40), (0, 198, 42, 240)
GREEN = lambda r, g, b: g > 170 and r < 140 and b < 170 and g - r > 60  # noqa: E731


def _home(frame: Path) -> bool:
    return (count_pixels(frame, HOME_BUTTON, GREEN) > 60
            and count_pixels(frame, GEAR_BUTTON, GREEN) < 50)


def _settings(frame: Path) -> bool:
    gear = count_pixels(frame, GEAR_BUTTON, GREEN)
    return 200 < gear < 800 and count_pixels(frame, HOME_BUTTON, GREEN) < 20


def _tap_until(outdir: Path, point: tuple[int, int], done, tries: int = 8,
               wait_s: float = 12) -> bool:
    """Tap until the screen shows what the tap was for. A tap that lands while
    the chip light-sleeps is lost (ADR 0165), on the board as here, and a
    person taps again. Held as long as a finger rests, a second: a run lost
    four 0.4 s taps in a row while awake, most likely pressed and released
    between two of the interface's reads of its panel.

    A dark screen is the interface in powersave, which reads only its button:
    it is woken with the trackball's click first, and never taken for the
    screen a tap was waiting for (a dark frame's title differs from any)."""
    frame = outdir / "frames" / "latest.png"
    for attempt in range(tries):
        if frame.exists() and _blank(frame):
            send_trackball_press(outdir)
            wait_for_panel(outdir, lambda f: not _blank(f), timeout=10)
        # A finger resting, then a quick one: which the interface misses
        # varies with what it is doing.
        send_touch(outdir, *point, hold=1.2 if attempt % 2 == 0 else 0.3)
        if wait_for_panel(outdir, lambda f: not _blank(f) and done(f), timeout=wait_s):
            return True
    return False


def _boots(outdir: Path) -> int:
    log = outdir / "t.otg.log"
    return log.read_bytes().count(b"Booted") if log.exists() else 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def entered(tmp_path_factory) -> tuple[Run, dict]:
    seen: dict = {}
    port = _free_port()

    def drive(outdir: Path) -> None:
        frame = outdir / "frames" / "latest.png"
        log = outdir / "t.otg.log"
        deadline = time.monotonic() + 500
        while _boots(outdir) < 1 and time.monotonic() < deadline:
            time.sleep(2)
        # The region, through the node's own API; it saves it and restarts.
        while "region_set" not in seen and time.monotonic() < deadline:
            time.sleep(10)
            try:
                node = mesh_api.Node(f"127.0.0.1:{port}", timeout=120)
                state = node.configuration()
                if "my_node_num" in state:
                    node.send(mesh_api.set_region(state["my_node_num"], mesh_api.REGIONS["us"],
                                                  state.get("raw_lora", b"")))
                    seen["region_set"] = True
                node.sock.close()
            except OSError:
                pass
        while _boots(outdir) < 2 and time.monotonic() < deadline:
            time.sleep(2)
        # Its interface has its configuration again: the home screen is up.
        while (log.read_bytes().split(b"Booted")[-1].count(b"Config Send Complete") < 1
               and time.monotonic() < deadline):
            time.sleep(2)
        seen["home"] = (_boots(outdir) >= 2
                        and wait_for_panel(outdir, _home, timeout=max(1, deadline - time.monotonic())))

        seen["settings"] = _tap_until(outdir, GEAR, _settings)
        if not seen["settings"]:
            return
        dialog = lambda f: count_pixels(f, SSID_FIELD, WHITE) > 3000  # noqa: E731
        seen["dialog"] = _tap_until(outdir, WIFI_ROW, dialog)
        if not seen["dialog"]:
            return
        # The keyboard comes up under the name field and over the key field,
        # where it leaves OK showing, so it is told by its white keycaps.
        keyboard_up = lambda f: (count_pixels(f, BOTTOM, WHITE) > 8000  # noqa: E731
                                 or count_pixels(f, TOP, WHITE) > 14000)
        keyboard_down = lambda f: (count_pixels(f, BOTTOM, WHITE) < 5000  # noqa: E731
                                   and count_pixels(f, TOP, WHITE) < 6000
                                   and count_pixels(f, DIALOG_OK_FACE, BLUE) > 500)
        for name, icon, text in (("ssid", SSID_ICON, SSID), ("key", KEY_ICON, KEY)):
            if not _tap_until(outdir, icon, keyboard_up):
                seen[f"{name}_keyboard"] = False
                return
            for character in text:
                send_text(outdir, character)
                time.sleep(2.5)
            seen[f"{name}_typed"] = _tap_until(outdir, icon, keyboard_down)
        # OK, until the node says it took the network.
        for _ in range(4):
            if frame.exists() and _blank(frame):
                send_trackball_press(outdir)        # powersave reads only this
                wait_for_panel(outdir, keyboard_down, timeout=10)
            send_touch(outdir, *DIALOG_OK)
            end = time.monotonic() + 30
            while time.monotonic() < end and b"Set config: WiFi" not in log.read_bytes():
                time.sleep(1)
            if b"Set config: WiFi" in log.read_bytes():
                seen["committed"] = True
                break
        if not seen.get("committed"):
            return
        # It restarts on it, and the boot after starts WiFi's side.
        end = time.monotonic() + 300
        while time.monotonic() < end:
            text = log.read_bytes()
            after = text.split(b"Set config: WiFi", 1)[-1]
            if b"Checking if we have a saved SSL Certificate" in after:
                seen["wifi_on_boot"] = True
                break
            time.sleep(2)
        uart = next(outdir.glob("*.uart.log"), None)
        seen["uart"] = uart.read_text(errors="replace") if uart else ""

    run = run_emulator(tmp_path_factory.mktemp("wifiscreen"), firmware=FIRMWARE_MESHTASTIC,
                       board="tdeck", timeout=1500,
                       overlays=(f"0xc90000={MESHTASTIC_LITTLEFS}",),
                       extra=("--psram-octal", "--reboot", "--serial-tcp", f"{port}:otg"),
                       script=drive)
    return run, seen


def test_the_settings_and_the_wifi_dialog_open_by_touch(entered) -> None:
    _, seen = entered
    assert seen.get("region_set") and seen.get("home"), f"no home screen: {seen}"
    assert seen.get("settings"), f"the gear opened nothing: {seen}"
    assert seen.get("dialog"), f"the WiFi row opened no dialog: {seen}"


def test_the_network_is_typed_into_the_dialog(entered) -> None:
    _, seen = entered
    assert seen.get("ssid_typed") and seen.get("key_typed"), seen


def test_ok_saves_it_and_the_node_restarts_with_wifi_on(entered) -> None:
    _, seen = entered
    assert seen.get("committed"), f"OK did not save the network: {seen}"
    assert seen.get("wifi_on_boot"), f"the next boot did not start WiFi: {seen}"


def test_no_boot_panics_but_the_forgiven_one(entered) -> None:
    """The region and the WiFi setting each restart the node on purpose; a
    boot may otherwise end only in ADR 0099's ipc1 canary."""
    _, seen = entered
    assert not unforgiven(seen.get("uart", "")), unforgiven(seen.get("uart", ""))
