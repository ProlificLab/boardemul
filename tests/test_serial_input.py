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
