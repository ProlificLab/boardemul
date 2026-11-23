#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Meshtastic's setup dialog on the T-Deck, filled in as a person does: the
short-name field's keyboard icon, the name typed, the keyboard put away, OK.

Shared by the tests that need a configured node out of a fresh one
(`test_meshtastic_touch`, `test_device_boot`). The interface loses a tap now
and then -- while it reads its configuration stream, or when the chip is in
light sleep (ADRs 0165, 0172) -- and blanks after half a minute without
input, so every step waits for what the panel shows and taps again while it
does not show it yet, as a person would.

The geometry is measured from a rendered 320x240 dialog and cross-checked
against the device-ui's generated layout. Controls are identified by counting
a region, never by sampling one pixel: the middle of a button is its label,
so the blue OK button reads back white there.
"""

from __future__ import annotations

import time
from pathlib import Path

from tests.support import (
    count_pixels, dark_pixels, pixel, send_text, send_touch, send_trackball_press,
    wait_for_panel,
)

SHORT_ICON = (277, 117)                 # the keyboard icon beside Short Name
SHORT_FIELD = (140, 106, 218, 125)      # its text area, white until typed into
OK_BUTTON = (141, 207)
OK_FACE = (117, 190, 167, 225)
NOTICE = (100, 70, 300, 140)            # where the firmware draws its restart notice
# Tapping the body of a field binds nothing (ADR 0030), so it is somewhere to
# touch that resets the interface's inactivity timer and changes nothing else.
FIELD_BODY = (179, 115)
# Every pixel of the short-name field. Typed text darkens some of them; a
# blanked screen darkens all of them, and counting ink alone cannot tell those
# apart -- which is how a run that had gone dark once read as having typed.
FIELD_PIXELS = (SHORT_FIELD[2] - SHORT_FIELD[0]) * (SHORT_FIELD[3] - SHORT_FIELD[1])

BLUE = lambda r, g, b: b > 150 and r < 110 and g < 140        # noqa: E731
WHITE = lambda r, g, b: r > 230 and g > 230 and b > 230       # noqa: E731
RED = lambda r, g, b: r > 150 and g < 90 and b < 90           # noqa: E731


def typed_into(frame: Path) -> bool:
    """Ink in the field, and the field not simply dark all over."""
    ink = dark_pixels(frame, SHORT_FIELD)
    return 0 < ink < FIELD_PIXELS * 0.6


def hold(attempt: int) -> float:
    """How long a tap rests: a finger's second, then a quick one, in turn.
    Runs of taps went unread while the interface was awake -- four in a row
    in the WiFi settings once (test_meshtastic_wifi_screen) -- and which
    length it missed varied, so a retry tries the other."""
    return 1.2 if attempt % 2 == 0 else 0.3


def blank(frame: Path) -> bool:
    """The interface in powersave: the white short-name field gone dark."""
    return dark_pixels(frame, SHORT_FIELD) >= FIELD_PIXELS * 0.95


def wait_awake(outdir: Path, predicate, timeout: float) -> bool:
    """Wait for the panel, keeping the interface awake while waiting.

    It blanks after thirty seconds of its own time with no input, and these
    waits are long: the run that found this had typed successfully and then
    watched the screen go dark while it waited for the keyboard to be put
    away. Touching a field body between looks resets that timer and presses
    nothing. Once it has gone to powersave it reads only its button ("disable
    touch, enable button input"), the trackball's click, so that wakes it.
    """
    deadline = time.monotonic() + timeout
    frame = outdir / "frames" / "latest.png"
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        if wait_for_panel(outdir, predicate, timeout=min(10.0, left)):
            return True
        if frame.exists() and blank(frame):
            send_trackball_press(outdir)
        else:
            send_touch(outdir, *FIELD_BODY, hold=0.2)


def configured(outdir: Path, timeout: float) -> bool:
    """Whether the interface has its node's configuration.

    It draws the dialog first and asks for the configuration after, and while
    the stream comes in it reads its touch panel only between packets: a tap
    then can come and go unread. The stream takes the board two seconds and
    here several (ADR 0172), which is how a run lost its first tap, waited
    twenty seconds for a keyboard and blanked before it typed. A person sees
    the same: a dialog that does not answer at first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = next(outdir.glob("*.otg.log"), None)
        if log is not None and b"Config Send Complete" in log.read_bytes():
            return True
        time.sleep(1)
    return False


def is_setup_dialog(frame: Path) -> bool:
    """The dialog itself, not merely a dark screen: a dark sidebar, a white
    short-name field and a blue OK button. A looser test matches a black frame
    during boot, and then the taps go nowhere."""
    return (all(c < 100 for c in pixel(frame, 2, 2))
            and count_pixels(frame, SHORT_FIELD, WHITE) > 500
            and count_pixels(frame, OK_FACE, BLUE) > 500)


def fill_in(outdir: Path, short_name: str, seen: dict,
            dialog_timeout: float = 480) -> None:
    """Wait for the dialog, bind the keyboard by touch, type `short_name`,
    put the keyboard away and commit with OK. What happened at each step is
    recorded in `seen`, so a failure says which one ran out."""
    frame = outdir / "frames" / "latest.png"
    # Booting to the dialog is the longest wait here and the one that varies
    # most with what else the host is doing, so it gets most of the ceiling.
    seen["dialog"] = wait_for_panel(outdir, is_setup_dialog, timeout=dialog_timeout)
    if not seen["dialog"]:
        return
    seen["configured"] = configured(outdir, timeout=180)
    seen["ink_before"] = dark_pixels(frame, SHORT_FIELD)

    # The interface blanks its panel after half a minute of its own time with
    # no input, so the first touch has to be prompt. Every input afterwards
    # resets that.
    # The keypad is bound when the on-screen keyboard is actually up, and on
    # its way in it covers the OK button. Waiting for that rather than for a
    # number of seconds is what keeps this from failing on a loaded host,
    # where six seconds of wall time can be almost no guest time at all --
    # which is how it failed (ADR 0040).
    #
    # A tap can also simply be lost: one that lands while the chip is in light
    # sleep wakes nothing, because on the T-Deck only the trackball, the radio
    # and the timer do (ADR 0165), as on the device. So the icon is tapped
    # again, as a person would, while the keyboard is visibly not up -- a tap
    # with it up would put it away.
    keyboard_up = lambda f: count_pixels(f, OK_FACE, BLUE) < 100  # noqa: E731
    seen["icon_taps"] = 0
    seen["keyboard"] = False
    for attempt in range(6):
        send_touch(outdir, *SHORT_ICON, hold=hold(attempt))   # binds the keypad
        seen["icon_taps"] += 1
        if wait_awake(outdir, keyboard_up, timeout=30):
            seen["keyboard"] = True
            break
    if not seen["keyboard"]:
        return
    for character in short_name:
        send_text(outdir, character)
        time.sleep(2.5)
    # Bounded, not assumed: if nothing was typed this falls through with the
    # field still empty and the caller's assertion says so.
    wait_awake(outdir, typed_into, timeout=90)
    seen["ink_after"] = dark_pixels(frame, SHORT_FIELD)

    # The same icon puts it away. And the OK button is reachable again only
    # once it has gone; tapping where the keyboard still is presses a key
    # instead. Tapped again while the keyboard is still up, as the first tap
    # was (a lost tap, ADR 0165).
    ok_visible = lambda f: count_pixels(f, OK_FACE, BLUE) > 500  # noqa: E731
    seen["dismissed"] = False
    for attempt in range(6):
        send_touch(outdir, *SHORT_ICON, hold=hold(attempt))
        if wait_awake(outdir, ok_visible, timeout=30):
            seen["dismissed"] = True
            break
    if not seen["dismissed"]:
        return
    # Committing makes the firmware restart itself to apply the settings, and
    # it says so on the panel first. The notice is what to wait for. OK is
    # tapped again only while it is still there to be tapped.
    #
    # A screen that went to powersave in the meantime shows neither, and reads
    # only its button: it is woken first, as a person would, rather than taken
    # for a dialog that has gone (which is how a suite once lost this step
    # with nothing to say for it).
    notice = lambda f: count_pixels(f, NOTICE, RED) > 200  # noqa: E731
    seen["reboot_notice"] = False
    seen["ok_taps"] = 0
    seen["woken"] = 0
    for attempt in range(6):
        if frame.exists() and blank(frame):
            send_trackball_press(outdir)
            seen["woken"] += 1
            wait_for_panel(outdir, ok_visible, timeout=10)
        send_touch(outdir, *OK_BUTTON, hold=hold(attempt))
        seen["ok_taps"] += 1
        if wait_for_panel(outdir, notice, timeout=30):
            seen["reboot_notice"] = True
            # Every reset before the commit's own was the forgiven one.
            uart = next(outdir.glob("*.uart.log"), None)
            seen["uart_before_commit"] = (uart.read_text(errors="replace")
                                          if uart else "")
            break
        if not wait_for_panel(outdir, lambda f: ok_visible(f) or blank(f), timeout=5):
            break
    # Whether the node itself said it would restart: a failure then says
    # whether the commit was lost or only its notice went unseen.
    log = next(outdir.glob("*.otg.log"), None)
    seen["restart_logged"] = log is not None and b"Reboot" in log.read_bytes()
