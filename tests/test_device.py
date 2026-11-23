#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Device folders: identity and storage that outlive a run (ADR 0133).

Fast: the folder format, the manifest's round trip, and the refusals the
emulator and the CLI both lean on. Starting a real device is the slow test's.
"""

from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
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
    assert fields["firmware"] == firmware.as_posix()      # forward slashes (ADR 0140)
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


def test_a_card_is_renamed_into_place_and_not_rewritten_unchanged(tmp_path, firmware,
                                                                fake_build, monkeypatch):
    """ADR 0133, amended: a save interrupted mid-write leaves the old card
    whole, and a run that left the card alone does not write it."""
    import hashlib
    real_replace = device_folder.os.replace
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    old = bytes(device.sdcard.read_bytes())
    loaded = hashlib.sha256(old).hexdigest()
    assert device_folder.save_sdcard(device, bytearray(old), loaded) is False

    def interrupted(src, dst):
        raise OSError("stopped while it wrote")
    monkeypatch.setattr(device_folder.os, "replace", interrupted)
    with pytest.raises(OSError):
        device_folder.save_sdcard(device, bytearray(b"\x5a" * len(old)), loaded)
    assert device.sdcard.read_bytes() == old, "the old card is whole"
    monkeypatch.setattr(device_folder.os, "replace", real_replace)
    assert device_folder.save_sdcard(device, bytearray(b"\x5a" * len(old)), loaded)
    assert device.sdcard.read_bytes()[:2] == b"\x5a\x5a"
    assert not device.sdcard.with_name(device.sdcard.name + ".part").exists()


def test_reflash_keeps_the_card_and_updates_the_record(tmp_path, firmware,
                                                       fake_build):
    devices = tmp_path / "devices"
    device = device_folder.create(devices, "pocket", firmware)
    device_folder.save_sdcard(device, bytearray(b"kept" + b"\x00" * 4092))
    new = tmp_path / "fw" / "other.bin"
    new.write_bytes(FIRMWARE_BYTES + b"new")
    updated = device_folder.reflash(device, new)
    assert updated.firmware == new.as_posix()
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


# -- snapshots and the card (ADR 0149) ----------------------------------------


def test_a_snapshot_brings_back_storage_card_and_settings(tmp_path, firmware,
                                                         fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    device = device_folder.set_settings(device, ["--reboot"])
    taken = device_folder.snapshot(device, "good")
    assert taken.label == "good" and taken.bytes > 32 * 1024 * 1024
    device.flash.write_bytes(b"broken")
    device.sdcard.write_bytes(b"\xff" * len(device.sdcard.read_bytes()))
    device = device_folder.set_settings(device, [])
    back = device_folder.restore(device, "good")
    assert back.flash.read_bytes() == b"flash"
    assert back.sdcard.read_bytes()[:1] != b"\xff"
    assert back.settings == ("--reboot",)
    assert [s.label for s in device_folder.snapshots(back)] == ["good"]
    device_folder.forget(back, "good")
    assert device_folder.snapshots(back) == []


@pytest.mark.parametrize("label", ["", "../x", "a b", "x" * 41])
def test_a_snapshot_label_is_a_plain_word(tmp_path, firmware, fake_build, label):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    with pytest.raises(device_folder.DeviceError):
        device_folder.snapshot(device, label or "/")


def test_a_held_device_is_neither_snapshotted_nor_given_a_card(tmp_path, firmware,
                                                               fake_build):
    """While a run holds the folder its files are not a state the device was
    ever in (ADR 0149)."""
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    (device.root / device_folder.RUNNING).write_text("1 2026-11-18T12:00:00\n")
    assert device_folder.held(device) == "2026-11-18T12:00:00"
    with pytest.raises(device_folder.DeviceBusy, match="stop it first"):
        device_folder.snapshot(device)
    with pytest.raises(device_folder.DeviceBusy):
        device_folder.card_from_files(device, [("A.TXT", b"a")])


@pytest.mark.skipif(os.name != "posix", reason="process numbers are checked on POSIX")
def test_a_marker_left_by_a_run_that_was_killed_no_longer_holds(tmp_path, firmware,
                                                                fake_build) -> None:
    """A killed run leaves its marker; from this machine, a process that is
    gone is seen to be gone, and the folder is the operator's again."""
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    marker = device.root / device_folder.RUNNING
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    marker.write_text(f"{proc.pid} 2026-11-20T10:20:43 {_socket.gethostname()}\n")
    assert device_folder.held(device) is None
    assert not marker.exists()
    # Another machine's run, or this one's while it lives, still holds.
    marker.write_text(f"{proc.pid} 2026-11-20T10:20:43 some-other-host\n")
    assert device_folder.held(device) == "2026-11-20T10:20:43"
    marker.write_text(f"{os.getpid()} 2026-11-20T10:20:43 {_socket.gethostname()}\n")
    assert device_folder.held(device) == "2026-11-20T10:20:43"


def test_a_card_made_from_files_holds_them_and_keeps_the_old_one(tmp_path, firmware,
                                                                fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    old = device.sdcard.read_bytes()
    taken = device_folder.card_from_files(device, [("notes.txt", b"hello card")])
    image = device.sdcard.read_bytes()
    assert len(image) == len(old)
    assert b"NOTES   TXT" in image and b"hello card" in image
    kept = device.root / "snapshots" / taken.label / "sdcard.img"
    assert kept.read_bytes() == old


@pytest.mark.parametrize("name", ["much-too-long.txt", "a.long", "two.dots.txt"])
def test_a_name_that_is_not_8_3_is_refused(tmp_path, firmware, fake_build, name):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    with pytest.raises(device_folder.DeviceError, match="8.3"):
        device_folder.card_from_files(device, [(name, b"x")])


def test_removing_a_device_takes_its_snapshots(tmp_path, firmware, fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    device_folder.snapshot(device, "one")
    device_folder.remove(device)
    assert not device.root.exists()


# -- clones (ADR 0155) ---------------------------------------------------------


def test_a_clone_has_the_storage_and_a_mac_of_its_own(tmp_path, firmware, fake_build):
    from tools import mkflash
    device = device_folder.create(tmp_path / "devices", "node", firmware,
                                  mac="24:6F:28:00:00:11")
    device = device_folder.set_settings(device, ["--air", "mesh"])
    device.flash.write_bytes(b"configured flash")
    device_folder.snapshot(device, "kept")
    twin = device_folder.clone(device, "node-2")
    assert twin.mac == "24:6F:28:00:00:12" and twin.cloned_from == "node"
    assert twin.flash.read_bytes() == b"configured flash"
    assert twin.sdcard.read_bytes() == device.sdcard.read_bytes()
    assert twin.efuse.read_bytes() == mkflash.build_efuse(mkflash.parse_mac(twin.mac))
    assert twin.settings == ("--air", "mesh")
    assert device_folder.snapshots(twin) == []          # the original's history
    assert device_folder.load(twin.root).cloned_from == "node"
    third = device_folder.clone(device, "node-3")
    assert third.mac == "24:6F:28:00:00:13"             # the next free one


def test_new_devices_each_get_a_mac_of_their_own(tmp_path, firmware, fake_build):
    devices = tmp_path / "devices"
    first = device_folder.create(devices, "mesh-a", firmware)
    second = device_folder.create(devices, "mesh-b", firmware)
    assert first.mac == device_folder.DEFAULT_MAC == "02:54:44:4D:00:01"
    assert second.mac == "02:54:44:4D:00:02"
    assert fake_build[-1][2] == second.mac        # the eFuses carry it
    device_folder.remove(first)
    assert device_folder.create(devices, "mesh-c", firmware).mac == device_folder.DEFAULT_MAC
    with pytest.raises(device_folder.DeviceError, match="already has the MAC"):
        device_folder.create(devices, "mesh-d", firmware, mac="02:54:44:4D:00:02")
    assert not (devices / "mesh-d").exists()


def test_a_clone_refuses_a_taken_mac_or_a_running_original(tmp_path, firmware,
                                                          fake_build):
    device = device_folder.create(tmp_path / "devices", "node", firmware,
                                  mac="24:6F:28:00:00:11")
    with pytest.raises(device_folder.DeviceError, match="already has the MAC"):
        device_folder.clone(device, "twin", mac="24:6F:28:00:00:11")
    with pytest.raises(device_folder.DeviceError, match="not a MAC"):
        device_folder.clone(device, "twin", mac="nope")
    (device.root / device_folder.RUNNING).write_text("1 now\n")
    with pytest.raises(device_folder.DeviceBusy):
        device_folder.clone(device, "twin")
    assert not (tmp_path / "devices" / "twin").exists()
