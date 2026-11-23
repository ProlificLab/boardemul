#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The test library (ADR 0182): a build read from its folder, devices made
and driven through the page's API, frames decoded. Against the real page,
with image building and runs faked, as tests/test_web.py does."""

from __future__ import annotations

import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import Build, BuildError, Lab  # noqa: E402
from boardemul.build import filesystem_offset  # noqa: E402
from boardemul.lab import Shot, decode_png  # noqa: E402
from emu import device as device_folder  # noqa: E402
from emu import run, web  # noqa: E402
from emu.png import RGB, encode_png  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def partition(kind: int, subtype: int, offset: int, size: int, label: bytes) -> bytes:
    return struct.pack("<2sBBII16sI", b"\xaa\x50", kind, subtype, offset, size,
                       label.ljust(16, b"\0"), 0)


TABLE = (partition(1, 0x02, 0x9000, 0x5000, b"nvs")
         + partition(0, 0x10, 0x10000, 0x300000, b"app0")
         + partition(1, 0x82, 0xC90000, 0x360000, b"spiffs")
         + b"\xff" * 32)


# -- a build ------------------------------------------------------------------


def test_a_build_folder_places_its_filesystem_by_its_partition_table(tmp_path) -> None:
    (tmp_path / "firmware.bin").write_bytes(b"app")
    (tmp_path / "littlefs.bin").write_bytes(b"fs")
    (tmp_path / "partitions.bin").write_bytes(TABLE)
    build = Build.from_dir(tmp_path)
    assert build.application == tmp_path / "firmware.bin"
    assert build.filesystem == tmp_path / "littlefs.bin"
    assert build.filesystem_offset == 0xC90000
    assert build.overlays == (f"0xc90000={tmp_path / 'littlefs.bin'}",)


def test_an_application_alone_is_a_build(tmp_path) -> None:
    (tmp_path / "firmware.bin").write_bytes(b"app")
    build = Build.from_dir(tmp_path)
    assert build.filesystem is None and build.overlays == ()


def test_a_filesystem_with_nowhere_to_go_is_refused(tmp_path) -> None:
    (tmp_path / "firmware.bin").write_bytes(b"app")
    (tmp_path / "littlefs.bin").write_bytes(b"fs")
    with pytest.raises(BuildError, match="partitions.bin"):
        Build.from_dir(tmp_path)
    (tmp_path / "partitions.bin").write_bytes(partition(1, 0x02, 0x9000, 0x5000, b"nvs"))
    with pytest.raises(BuildError, match="no filesystem partition"):
        Build.from_dir(tmp_path)


def test_no_application_is_no_build(tmp_path) -> None:
    with pytest.raises(BuildError, match="firmware.bin"):
        Build.from_dir(tmp_path)


def test_the_table_ends_at_the_first_entry_without_its_magic() -> None:
    assert filesystem_offset(b"\xff" * 32 + TABLE) is None


# -- frames -------------------------------------------------------------------


def test_a_frame_decodes_to_the_pixels_it_was_made_of() -> None:
    pixels = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
    shot = Shot(encode_png(2, 2, pixels, RGB))
    assert (shot.width, shot.height, shot.channels) == (2, 2, 3)
    assert shot.pixel(1, 1) == (100, 110, 120)
    assert shot.same_as(encode_png(2, 2, pixels, RGB))
    assert not shot.same_as(encode_png(2, 2, bytes(12), RGB))
    assert shot.same_as(encode_png(2, 2, bytes(p + 2 for p in pixels), RGB), tolerance=2)


def test_every_png_row_filter_is_undone() -> None:
    import zlib
    # One grey row per filter type (0 to 4), each encoding the bytes 5, 7.
    # Below the first row, "up" and Paeth predict each byte exactly (zero
    # left over), and "average" predicts (left + up) // 2.
    rows = [b"\x00\x05\x07", b"\x01\x05\x02", b"\x02\x00\x00", b"\x03\x03\x01",
            b"\x04\x00\x00"]
    ihdr = struct.pack(">IIBBBBB", 2, 5, 8, 0, 0, 0, 0)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    _w, _h, _c, data = decode_png(png)
    assert data == bytes([5, 7]) * 5


# -- devices through the page --------------------------------------------------


@pytest.fixture
def page(tmp_path, monkeypatch):
    def build(firmware, outdir, mac, overlays):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "flash.bin").write_bytes(b"flash" * 1024)
        (outdir / "efuse.bin").write_bytes(b"efuse")
        return outdir / "flash.bin", outdir / "efuse.bin"

    monkeypatch.setattr(run, "build_images", build)
    started = []
    monkeypatch.setattr(device_folder.RunHandle, "start",
                        lambda self: started.append((self.device.name, self.extra)))
    monkeypatch.setattr(device_folder.RunHandle, "alive", property(lambda self: True))
    srv = web.make_server(tmp_path / "devices", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "devices", started
    srv.state.stop_all()
    srv.shutdown()
    thread.join(timeout=5)


def test_a_device_is_made_from_a_build_and_started_on_an_air(page, tmp_path) -> None:
    url, devices, started = page
    folder = tmp_path / "build"
    folder.mkdir()
    (folder / "firmware.bin").write_bytes(b"app")
    (folder / "littlefs.bin").write_bytes(b"fs")
    (folder / "partitions.bin").write_bytes(TABLE)
    lab = Lab(url)
    node = lab.device("node", build=folder, board="tdeck", settings=("--host-clock",))
    made = device_folder.load(devices / "node")
    assert made.board == "tdeck"
    assert len(made.overlays) == 1 and made.overlays[0].startswith("0xc90000=")
    assert made.settings == ("--reboot", "--host-clock"), "its own kept, the asked added"
    node.start(air="bench")
    assert started and started[-1][0] == "node"
    assert "--air" in started[-1][1]
    again = lab.device("node", build=folder)
    assert again.name == "node", "a second make replaces the first"
    lab.close()


def test_a_device_that_is_not_running_says_so(page, tmp_path) -> None:
    url, _devices, _started = page
    from boardemul import LabError
    from boardemul.lab import Device
    lab = Lab(url)
    with pytest.raises(LabError, match="404"):
        Device(lab, "ghost").serial("usb").lines()
    lab.close()


def test_the_test_command_wants_its_command_after_two_dashes() -> None:
    done = subprocess.run([sys.executable, "-m", "boardemul", "test", "--build", "x"],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "after --" in done.stderr
