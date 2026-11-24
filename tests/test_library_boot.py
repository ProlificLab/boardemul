#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The test library on a real boot (ADR 0182).

Slow: a Meshtastic build folder -- the pinned firmware, its filesystem, and
a partition table that puts the filesystem where Meshtastic's own does --
made into a device through a page the Lab starts itself, then read through
its OTG serial port and its screen; and `boardemul test` running a command
against two devices on one air, passing on that command's exit status.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import Lab  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def entry(kind: int, subtype: int, offset: int, size: int, label: bytes) -> bytes:
    return struct.pack("<2sBBII16sI", b"\xaa\x50", kind, subtype, offset, size,
                       label.ljust(16, b"\0"), 0)


@pytest.fixture(scope="module")
def build(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("pio") / "t-deck-tft"
    folder.mkdir()
    shutil.copy(FIRMWARE_MESHTASTIC, folder / "firmware.bin")
    shutil.copy(MESHTASTIC_LITTLEFS, folder / "littlefs.bin")
    (folder / "partitions.bin").write_bytes(
        entry(1, 0x02, 0x9000, 0x5000, b"nvs")
        + entry(0, 0x10, 0x10000, 0x640000, b"app0")
        + entry(1, 0x82, 0xC90000, 0x370000, b"spiffs")
        + b"\xff" * 32)
    return folder


def test_a_lab_device_boots_the_build_and_is_read(build, tmp_path) -> None:
    with Lab() as lab:
        node = lab.device("libnode", build=build, board="tdeck")
        node.start()
        found = node.serial("otg").expect(r"SX126x init result (-?\d+)", timeout=600)
        assert found.group(1) == "0", "the radio came up"
        # The filesystem reached its partition: the device-ui reads its
        # settings from it and draws more than the boot logo.
        shot = node.screen.wait(lambda s: s.pixel(2, 2) != (255, 255, 255), timeout=300)
        assert shot.width == 320 and shot.height == 240
        logs = node.save_logs(tmp_path / "libnode.zip")
        assert logs.stat().st_size > 0


def test_boardemul_test_answers_with_the_command_s_status(build, tmp_path) -> None:
    check = ("import os,sys; from boardemul import Lab; from boardemul.lab import Device; "
             "lab=Lab(); names=os.environ['BOARDEMUL_DEVICES'].split(','); "
             "assert len(names)==2; "
             "[Device(lab,n).serial('otg').expect('SX126x init result 0',timeout=900) "
             "for n in names]; sys.exit(7)")
    done = subprocess.run(
        [sys.executable, "-m", "boardemul", "test", "--build", str(build),
         "--board", "tdeck", "--devices", "2", "--logs", str(tmp_path / "logs"),
         "--", sys.executable, "-c", check],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800)
    assert done.returncode == 7, done.stderr[-2000:]
    kept = sorted(p.name for p in (tmp_path / "logs").glob("*.zip"))
    assert kept == ["dut1.zip", "dut2.zip"]


def test_a_scenario_runs_two_devices_one_on_a_described_board(build, tmp_path) -> None:
    """`boardemul scenario` (ADR 0185) on the same build: a device on the
    T-Deck and one on its description (ADR 0184), on one air, each waited
    for on its OTG port, and a frame kept."""
    described = REPO_ROOT / "emu" / "boards" / "tdeck.toml"
    file = tmp_path / "pair.toml"
    file.write_text(
        "[[device]]\n"
        'name = "hand"\n'
        f'build = "{build.as_posix()}"\n'
        "[[device]]\n"
        'name = "described"\n'
        f'build = "{build.as_posix()}"\n'
        f'board = "{described.as_posix()}"\n'
        "[[step]]\n"
        'device = "hand"\n'
        'expect = "SX126x init result 0"\n'
        'port = "otg"\n'
        "timeout = 900\n"
        "[[step]]\n"
        'device = "described"\n'
        'expect = "SX126x init result 0"\n'
        'port = "otg"\n'
        "timeout = 900\n"
        "[[step]]\n"
        'device = "described"\n'
        'save_screen = "shots/described.png"\n')
    done = subprocess.run(
        [sys.executable, "-m", "boardemul", "scenario", str(file),
         "--logs", str(tmp_path / "logs")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=2400)
    print(done.stdout)
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-2000:])
    assert "ok   step 3" in done.stdout and done.stdout.rstrip().endswith("passed")
    assert (tmp_path / "shots" / "described.png").read_bytes()[:4] == b"\x89PNG"
    kept = sorted(p.name for p in (tmp_path / "logs").glob("*.zip"))
    assert kept == ["described.zip", "hand.zip"]
