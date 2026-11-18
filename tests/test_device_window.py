#!/usr/bin/env python3
"""The window's device chooser and the run it starts (ADR 0134).

Fast: the command a started run gets, and the no-run paths of the handle.
The chooser's list is checked on a machine with a display, like the other
widget tests.
"""

from __future__ import annotations

import json
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
    root = tmp_path / "devices" / "pocket"
    outdir = tmp_path / "logs" / "pocket" / "1"
    cmd = device_folder.launch_command(root, outdir)
    joined = " ".join(cmd)
    assert "--device" in cmd and str(root) in cmd
    assert "--bus" in joined and "--outdir" in joined and str(outdir) in joined
    # A window-run device stops when its user stops it, not on a ceiling.
    assert str(device_folder.WINDOW_RUN_TIMEOUT) in cmd
    assert cmd[1].endswith("run.py")


def test_where_the_emulator_cannot_run_the_run_moves_into_the_container(
        tmp_path, monkeypatch):
    """ADR 0140: the same flags, inside `docker run`, with the repository, the
    folder and the output each mounted where the command names them."""
    monkeypatch.setenv("BOARDEMUL_IMAGE", "deck-image:7")
    root = tmp_path / "devices" / "my pocket"
    outdir = tmp_path / "logs" / "my pocket" / "3"
    cmd = device_folder.container_command(root, outdir, ("--wifi-ap", "deck"))
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert cmd[cmd.index("--name") + 1] == "boardemul-my-pocket-3"
    assert cmd[cmd.index("--stop-signal") + 1] == "SIGINT"
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert mounts == [f"{device_folder.REPO_ROOT.as_posix()}:/work",
                      f"{root.resolve().as_posix()}:/device",
                      f"{outdir.resolve().as_posix()}:/out"]
    tail = cmd[cmd.index("deck-image:7"):]
    assert tail[:3] == ["deck-image:7", "python3", "emu/run.py"]
    assert tail[tail.index("--device") + 1] == "/device"
    assert tail[tail.index("--outdir") + 1] == "/out"
    assert tail[-2:] == ["--wifi-ap", "deck"]


def test_a_container_run_on_an_air_meets_the_others_on_a_network(tmp_path):
    """ADR 0145: the airs' volume, the network, the board's address there,
    and each named air placed under the volume."""
    root = tmp_path / "devices" / "node"
    outdir = tmp_path / "logs" / "node" / "1"
    cmd = device_folder.container_command(
        root, outdir, ("--air", "mesh", "--ble-air", "talk", "--at", "5,0"))
    assert f"{device_folder.AIR_VOLUME}:/air" in cmd
    assert cmd[cmd.index("--network") + 1] == device_folder.AIR_NETWORK
    assert "BOARDEMUL_AIR_HOST=auto" in cmd
    assert cmd[-6:] == ["--air", "/air/lora/mesh", "--ble-air", "/air/ble/talk",
                        "--at", "5,0"]


def test_a_container_run_off_the_air_is_started_as_before(tmp_path):
    cmd = device_folder.container_command(tmp_path / "d", tmp_path / "o",
                                          ("--reboot",))
    assert "--network" not in cmd and "BOARDEMUL_AIR_HOST=auto" not in cmd


def test_a_run_on_this_host_keeps_its_airs_beside_the_folders(tmp_path):
    root = tmp_path / "devices" / "node"
    cmd = device_folder.launch_command(root, tmp_path / "o", ("--air", "mesh"))
    assert cmd[-2:] == ["--air", f"{(tmp_path / 'devices').resolve().as_posix()}/.air/lora/mesh"]


def test_a_run_is_told_the_devices_settings_then_its_own(tmp_path, firmware,
                                                         fake_build, monkeypatch):
    from emu.device import RunHandle
    import subprocess
    monkeypatch.setenv("BOARDEMUL_RUN_IN", "native")
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    device = device_folder.set_settings(device, ["--reboot", "--air", "mesh"])
    assert device_folder.load(device.root).settings == ("--reboot", "--air", "mesh")
    seen = []

    class Fake:
        def __init__(self, cmd, **_kw):
            seen.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", Fake)
    RunHandle(device, logs=tmp_path / "logs", extra=("--icount",)).start()
    tail = seen[0][-4:]
    assert tail[0] == "--reboot" and tail[1] == "--air"
    assert tail[2].endswith("/.air/lora/mesh") and tail[3] == "--icount"


@pytest.mark.parametrize("bad", [["--air", "/tmp/x"], ["--qemu", "q"], ["--gps"]])
def test_a_device_keeps_only_settings_from_the_list(tmp_path, firmware,
                                                    fake_build, bad):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    with pytest.raises(device_folder.DeviceError):
        device_folder.set_settings(device, bad)


def test_the_host_decides_where_a_run_starts(monkeypatch):
    monkeypatch.setenv("BOARDEMUL_RUN_IN", "container")
    assert not device_folder.runs_natively()
    monkeypatch.setenv("BOARDEMUL_RUN_IN", "native")
    assert device_folder.runs_natively()
    monkeypatch.delenv("BOARDEMUL_RUN_IN")
    import socket
    assert device_folder.runs_natively() == hasattr(socket, "AF_UNIX")


def test_a_container_run_without_docker_says_so(tmp_path, firmware, fake_build,
                                                  monkeypatch):
    from emu.device import RunHandle
    monkeypatch.setenv("BOARDEMUL_RUN_IN", "container")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    handle = RunHandle(device, logs=tmp_path / "logs")
    with pytest.raises(RuntimeError, match="docker is not on PATH"):
        handle.start()


def test_a_folder_names_its_firmware_with_forward_slashes(tmp_path, firmware,
                                                          fake_build):
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    assert "\\" not in device.firmware
    manifest = device.root / device_folder.MANIFEST
    fields = json.loads(manifest.read_text())
    fields["firmware"] = "firmware\\T-Deck.bin"       # as Windows once wrote it
    manifest.write_text(json.dumps(fields))
    assert device_folder.load(device.root).firmware == "firmware/T-Deck.bin"


def test_a_run_tells_why_it_ended(tmp_path, firmware, fake_build):
    from emu.device import RunHandle
    device = device_folder.create(tmp_path / "devices", "pocket", firmware)
    handle = RunHandle(device, logs=tmp_path / "logs")
    assert handle.console_tail() == []
    (handle.outdir / "console.log").write_text(
        "".join(f"line {i}\n" for i in range(100)) + "AttributeError: no AF_UNIX\n")
    tail = handle.console_tail()
    assert len(tail) == 40 and tail[-1] == "AttributeError: no AF_UNIX"
    (handle.outdir / "console.log").write_text(
        "\x1b[1;35mAttributeError\x1b[0m: \x1b[35mno AF_UNIX\x1b[0m\n")
    assert handle.console_tail() == ["AttributeError: no AF_UNIX"]


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
