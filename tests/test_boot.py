#!/usr/bin/env python3
"""End-to-end tests: real firmware, real QEMU, real pixels.

These are minutes rather than milliseconds, so they are marked slow and skipped
unless asked for:

    python3 -m pytest tests/ -m slow

They exist because everything below them can pass while the thing as a whole
does nothing. The golden frames are the only assertion in this project that the
emulator draws the right picture.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import FIRMWARE, REPO_ROOT, region_digest  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

GOLDEN = Path(__file__).resolve().parent / "golden"
# The middle icon of the launcher's top row, on a 240x320 panel.
SETTING_ICON = (120, 75)
LAUNCHER_FRAMES = 3         # clear, splash, then the menu


def qemu_available() -> bool:
    local = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
    return bool(os.environ.get("BOARDEMUL_QEMU")
                or local.is_file()
                or shutil.which("qemu-system-xtensa"))


@dataclass
class Run:
    outdir: Path
    frames: list[Path] = field(default_factory=list)
    uart: str = ""

    def digests(self) -> list[str]:
        return [region_digest(f) for f in self.frames]


def _frames(outdir: Path) -> list[Path]:
    return sorted((outdir / "frames").glob("frame-*.png"))


def run_emulator(outdir: Path, *, tap: tuple[int, int] | None = None,
                 timeout: int = 150, settle: int = 25) -> Run:
    """Boot the factory firmware, optionally tap once the launcher is up."""
    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE), "--bus", "--tag", "t",
         "--outdir", str(outdir), "--timeout", str(timeout),
         "--stall", str(timeout)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(_frames(outdir)) >= LAUNCHER_FRAMES:
                break
            if proc.poll() is not None:
                break
            time.sleep(1)

        if tap is not None:
            events = outdir / "input.jsonl"
            with events.open("a") as fh:
                fh.write(json.dumps({"type": "touch", "x": tap[0],
                                     "y": tap[1], "down": True}) + "\n")
                fh.flush()
                time.sleep(0.8)
                fh.write(json.dumps({"type": "touch", "down": False}) + "\n")
                fh.flush()
            # A refresh takes a couple of seconds of emulated time; give the
            # firmware room to redraw before looking.
            time.sleep(settle)
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    uart = ""
    for log in outdir.glob("*.uart.log"):
        uart = log.read_text(errors="replace")
    return Run(outdir=outdir, frames=_frames(outdir), uart=uart)


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("boot"))


@pytest.fixture(scope="module")
def tapped(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("tap"), tap=SETTING_ICON)


# -- boot -------------------------------------------------------------------


def test_rom_loads_the_bootloader_segments(booted: Run) -> None:
    """These lines are computable from the image header before running
    anything, so a mismatch means the flash image is wrong -- not the hardware.
    """
    for expected in ("load:0x3fce3808,len:0x44c",
                     "load:0x403c9700,len:0xbd8",
                     "load:0x403cc700,len:0x2a80",
                     "entry 0x403c98d0"):
        assert expected in booted.uart, expected


def test_psram_is_detected(booted: Run) -> None:
    """It only enumerates in quad line mode under this fork (ADR 0007), and
    without it LVGL allocates nothing and there is no picture at all."""
    assert "PSRAM enabled" in booted.uart
    assert "PSRAM ID read error" not in booted.uart


def test_the_firmware_does_not_panic(booted: Run) -> None:
    assert "Guru Meditation" not in booted.uart
    assert "Rebooting" not in booted.uart


def test_touch_controller_identifies_itself(booted: Run) -> None:
    assert "ictype:[328]" in booted.uart


def test_launcher_is_drawn(booted: Run) -> None:
    golden = region_digest(GOLDEN / "launcher.png")
    assert golden in booted.digests(), (
        "no frame matched the golden launcher; "
        f"got {booted.digests()}")


# -- interaction ------------------------------------------------------------


def test_a_tap_changes_the_screen(tapped: Run) -> None:
    launcher = region_digest(GOLDEN / "launcher.png")
    digests = tapped.digests()
    assert launcher in digests, "never reached the launcher, so the tap proves nothing"
    after = digests[digests.index(launcher) + 1:]
    assert any(d != launcher for d in after), "the screen never changed"


def test_a_tap_opens_settings(tapped: Run) -> None:
    assert region_digest(GOLDEN / "settings.png") in tapped.digests()
