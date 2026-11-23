#!/usr/bin/env python3
"""A device remembers what its guest saved (ADR 0133).

Slow: creates a device from Meshtastic's image, boots it twice. The first
boot finds a fresh filesystem and asks its setup dialog; the test answers it
the way a person does, and the run is stopped. The second boot must come up
already configured -- no dialog, a node with its region -- because the flash
it wrote is the same file the folder holds. Skips without a built QEMU or
the vendor images, like every boot test.
"""

from __future__ import annotations

import hashlib
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, qemu_available, reset_trouble,
    unforgiven, wait_for_panel,
)
from tests.setup_dialog import fill_in  # noqa: E402
from tools.make_configured_fs import count_pixels, is_setup_dialog  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(),
                       reason="vendor firmware not present"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(),
                       reason="no filesystem image"),
]

OVERLAY_AT = 0xC90000
NAME = "per1"
# How long a second boot is watched for a dialog that must never come.
NO_DIALOG_WINDOW = 75.0


def stop(proc: subprocess.Popen) -> None:
    """Interrupt the run the way a person stops it, so its own shutdown path
    runs: the card is written back and the manifest is kept."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def boot(device_root: Path, outdir: Path, tag: str) -> subprocess.Popen:
    """With --reboot, as every single-board boot test (ADR 0125): the ipc1
    canary Meshtastic trips now and then is survived, as the board does."""
    outdir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "emu/run.py", "--device", str(device_root),
         "--bus", "--outdir", str(outdir), "--tag", tag, "--reboot",
         "--timeout", "600", "--stall", "600"],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)


@pytest.fixture(scope="module")
def node(tmp_path_factory) -> dict:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    work = tmp_path_factory.mktemp("device")
    devices = work / "devices"
    from emu import device as device_folder
    device = device_folder.create(
        devices, "node", FIRMWARE_MESHTASTIC, board="tdeck", psram_octal=True,
        overlays=(f"0x{OVERLAY_AT:x}={MESHTASTIC_LITTLEFS}",))
    return {"device": device, "work": work,
            "flash_sha": lambda: hashlib.sha256(device.flash.read_bytes()).hexdigest()}


def test_the_flash_is_the_folders_own(node) -> None:
    """Sanity of the fixture: the folder's flash is a real image, sized like
    the firmware's, and the manifest recorded what went into it."""
    device = node["device"]
    assert device.flash.stat().st_size >= 4 * 1024 * 1024
    assert device.overlays == (f"0x{OVERLAY_AT:x}={MESHTASTIC_LITTLEFS}",)


def test_a_configured_device_comes_back_configured(node) -> None:
    device = node["device"]
    before = node["flash_sha"]()

    # -- first boot: fresh, so the setup dialog is the whole interface -------
    first = node["work"] / "boot1"
    proc = boot(device.root, first, "boot1")
    seen: dict = {}
    try:
        # As a person fills it in, tapping again when a tap is lost: one OK in
        # a run of three went unread, nothing was saved, and the second boot
        # rightly asked again (tests/setup_dialog.py).
        fill_in(first, NAME, seen, dialog_timeout=300.0)
        assert seen.get("dialog"), "a fresh device never asked its setup dialog"
        assert seen.get("reboot_notice"), f"OK did not commit: {seen}"
        resets = seen["uart_before_commit"].count("rst:0x")
        # Stopped at the restart it asks for, once it has saved, as the run
        # ended there before --reboot: the boot after it rewrites its files
        # first thing, and a stop in the middle of that would be judged.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline \
                and _uart(first, "boot1").count("rst:0x") <= resets:
            time.sleep(0.25)
    finally:
        stop(proc)
    # This boot ends in the restart OK asks for; before it, only the canary.
    wrong = unforgiven(_uart(first, "boot1"))
    assert not wrong, f"the first boot panicked, not in ADR 0099's canary: {wrong[0]!r}"

    # The guest had somewhere to put a preference, and used it.
    after_first = node["flash_sha"]()
    assert after_first != before, "the guest wrote nothing to the device's flash"

    # -- second boot: the same folder, so the same device ---------------------
    second = node["work"] / "boot2"
    proc = boot(device.root, second, "boot2")
    try:
        frames = second / "frames"
        assert wait_for_panel(second, lambda f: _ui_is_up(f, frames), 300.0), \
            "the second boot never drew its interface"
        # A node with no region asks the setup dialog every boot (that is
        # ADR 0026's flow). This one has one, so no dialog may appear while
        # the interface is up.
        deadline = time.monotonic() + NO_DIALOG_WINDOW
        asked = False
        while time.monotonic() < deadline:
            latest = frames / "latest.png"
            if latest.exists() and is_setup_dialog(latest):
                asked = True
                break
            time.sleep(1.0)
        assert not asked, "the second boot asked the setup dialog again: nothing was kept"
        assert _frames_differ(first, second), "the second boot drew the first boot's screen"
    finally:
        stop(proc)
    trouble = reset_trouble(_uart(second, "boot2"))
    assert not trouble, trouble


def _uart(outdir: Path, tag: str) -> str:
    path = outdir / f"{tag}.uart.log"
    return path.read_text(errors="replace") if path.exists() else ""


def _ui_is_up(frame: Path, frames: Path) -> bool:
    """The device-ui is drawing: at least three frames exist and the latest is
    neither the ROM clear nor the vendor splash (which are bright nearly
    everywhere)."""
    if len(list(frames.glob("frame-*.png"))) < 3:
        return False
    return count_pixels(frame, (0, 0, 240, 40), lambda r, g, b: r + g + b < 250) > 200


def _frames_differ(first: Path, second: Path) -> bool:
    a = first / "frames" / "latest.png"
    b = second / "frames" / "latest.png"
    if not a.exists() or not b.exists():
        return False
    return a.read_bytes() != b.read_bytes()
