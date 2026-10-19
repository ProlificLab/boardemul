#!/usr/bin/env python3
"""Device folders: identity and storage that outlive a run (ADR 0133).

Fast: the folder format, the manifest's round trip, and the refusals the
emulator and the CLI both lean on. Starting a real device is the slow test's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import device as device_folder  # noqa: E402
from emu import run  # noqa: E402


FIRMWARE_BYTES = bytes([0xE9, 1, 2, 3]) + b"\x00" * 32


@pytest.fixture()
def firmware(tmp_path: Path) -> Path:
    path = tmp_path / "fw" / "demo.bin"
    path.parent.mkdir()
    path.write_bytes(FIRMWARE_BYTES)
    return path


@pytest.fixture()
def fake_build(tmp_path: Path, monkeypatch):
    """Stand in for mkflash: two files, recorded, never a real image."""
    calls: list[tuple[Path, Path, str, list[str]]] = []

    def build(firmware: Path, outdir: Path, mac: str,
              overlays: list[str]) -> tuple[Path, Path]:
        outdir.mkdir(parents=True, exist_ok=True)
        flash, efuse = outdir / "flash.bin", outdir / "efuse.bin"
        flash.write_bytes(b"flash")
        efuse.write_bytes(b"efuse")
        calls.append((firmware, outdir, mac, list(overlays)))
        return flash, efuse

    monkeypatch.setattr(run, "build_images", build)
    return calls


# -- the folder format --------------------------------------------------------


def test_create_makes_the_folder_and_its_storage(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    device = device_folder.create(devices, "pocket", firmware)
    assert device.name == "pocket"
    assert device.flash.is_file() and device.efuse.is_file()
    assert device.has_sdcard                      # this board has a card
    assert len(device.sdcard.read_bytes()) > 0
    fields = json.loads((devices / "pocket" / "device.json").read_text())
    assert fields["board"] == "tdeck-max"
    assert fields["firmware"] == str(firmware)
    assert fields["overlays"] == []


def test_create_records_the_firmwares_hash(tmp_path, firmware, fake_build):
    import hashlib
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    assert device.firmware_sha256 == hashlib.sha256(firmware.read_bytes()).hexdigest()


def test_create_passes_the_overlays_to_the_assembler(tmp_path, firmware, fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware,
                                  overlays=("0xC90000=fs.bin",))
    assert fake_build[0][3] == ["0xC90000=fs.bin"]
    assert device.overlays == ("0xC90000=fs.bin",)


def test_create_refuses_an_existing_device(tmp_path, firmware, fake_build):
    device_folder.create(tmp_path / "devices", "pocket", firmware)
    with pytest.raises(device_folder.DeviceError):
        device_folder.create(tmp_path / "devices", "pocket", firmware)


def test_create_refuses_a_missing_firmware_and_leaves_nothing(tmp_path, fake_build):
    devices = tmp_path / "devices"
    with pytest.raises(device_folder.DeviceError):
        device_folder.create(devices, "pocket", tmp_path / "nope.bin")
    assert not (devices / "pocket").exists()


def test_a_failed_assembly_leaves_no_half_device(tmp_path, firmware, monkeypatch):
    devices = tmp_path / "devices"
    root = devices / "pocket"

    def broken(firmware, outdir, mac, overlays):
        (outdir / "flash.bin").write_bytes(b"half")
        raise RuntimeError("assembler exploded")

    monkeypatch.setattr(run, "build_images", broken)
    with pytest.raises(RuntimeError):
        device_folder.create(devices, "pocket", firmware)
    assert not root.exists()


def test_a_bad_name_is_refused_before_anything_is_made(tmp_path, fake_build):
    for name in ("", "..", "a/b", "a\\b", "."):
        with pytest.raises(device_folder.DeviceError):
            device_folder.create(tmp_path / "devices", name, tmp_path / "x.bin")


# -- reading it back ----------------------------------------------------------


def test_load_round_trips_the_manifest(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    made = device_folder.create(devices, "pocket", firmware, board="tdeck",
                                mac="02:00:00:00:00:02", psram_octal=True)
    loaded = device_folder.load(made.root)
    assert loaded == made


def test_load_refuses_an_ordinary_directory(tmp_path):
    with pytest.raises(device_folder.DeviceError):
        device_folder.load(tmp_path)


def test_load_refuses_a_device_without_storage(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    device = device_folder.create(devices, "pocket", firmware)
    device.flash.unlink()
    with pytest.raises(device_folder.DeviceError):
        device_folder.load(device.root)


# -- the card -----------------------------------------------------------------


def test_an_absent_card_loads_as_empty_of_the_boards_size(tmp_path, firmware,
                                                          fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    device.sdcard.unlink()
    image = device_folder.load_sdcard(device, 4 * 1024 * 1024)
    assert len(image) == 4 * 1024 * 1024 and not any(image)


def test_a_card_of_another_size_is_refused_not_resized(tmp_path, firmware,
                                                       fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    device.sdcard.write_bytes(b"tiny")
    with pytest.raises(device_folder.DeviceError):
        device_folder.load_sdcard(device, 4 * 1024 * 1024)


def test_the_cards_contents_come_back_round(tmp_path, firmware, fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    image = device_folder.load_sdcard(device, device.sdcard.stat().st_size)
    image[0:5] = b"hello"
    device_folder.save_sdcard(device, image)
    assert device.sdcard.read_bytes()[0:5] == b"hello"


# -- flashing and removing ----------------------------------------------------


def test_reflash_keeps_the_card_and_updates_the_record(tmp_path, firmware,
                                                       fake_build):
    devices = tmp_path / "devices"
    device = device_folder.create(devices, "pocket", firmware)
    device_folder.save_sdcard(device, bytearray(b"kept" + b"\x00" * 4092))
    new = tmp_path / "fw" / "other.bin"
    new.write_bytes(FIRMWARE_BYTES + b"new")
    updated = device_folder.reflash(device, new)
    assert updated.firmware == str(new)
    assert updated.firmware_sha256 != device.firmware_sha256
    assert updated.sdcard.read_bytes()[0:4] == b"kept"
    assert fake_build[-1][0] == new                  # the assembler saw it


def test_remove_takes_the_whole_folder(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    device = device_folder.create(devices, "pocket", firmware)
    device_folder.remove(device)
    assert not device.root.exists()


def test_all_devices_sorts_by_name(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    device_folder.create(devices, "zeta", firmware)
    device_folder.create(devices, "alpha", firmware)
    assert [d.name for d in device_folder.all_devices(devices)] == ["alpha", "zeta"]


# -- what the emulator refuses ------------------------------------------------


def test_run_refuses_a_firmware_and_a_device_together(tmp_path, firmware,
                                                      fake_build):
    with pytest.raises(SystemExit, match="either a firmware"):
        run.main([str(firmware), "--device", str(tmp_path)])


def test_run_refuses_neither(tmp_path):
    with pytest.raises(SystemExit, match="either a firmware"):
        run.main([])


def test_run_refuses_an_overlay_on_a_device(tmp_path, firmware, fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    with pytest.raises(SystemExit, match="--device keeps its own storage"):
        run.main(["--device", str(device.root),
                  "--overlay", "0xC90000=fs.bin"])


def test_run_refuses_a_folder_that_is_not_a_device(tmp_path, fake_build):
    with pytest.raises(SystemExit, match="not a device folder"):
        run.main(["--device", str(tmp_path)])


def test_run_takes_the_devices_identity_without_rebuilding(
        tmp_path, firmware, fake_build, monkeypatch):
    """In device mode the assembler is not called at all: the folder's own
    storage is what the run points QEMU at."""
    device = device_folder.create(tmp_path / "devices", "pocket", firmware,
                                  mac="02:00:00:00:00:09")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a device run rebuilt its storage")

    monkeypatch.setattr(run, "build_images", forbidden)
    monkeypatch.setattr(run, "find_qemu", lambda _e=None: Path("qemu-stub"))
    monkeypatch.setattr(run, "qemu_version", lambda _q: "stub 0")
    started = {}

    def fake_run_qemu(cmd, *_a, **_kw):
        started["cmd"] = cmd
        return "ended", 0.1

    monkeypatch.setattr(run, "run_qemu", fake_run_qemu)
    run.main(["--device", str(device.root), "--timeout", "1"])
    cmd = started["cmd"]
    # The paths ride inside -drive arguments, so look there.
    assert any(f"file={device.flash}," in arg for arg in cmd)
    assert any(f"file={device.efuse}," in arg for arg in cmd)
    assert "02:00:00:00:00:09" not in cmd            # the MAC is in the eFuses
