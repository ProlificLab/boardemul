# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A PlatformIO build's firmware.bin is the application alone; the Build
places it with its bootloader and partition table as a flash is written
(ADR 0182, amended)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import Build, BuildError  # noqa: E402
from boardemul.build import TABLE_OFFSET, is_whole_image  # noqa: E402
from emu import device as device_folder, run  # noqa: E402


def entry(kind: int, subtype: int, offset: int, size: int, label: bytes) -> bytes:
    return struct.pack("<2sBBII16sI", b"\xaa\x50", kind, subtype, offset, size,
                       label.ljust(16, b"\0"), 0)


TABLE = (entry(1, 0x02, 0x9000, 0x5000, b"nvs") + entry(0, 0x10, 0x10000, 0x330000, b"app0")
         + entry(1, 0x82, 0x670000, 0x180000, b"spiffs") + b"\xff" * 32)
BOOT = b"\xe9" + b"B" * 99
APP = b"\xe9" + b"A" * 199


def pio_folder(tmp_path: Path, **leave_out) -> Path:
    folder = tmp_path / "t-deck"
    folder.mkdir()
    for name, data in (("bootloader.bin", BOOT), ("partitions.bin", TABLE),
                       ("firmware.bin", APP)):
        if name not in leave_out:
            (folder / name).write_bytes(data)
    return folder


def test_an_application_alone_is_placed_with_its_bootloader_and_table(tmp_path):
    build = Build.from_dir(pio_folder(tmp_path))
    image = build.application.read_bytes()
    assert is_whole_image(image)
    assert image[:len(BOOT)] == BOOT
    assert image[TABLE_OFFSET:TABLE_OFFSET + len(TABLE)] == TABLE
    assert image[0x10000:0x10000 + len(APP)] == APP
    assert image[len(BOOT):TABLE_OFFSET] == b"\xff" * (TABLE_OFFSET - len(BOOT))
    assert not (tmp_path / "t-deck" / "firmware.bin").read_bytes() == image
    # The same content is the same file, made once.
    assert Build.from_dir(tmp_path / "t-deck").application == build.application


def test_a_whole_image_is_used_as_it_is(tmp_path):
    whole = bytearray(b"\xff" * 0x10000)
    whole[0] = 0xE9
    whole[TABLE_OFFSET:TABLE_OFFSET + len(TABLE)] = TABLE
    (tmp_path / "firmware.bin").write_bytes(bytes(whole))
    assert Build.from_dir(tmp_path).application == tmp_path / "firmware.bin"


def test_an_application_without_its_pieces_goes_as_it_is(tmp_path):
    """Without the bootloader beside it there is nothing to place it with;
    the emulator judges it, and says why it refuses (below)."""
    folder = pio_folder(tmp_path, **{"bootloader.bin": True})
    assert Build.from_dir(folder).application == folder / "firmware.bin"


def test_a_refused_image_is_a_device_error_not_an_exit(tmp_path, monkeypatch):
    def refuse(firmware, outdir, mac, overlays):
        raise SystemExit("mkflash refused the image; not launching QEMU")

    monkeypatch.setattr(run, "build_images", refuse)
    firmware = tmp_path / "app.bin"
    firmware.write_bytes(APP)
    with pytest.raises(device_folder.DeviceError, match="not an image this can flash"):
        device_folder.create(tmp_path / "devices", "x", firmware)
    assert not (tmp_path / "devices" / "x").exists()
