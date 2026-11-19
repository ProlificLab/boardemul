#!/usr/bin/env python3
"""Typing into the serial ports, end to end except the guest (ADR 0156)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.run import build_argv  # noqa: E402
from emu.serial_input import SerialInput  # noqa: E402
from emu.skin import InputWriter  # noqa: E402


def test_each_port_reads_its_pipe(tmp_path: Path) -> None:
    pipes = {"uart": tmp_path / "uart.in", "usb": tmp_path / "usb.in"}
    argv = build_argv(Path("qemu"), Path("f.bin"), Path("e.bin"), tmp_path / "u.log",
                      tmp_path / "q.log", False, 8, usb=tmp_path / "usb.log",
                      serial_in=pipes)
    joined = " ".join(argv)
    assert f"file,id=uart0,path={tmp_path / 'u.log'},input-path={pipes['uart']}" in joined
    assert argv[argv.index("-serial") + 1] == "chardev:uart0"
    assert f"file,id=usbjtag,path={tmp_path / 'usb.log'},input-path={pipes['usb']}" in joined


def test_without_pipes_the_ports_only_write(tmp_path: Path) -> None:
    argv = build_argv(Path("qemu"), Path("f.bin"), Path("e.bin"), tmp_path / "u.log",
                      tmp_path / "q.log", False, 8, usb=tmp_path / "usb.log")
    assert argv[argv.index("-serial") + 1] == f"file:{tmp_path / 'u.log'}"
    assert "input-path" not in " ".join(argv)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes")
def test_what_is_typed_comes_out_of_the_pipe(tmp_path: Path) -> None:
    serial = SerialInput(tmp_path)
    try:
        reader = os.open(serial.paths["usb"], os.O_RDONLY | os.O_NONBLOCK)
        assert serial.write("usb", "hello\n") == 6
        assert os.read(reader, 64) == b"hello\n"
        os.close(reader)
        assert serial.write("nowhere", "x") == 0
    finally:
        serial.close()
    assert not (tmp_path / "usb.in").exists()


def test_the_input_file_carries_a_serial_event(tmp_path: Path) -> None:
    writer = InputWriter(tmp_path / "input.jsonl")
    writer.serial("usb", "help\n")
    import json
    assert json.loads((tmp_path / "input.jsonl").read_text().splitlines()[-1]) == \
        {"type": "serial", "port": "usb", "text": "help\n"}


# -- a serial port on TCP (ADR 0158) ------------------------------------------


class FakeSerial:
    def __init__(self):
        self.got: list[tuple[str, bytes]] = []

    def write_bytes(self, port: str, data: bytes) -> int:
        self.got.append((port, data))
        return len(data)


def test_a_tcp_client_reads_what_the_guest_writes_from_its_connection_on(tmp_path):
    import socket
    import time
    from emu.serial_input import SerialBridge
    log = tmp_path / "t.usb.log"
    log.write_bytes(b"before the client\n")
    serial = FakeSerial()
    bridge = SerialBridge("usb", 0, log, serial)
    try:
        client = socket.create_connection(bridge.address, timeout=5)
        time.sleep(0.2)
        with log.open("ab") as fh:
            fh.write(b"\x94\xc3\x00\x02hi")
        got = b""
        deadline = time.monotonic() + 5
        while len(got) < 6 and time.monotonic() < deadline:
            got += client.recv(64)
        assert got == b"\x94\xc3\x00\x02hi"            # not what came before
        client.sendall(b"\x94\xc3\x00\x02\x18\x01")
        deadline = time.monotonic() + 5
        while not serial.got and time.monotonic() < deadline:
            time.sleep(0.05)
        assert serial.got == [("usb", b"\x94\xc3\x00\x02\x18\x01")]
        client.close()
        # The next client may connect once the first has gone.
        second = socket.create_connection(bridge.address, timeout=5)
        deadline = time.monotonic() + 5
        while bridge.clients < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        second.close()
    finally:
        bridge.close()
    assert bridge.clients == 2


def test_a_serial_tcp_setting_is_a_port_and_a_port_name():
    from emu import device as device_folder
    assert device_folder.check_settings(["--serial-tcp", "4403"]) == ("--serial-tcp", "4403")
    assert device_folder.check_settings(["--serial-tcp", "5555:uart"])
    for bad in ("80", "70000", "4403:jtag", "../x"):
        with pytest.raises(device_folder.DeviceError):
            device_folder.check_settings(["--serial-tcp", bad])


def test_a_container_run_publishes_the_port_on_loopback(tmp_path):
    from emu import device as device_folder
    cmd = device_folder.container_command(tmp_path / "d", tmp_path / "o",
                                          ("--serial-tcp", "4403"))
    assert cmd[cmd.index("-p") + 1] == "127.0.0.1:4403:4403"
    assert "BOARDEMUL_BIND_ALL=1" in cmd
