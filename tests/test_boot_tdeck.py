#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for the T-Deck: the vendor firmware reaches its interface.

There are no golden frames here, and deliberately: LVGL animates the launcher
and the status bar carries a clock, so no single frame is stable. What is
stable is the sequence of walls the firmware used to stop at, and each test
below is one of them no longer stopping it (ADR 0013, 0016, 0019, 0020).

The board may reboot, as a T-Deck does after a panic (ADR 0099). One panic is
forgiven: the firmware's own ipc1 stack overflow, which 250620 hits on about
one loaded boot in twenty. The board is then judged on the boot that came
back, and any other panic still fails (ADR 0114).

    python3 -m pytest tests/ -m slow
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_TDECK, FIRMWARE_TDECK_BUILDS, Run, audio_streams, bus_clocks,
    by_boot, last_boot, qemu_available, radio_busy, run_emulator,
    unexplained_resets, unforgiven,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_TDECK.exists(),
                       reason="T-Deck vendor firmware not present"),
]


def _shown_everything(build: str):
    """The last things this module asks about: the battery read, an interface
    that kept drawing, and for a build that starts its station at boot, that it
    did -- all in the boot that came last. Once those are in, the rest of the
    timeout is an idle guest.
    """
    def shown(uart: str, frames: list) -> bool:
        # A panic that is not forgiven has decided the FPU test already. Once
        # the board is back from it the panic is in the log whole, and waiting
        # longer would only watch the board boot-loop to the timeout.
        if any(unforgiven(boot) for boot in by_boot(uart)[:-1]):
            return True
        last = last_boot(uart)
        if "ADC_CALI" not in last or len(frames) <= 100:
            return False
        return build not in STARTS_STATION_AT_BOOT or "WiFi client started" in last
    return shown


@pytest.fixture(scope="module", params=FIRMWARE_TDECK_BUILDS or [FIRMWARE_TDECK],
                ids=lambda p: p.stem.replace("T-Deck_UnitTest_", ""))
def booted(request, tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    build = request.param.stem[-6:]
    run = run_emulator(tmp_path_factory.mktemp("tdeck"),
                       firmware=request.param, board="tdeck", timeout=300,
                       extra=("--reboot",), until=_shown_everything(build))
    if run.boots > 1:
        # A pass that needed a second boot says so, rather than passing
        # quietly; a failure carries the count in its message instead.
        warnings.warn(f"build {build} booted {run.boots} times (ADR 0114)")
    return run


def _boots(run: Run) -> str:
    return f"booted {run.boots} time(s)"


def test_the_fpu_is_enabled_before_the_scheduler_starts(booted: Run) -> None:
    """The regression this board exists to guard (ADR 0013).

    With CPENABLE reset to 0 this firmware dies about a second in, inside a
    C++ global constructor, on a float that no task yet owns the FPU state for.
    Silicon resets it to 0xff, and so does QEMU now (ADR 0143).
    The panic prints before anything else the firmware would say, so its absence
    is the assertion; the tests below only show it kept going afterwards.

    The board may reboot now (ADR 0114), so "no panic at all" would fail it for
    the firmware's own ipc1 overflow. Each boot is judged by the panic that
    ended it: that one is forgiven, with what its panic handler does after it.
    This one, or any other, fails in whichever boot it came.
    """
    assert "Coprocessor exception" not in booted.uart, _boots(booted)
    assert not unforgiven(booted.uart), (unforgiven(booted.uart), _boots(booted))


def test_it_reboots_only_from_the_forgiven_panic(booted: Run) -> None:
    """A reboot is allowed for the panic ADR 0099 traced, and nothing else.

    Before the board could reboot, any reset ended the run and failed what came
    after it. So a boot that ended some other way -- a watchdog, an abort, a
    reset with no panic before it -- still fails, here (ADR 0114). So does a
    boot of nothing but the ROM's banner: the canary's reset used to print two,
    until QEMU held core 0 where the restart stalls it (ADR 0123).
    """
    unexplained = unexplained_resets(booted.uart)
    boots = by_boot(booted.uart)
    assert not unexplained, (
        f"boot {unexplained[0]} of {len(boots)} ended without the ipc1 canary "
        "panic: " + boots[unexplained[0] - 1][-1500:])


def test_it_reaches_the_arduino_layer(booted: Run) -> None:
    """The constructor that used to kill it runs before any of this, so getting
    here at all means startup completed."""
    last = last_boot(booted.uart)
    assert "[E][" in last or "[I][" in last, (last[-400:], _boots(booted))


def test_it_gets_far_enough_to_mount_storage(booted: Run) -> None:
    """Reaching storage at all means the app's own setup() is running.

    What that looks like differs by build and has changed. There is a card in
    the slot now (ADR 0033), so a build that mounts it says how big it is,
    where it used to fail; a build whose internal filesystem is empty still
    says so. Either is evidence of the same thing, which is why this asks for
    storage to have been reached rather than for it to have failed.
    """
    last = last_boot(booted.uart)
    reached = "SD Card Size" in last or "SPIFFS" in last or "sd_diskio" in last
    assert reached, (last[-400:], _boots(booted))


# Builds whose setup() starts the WiFi station, and so meet the four radio
# waits of ADR 0019 during boot. 251113 initialises WiFi (WIFI_READY) but does
# not start the station until asked from its interface, so at boot it never
# reaches the gates -- the claim below is not false for it, it is not made.
STARTS_STATION_AT_BOOT = {"250620"}


def test_it_gets_through_the_radio_bring_up(booted: Run, request) -> None:
    """esp_wifi_start() returns only once four hardware waits are answered
    (ADR 0019), and Arduino's own event handler announces it with STA_START:
    "WiFi client started"."""
    build = request.node.callspec.id
    if build not in STARTS_STATION_AT_BOOT:
        pytest.skip(f"build {build} does not start the station during setup")
    last = last_boot(booted.uart)
    after = last.split("WiFi interface ready", 1)[-1][:600]
    assert "WiFi client started" in last, (after, _boots(booted))


def test_the_radio_comes_up_waiting_on_its_busy_line(booted: Run) -> None:
    """BUSY on pin 13 and reset on pin 17, kept on the guest's clock (ADR
    0111). RadioLib waits on BUSY before and after every command and gives up
    with -705 after a second; a line held and never released is how that
    looked while it was being built. A reset holds it for the datasheet's
    3.5 ms start-up."""
    # 250620 says so on its UART, 251113 on its USB serial port. The ROM's
    # banner reaches both, so each is cut at the same boot.
    said = last_boot(booted.uart) + last_boot(booted.usb)
    assert "Start Radio success!" in said, (said[-1500:], _boots(booted))
    busy = radio_busy(booted.outdir)
    assert busy is not None, "the controller never reported the radio's BUSY line"
    holds, longest = busy
    assert holds >= 1 and longest >= 3500, busy


def test_the_microphone_is_clocked_at_the_rate_the_firmware_set(booted: Run) -> None:
    """The vendor sets its microphone's I2S to 16 kHz, 16-bit
    (MIC_I2S_SAMPLE_RATE in its UnitTest sources), and a capture takes as long
    as that clock says, not a fixed tick per buffer (ADR 0119)."""
    streams = audio_streams(booted.outdir)
    mic = [s for s in streams if s[:2] == ("i2s1", "receive")]
    assert mic, f"no capture was paced by its clock: {streams}"
    _block, _direction, hz, _channels, bits = mic[0]
    assert abs(hz - 16000) < 16 and bits == 16, mic[0]


def test_the_radio_and_i2c_buses_run_at_their_drivers_clocks(booted: Run) -> None:
    """RadioLib talks to the radio at its default 2 MHz (RADIOLIB_DEFAULT_SPI_
    SETTINGS for Arduino), and Wire, begun without a frequency, runs at the
    100 kHz i2cInit gives it. Each transfer now takes that long on the bus
    (ADRs 0120, 0121)."""
    clocks = bus_clocks(booted.outdir)
    assert 2e6 in clocks.get("spi2:1", []), clocks
    scl = clocks.get("i2c0", [])
    assert scl and abs(scl[0] - 100e3) < 1e3, clocks


def test_it_reads_its_battery_and_keeps_drawing(booted: Run) -> None:
    """The ADC conversion completing is what lets LVGL keep its turn (ADR
    0020). A stalled interface stops at a couple of dozen frames; a live one
    draws about ten a second for as long as it runs.

    The panel is a part on the board, not in the chip, so it numbers its frames
    across a reboot and the count here is the run's. A boot the forgiven panic
    ended had drawn two or three: it panics while starting the radio, before
    the interface runs (ADR 0114).
    """
    assert "ADC_CALI" in last_boot(booted.uart), _boots(booted)
    assert len(booted.frames) > 100, (len(booted.frames), _boots(booted))
