#!/usr/bin/env python3
"""A restart from either core boots the chip once (ADR 0123).

ESP-IDF's restart, run on core 1, resets core 0 and stalls it, then resets it
again, releases it, and resets core 1. QEMU's ESP32-S3 used to ignore the
stall and make each CPU reset a machine reset, so core 0 ran its ROM, and
often the bootloader, between its two resets. Every restart from core 1
printed the ROM's banner two or three times, and once core 1 died inside its
own restart.

The vendor firmware restarts from core 1 only after a panic that comes once
in tens of loaded boots (ADR 0114), so this boots a firmware of ours that does
it on purpose. tools/restart_probe restarts once from each core, by
esp_restart() and by abort(), then stops. It is not vendor firmware, so it is
neither downloaded nor pinned. It is built from its source, and the test skips
until it has been:

    python tools/restart_probe/build.py
    python3 -m pytest tests/test_restart_probe.py -m slow
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    BOOT_BANNER, FIRMWARE_RESTART_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_RESTART_PROBE.exists(),
                       reason="restart probe not built; run tools/restart_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

# What the probe does, boot by boot, as it says before each restart.
PLAN = [("esp_restart", 1), ("esp_restart", 0), ("abort", 1), ("abort", 0)]
# esp_reset_reason() as each boot reports it: power-on, then ESP_RST_SW after
# the two esp_restart() calls and ESP_RST_PANIC after the two aborts.
REASONS = [1, 3, 3, 4, 4]


def _done(uart: str, frames: list) -> bool:
    return "probe: done" in uart


def _restarts(uart: str) -> list[tuple[str, str]]:
    """Each restart: what the probe said it was about to do, and everything
    the chip said from then until the probe's next boot line."""
    parts = re.split(r"probe: ((?:esp_restart|abort) on core \d)", uart)
    return [(said, after.split("probe: boot", 1)[0])
            for said, after in zip(parts[1::2], parts[2::2])]


@pytest.fixture(scope="module", params=["single", "multi"])
def restarted(request, tmp_path_factory) -> Run:
    """The whole plan, allowed to reboot, on one translation thread and on a
    thread per core (BOARDEMUL_TCG_THREADS).

    One thread, which every other run uses, is where the defect lived: without
    ADR 0123's patch that arm fails every test below. With a thread per core
    the old race was narrow enough to pass, so that arm guards the patch's
    other path, the reset done on the reset core's own thread."""
    return run_emulator(tmp_path_factory.mktemp(f"probe-{request.param}"),
                        firmware=FIRMWARE_RESTART_PROBE, board="tdeck",
                        timeout=180, extra=("--reboot",),
                        env={"BOARDEMUL_TCG_THREADS": request.param},
                        until=_done)


@pytest.fixture(scope="module")
def ended(tmp_path_factory) -> tuple[Run, str]:
    """The first restart, under -no-reboot, which is how a single-board test
    sees a restart happen (ADR 0099)."""
    run = run_emulator(tmp_path_factory.mktemp("probe-no-reboot"),
                       firmware=FIRMWARE_RESTART_PROBE, board="tdeck", timeout=60)
    manifest = next(iter(run.outdir.glob("*.manifest.json")), None)
    assert manifest is not None, "the run left no manifest"
    return run, json.loads(manifest.read_text())["exit_reason"]


def test_the_probe_runs_its_whole_plan(restarted: Run) -> None:
    """Four restarts, from the cores and in the ways it was told, and a boot
    after each that knows why it happened."""
    said = [(how, int(core)) for how, core in
            re.findall(r"probe: (esp_restart|abort) on core (\d)", restarted.uart)]
    assert said == PLAN, restarted.uart[-1500:]
    reasons = [int(r) for r in
               re.findall(r"probe: boot \d+, reset reason (\d+)", restarted.uart)]
    assert reasons == REASONS, restarted.uart[-1500:]
    assert "probe: done" in restarted.uart


def test_each_restart_runs_the_rom_once(restarted: Run) -> None:
    """The regression: a restart from core 1 printed the banner two or three
    times, because core 0 ran its ROM between its two resets."""
    banners = {said: after.count(BOOT_BANNER) for said, after in _restarts(restarted.uart)}
    assert len(banners) == len(PLAN), banners
    assert all(n == 1 for n in banners.values()), banners


def test_the_usb_port_hears_the_same_boots(restarted: Run) -> None:
    """The ROM writes its banner to the USB serial port too (ADR 0100), so a
    second run of the ROM shows there even if the UART were cut short."""
    assert restarted.usb.count(BOOT_BANNER) == restarted.boots == len(PLAN) + 1


def test_no_restart_crashes_on_the_way(restarted: Run) -> None:
    """With core 0 running its loader while core 1 was still in its restart,
    core 1 once took a Double exception there, and the chain ended in a
    system reset. abort() says so in words of its own, not a Guru Meditation."""
    assert "Guru Meditation" not in restarted.uart, restarted.uart[-1500:]
    assert "rst:0x3 " not in restarted.uart, restarted.uart[-1500:]


def test_without_reboot_a_restart_from_core_1_ends_the_run(ended) -> None:
    """Core 1's reset of itself is what ends the run now, a guest reset like
    core 0's (ADR 0123). Before, the run ended at the restart's first reset of
    core 0, so this passed then too: it guards the exit point the patch moved,
    which a single-board test relies on to see a restart."""
    run, reason = ended
    assert "probe: esp_restart on core 1" in run.uart, run.uart[-1500:]
    assert reason == "guest exited (code 0)", reason
    assert run.boots == 1, run.uart[-1500:]
