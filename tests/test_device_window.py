#!/usr/bin/env python3
"""The window's device chooser and the run it starts (ADR 0134).

Fast: the command a started run gets, and the no-run paths of the handle.
The chooser's list is checked on a machine with a display, like the other
widget tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import device as device_folder  # noqa: E402
from emu import run  # noqa: E402


FIRMWARE_BYTES = bytes([0xE9]) + b"\x00" * 35


@pytest.fixture()
def firmware(tmp_path: Path) -> Path:
    path = tmp_path / "fw" / "demo.bin"
    path.parent.mkdir()
    path.write_bytes(FIRMWARE_BYTES)
    return path


@pytest.fixture()
def fake_build(tmp_path, monkeypatch):
    def build(firmware: Path, outdir: Path, mac: str,
              overlays: list[str]) -> tuple[Path, Path]:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "flash.bin").write_bytes(b"flash")
        (outdir / "efuse.bin").write_bytes(b"efuse")
        return outdir / "flash.bin", outdir / "efuse.bin"

    monkeypatch.setattr(run, "build_images", build)


def test_the_launch_command_is_the_emulators_own_flags(tmp_path):
    from emu import app
    root = tmp_path / "devices" / "pocket"
    outdir = tmp_path / "logs" / "pocket" / "1"
    cmd = device_folder.launch_command(root, outdir)
    joined = " ".join(cmd)
    assert "--device" in cmd and str(root) in cmd
    assert "--bus" in joined and "--outdir" in joined and str(outdir) in joined
    # A window-run device stops when its user stops it, not on a ceiling.
    assert str(device_folder.WINDOW_RUN_TIMEOUT) in cmd
    assert cmd[1].endswith("run.py")


def test_a_never_started_run_stops_as_nothing(tmp_path, firmware, fake_build):
    from emu.device import RunHandle
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    handle = RunHandle(device, logs=tmp_path / "logs")
    assert not handle.alive
    handle.stop()                       # a no-op, not an error
    assert not handle.alive


def test_the_run_directory_counts_up(tmp_path, firmware, fake_build):
    from emu.device import RunHandle
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    logs = tmp_path / "logs"
    first = RunHandle(device, logs=logs).outdir
    second = RunHandle(device, logs=logs).outdir
    assert first != second and second.parent == first.parent


# -- the chooser, on a machine that has a display ----------------------------

from tests.test_app_input import needs_display, tk_root  # noqa: E402


@needs_display
def test_the_chooser_lists_the_devices(tmp_path, firmware, fake_build):
    import tkinter as tk

    from emu.chooser import Chooser
    devices = tmp_path / "devices"
    device_folder.create(devices, "pocket", firmware)
    device_folder.create(devices, "desk", firmware, board="tdeck")

    root = tk_root()
    root.withdraw()
    try:
        chooser = Chooser(root, devices)
        rows = chooser.rows()
    finally:
        chooser.top.destroy()
        root.destroy()
    assert any(row.startswith("pocket") for row in rows), rows
    assert any(row.startswith("desk") for row in rows), rows


@needs_display
def test_starting_needs_a_selection(tmp_path, firmware, fake_build):
    import tkinter as tk

    from emu.chooser import Chooser
    devices = tmp_path / "devices"
    device_folder.create(devices, "pocket", firmware)

    root = tk_root()
    root.withdraw()
    try:
        chooser = Chooser(root, devices)
        chooser.selected = None
        chooser._start()
        assert chooser.pick is None
        chooser.select(0)
        chooser._start()
        assert chooser.pick is not None and chooser.pick[0] == "device"
        assert chooser.pick[1].name == "pocket"
    finally:
        chooser.top.destroy()
        root.destroy()


@needs_display
def test_the_form_creates_through_the_injected_creator(tmp_path, firmware, fake_build):
    import tkinter as tk

    from emu.chooser import Chooser
    devices = tmp_path / "devices"
    made = []

    def creator(devices, name, firmware_path):
        made.append((name, firmware_path))
        return device_folder.create(devices, name, firmware_path)

    root = tk_root()
    root.withdraw()
    try:
        chooser = Chooser(root, devices, creator=creator)
        assert chooser.creator is creator
        device = chooser.creator(devices, "pocket", firmware)
        assert made == [("pocket", firmware)]
        assert device.name == "pocket"
    finally:
        chooser.top.destroy()
        root.destroy()


@needs_display
def test_the_form_refuses_an_unnamed_device(tmp_path, firmware, fake_build):
    import tkinter as tk

    from emu.chooser import Chooser
    devices = tmp_path / "devices"
    made = []

    def creator(devices, name, firmware_path):
        made.append(name)
        return device_folder.create(devices, name, firmware_path)

    root = tk_root()
    root.withdraw()
    try:
        chooser = Chooser(root, devices, creator=creator)
        chooser.state = "form"
        chooser._create()
        assert made == [] and "name" in chooser.note_text
    finally:
        chooser.top.destroy()
        root.destroy()
